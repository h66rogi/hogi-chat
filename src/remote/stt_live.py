#!/usr/bin/env python3
"""BJ 실시간 전사 (서버 전용).

SOOP 방송을 끊김 없이 받아(soop_native, 2초 HLS 조각) 오디오만 24kHz PCM으로 풀고,
간단한 음량 기반 말소리 감지(VAD)로 말하는 구간만 OpenAI Realtime 전사 세션에 흘려 넣는다.
말이 끝나면(0.7초 침묵) 또는 12초마다 commit해서 문장 단위 결과를 transcript.jsonl에 쓴다.

예전 방식(20초 녹음 → 파일 전사)은 매 조각마다 새로 접속해 조각 사이가 비고 결과가 20~45초 늦었다.
파일 전사는 조각 사이가 비고 20~45초 늦어서 실시간 판단에 쓸 수 없었다. 예전 방식은 stt_oci.py(hogi-stt)로 남겨 둔다.
"""

import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))))
import config  # noqa: E402  (repo root)
import array
import base64
import glob
import json
import math
import os
import shutil
import subprocess
import sys
import threading
import time
from collections import deque

import websocket

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)
import stt_oci as base  # noqa: E402  (clean, scene_on_cue, env, month_spent, 경로 상수 재사용)

WORK = "/tmp/hogi-stt-live"
base.WORK = WORK  # scene_on_cue가 최근 영상 조각을 여기서 찾는다
MODEL = os.environ.get("HOGI_LIVE_STT_MODEL", "gpt-live-transcribe")
DELAY = os.environ.get("HOGI_LIVE_STT_DELAY", "medium")
RATE = 24000
FRAME = 480  # 20ms
SPEECH_DB = float(os.environ.get("HOGI_VAD_DB", "-38"))
END_SILENCE = 0.7
MAX_TURN = 12.0
PREROLL = 0.3
PRICE_PER_MIN = float(os.environ.get("HOGI_LIVE_STT_PRICE", "0.017"))
KEYWORDS = [w for w in os.environ.get("HOGI_STT_KEYWORDS", "").split(",") if w]
PROMPT = ("%s인터넷 방송. 방송인의 한국어 말소리만 받아쓴다. 노래 가사, 배경음악, 효과음은 옮기지 않는다."
          % ((config.BJ_NAME + " ") if config.BJ_NAME else ""))


def log(msg):
    base.log("[live] " + msg)


class Transcriber:
    """OpenAI Realtime 전사 세션. 끊기면 다시 붙는다."""

    def __init__(self):
        self.ws = None
        self.lock = threading.Lock()
        self.ready = threading.Event()
        self.pending = deque()  # commit한 구간의 (시작, 끝, 초) — committed 이벤트 순서대로 item_id에 붙인다
        self.items = {}
        self.last_ok = time.time()

    def connect(self):
        key = base.env("OPENAI_API_KEY")
        self.ready.clear()
        self.ws = websocket.WebSocketApp(
            "wss://api.openai.com/v1/realtime?intent=transcription",
            header=[f"Authorization: Bearer {key}"],
            on_open=self._on_open, on_message=self._on_message,
            on_error=lambda ws, e: log(f"[세션 오류] {type(e).__name__}: {str(e)[:160]}"),
            on_close=lambda ws, c, m: (self.ready.clear(), log(f"[세션 닫힘] {c} {str(m)[:80]}")))
        threading.Thread(target=self.ws.run_forever, kwargs={"ping_interval": 20}, daemon=True).start()
        if not self.ready.wait(15):
            log("[세션 준비 실패] 15초 안에 session.updated 없음")
            return False
        return True

    def _on_open(self, ws):
        ws.send(json.dumps({"type": "session.update", "session": {
            "type": "transcription",
            "audio": {"input": {
                "format": {"type": "audio/pcm", "rate": RATE},
                "transcription": {"model": MODEL, "prompt": PROMPT, "keywords": KEYWORDS,
                                  "languages": ["ko"], "delay": DELAY},
                "turn_detection": None}}}}))

    def _on_message(self, ws, raw):
        try:
            ev = json.loads(raw)
        except Exception:
            return
        t = ev.get("type", "")
        if t in ("session.updated", "transcription_session.updated", "session.created") and t != "session.created":
            self.ready.set()
            log(f"[세션 준비] model={MODEL} delay={DELAY}")
        elif t == "input_audio_buffer.committed":
            with self.lock:
                span = self.pending.popleft() if self.pending else (time.time(), time.time(), 0)
            self.items[ev.get("item_id")] = span
        elif t == "conversation.item.input_audio_transcription.completed":
            span = self.items.pop(ev.get("item_id"), (time.time(), time.time(), 0))
            self.last_ok = time.time()
            write(ev.get("transcript") or "", span)
        elif t == "conversation.item.input_audio_transcription.failed" or t == "error":
            log(f"[전사 실패] {json.dumps(ev, ensure_ascii=False)[:240]}")
            if t == "error" and not self.ready.is_set():
                self.ready.set()  # 설정 오류도 대기를 풀어 로그를 남기고 재시도하게

    def alive(self):
        return self.ws is not None and self.ws.sock is not None and self.ws.sock.connected and self.ready.is_set()

    def append(self, pcm):
        self.ws.send(json.dumps({"type": "input_audio_buffer.append",
                                 "audio": base64.b64encode(pcm).decode()}))

    def commit(self, start, end, secs):
        with self.lock:
            self.pending.append((start, end, secs))
        self.ws.send(json.dumps({"type": "input_audio_buffer.commit"}))

    def close(self):
        try:
            self.ws.close()
        except Exception:
            pass


def write(text, span):
    start, end, secs = span
    cleaned = base.clean(text)
    cost = round(secs / 60.0 * PRICE_PER_MIN, 6)
    with open(base.USAGE, "a", encoding="utf-8") as f:
        f.write(json.dumps({"ts": int(time.time()), "ym": time.strftime("%Y-%m"), "kind": "stt",
                            "model": MODEL, "cost": cost, "seconds": round(secs, 1)}) + "\n")
    if not cleaned:
        log(f"[버림] {text[:80]!r} {secs:.1f}s")
        return
    rec = {"ts": int(start), "end": int(end), "text": cleaned, "cost": cost, "live": True,
           "seconds": round(secs, 1), "lag": round(time.time() - end, 1)}
    with open(base.OUT, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    log(f"[전사] {cleaned!r} {secs:.1f}s 지연 {rec['lag']}s")
    try:
        base.scene_on_cue(cleaned)
    except Exception as e:
        log(f"[화면 예외 실패] {type(e).__name__}: {e}")


def decode(seg):
    """조각 하나를 24kHz mono s16le로. 방송 음량이 낮아 말소리 정규화를 건다."""
    r = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", seg, "-vn",
                        "-af", "highpass=f=80,speechnorm=e=6:r=0.0001:l=1",
                        "-ac", "1", "-ar", str(RATE), "-f", "s16le", "pipe:1"],
                       capture_output=True, timeout=30)
    return r.stdout if r.returncode == 0 else b""


def db(frame):
    a = array.array("h", frame)
    if not a:
        return -120.0
    s = sum(x * x for x in a) / len(a)
    return 10 * math.log10(s / (32768.0 ** 2) + 1e-12)


class Vad:
    """말하는 구간만 보낸다(비용·오인식 감소). 침묵 0.7초면 commit, 12초 넘으면 강제 commit."""

    def __init__(self, tr):
        self.tr = tr
        self.pre = deque(maxlen=int(PREROLL * RATE / FRAME))
        self.in_turn = False
        self.turn_start = 0.0
        self.turn_secs = 0.0
        self.silence = 0.0
        self.voiced = 0.0
        self.ema = None
        self.dip = 0.0

    def feed(self, pcm, wall_end):
        n = len(pcm) // (FRAME * 2)
        for i in range(n):
            fr = pcm[i * FRAME * 2:(i + 1) * FRAME * 2]
            t = wall_end - (n - i) * FRAME / RATE
            speech = db(fr) > SPEECH_DB
            if not self.in_turn:
                self.pre.append(fr)
                if speech:
                    self.in_turn, self.turn_start, self.silence, self.voiced = True, t, 0.0, 0.0
                    buf = b"".join(self.pre)
                    self.pre.clear()
                    self.tr.append(buf)
                    self.turn_secs = len(buf) / 2 / RATE
                continue
            self.tr.append(fr)
            self.turn_secs += FRAME / RATE
            d = db(fr)
            self.ema = d if self.ema is None else 0.97 * self.ema + 0.03 * d
            # 배경음악이 깔리면 완전한 침묵이 없다. 구간 평균보다 8dB 이상 낮은 0.25초를 '숨 쉬는 틈'으로 본다.
            self.dip = self.dip + FRAME / RATE if d < self.ema - 8 else 0.0
            if speech:
                self.silence = 0.0
                self.voiced += FRAME / RATE
            else:
                self.silence += FRAME / RATE
            pause = self.turn_secs >= 5 and self.dip >= 0.25
            if self.silence >= END_SILENCE or pause or self.turn_secs >= MAX_TURN:
                self.ema, self.dip = None, 0.0
                if self.voiced >= 0.3:
                    self.tr.commit(self.turn_start, t, self.turn_secs)
                else:
                    self.tr.ws.send(json.dumps({"type": "input_audio_buffer.clear"}))
                self.in_turn = False


def segments():
    """완성된 조각 경로(part 순서, 조각 번호 순서). 마지막 조각은 쓰는 중일 수 있어 제외."""
    out = []
    parts = sorted(glob.glob(os.path.join(WORK, "part-*")))
    for i, part in enumerate(parts):
        segs = sorted(glob.glob(os.path.join(part, "segment-*.ts")))
        out += [(s, part) for s in (segs if i < len(parts) - 1 else segs[:-1])]
    return out


def run_once():
    if not base.BJ:
        log("[설정 오류] HOGI_BJ_ID가 비어 있다")
        time.sleep(60)
        return
    if not os.path.exists(base.REC):
        log(f"[레코더 없음] {base.REC} — HOGI_RECORDER로 경로를 지정한다")
        time.sleep(60)
        return
    if not base.is_live():
        log("[방송 꺼짐] 45초 뒤 다시 확인")
        time.sleep(45)
        return
    if base.month_spent() >= base.MONTHLY_CAP:
        log(f"[예산 초과] 이번 달 ${base.month_spent():.2f} ≥ ${base.MONTHLY_CAP}")
        time.sleep(300)
        return
    shutil.rmtree(WORK, ignore_errors=True)
    rec_log = open(WORK + ".log", "w")
    rec = subprocess.Popen([sys.executable, base.REC, f"https://play.sooplive.com/{base.BJ}", "--output", WORK,
                            "--seconds", "0", "--segment-seconds", "2", "--min-height", "360",
                            "--session-seconds", "170", "--max-gb", "40", "--cookies", base.COOKIES],
                           stdout=rec_log, stderr=subprocess.STDOUT)
    tr = Transcriber()
    if not tr.connect():
        rec.terminate()
        time.sleep(20)
        return
    vad = Vad(tr)
    done, last_seg = set(), time.time()
    log("[시작] 수신·전사 연결됨")
    try:
        while rec.poll() is None:
            fresh = [(s, p) for s, p in segments() if s not in done]
            if not fresh:
                if time.time() - last_seg > 60:
                    log("[수신 정지] 60초간 새 조각 없음 → 재시작")
                    break
                time.sleep(0.3)
                continue
            for seg, part in fresh:
                pcm = decode(seg)
                done.add(seg)
                last_seg = time.time()
                if not tr.alive():
                    log("[세션 재연결]")
                    tr.close()
                    tr = Transcriber()
                    if not tr.connect():
                        raise RuntimeError("전사 세션 재연결 실패")
                    vad.tr = tr
                    vad.in_turn = False
                if pcm:
                    vad.feed(pcm, time.time())
            # 화면 예외용으로 마지막 조각 몇 개만 남기고 지운다(디스크 보호)
            for s, _ in segments()[:-4]:
                try:
                    os.remove(s)
                except FileNotFoundError:
                    pass
            if len(done) > 5000:
                done = set(list(done)[-500:])
        log(f"[수신 종료] exit={rec.poll()} {open(WORK + '.log').read()[-200:]!r}")
    finally:
        if rec.poll() is None:
            rec.terminate()
            try:
                rec.wait(10)
            except subprocess.TimeoutExpired:
                rec.kill()
        tr.close()


def main():
    log("=== 실시간 전사 시작 ===")
    while True:
        try:
            run_once()
        except Exception as e:
            log(f"[오류] {type(e).__name__}: {e}")
        time.sleep(5)


if __name__ == "__main__":
    main()
