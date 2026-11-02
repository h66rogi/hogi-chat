#!/usr/bin/env python3
import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
import config  # noqa: E402  (repo root)
"""방송 지각(perception): 화면 캡처 + 음성 전사를 컨텍스트로 만든다.

- 화면: SOOP 라이브 썸네일(liveimg) 20초마다 1장 → 비전 모델이 한 줄 설명 → scene.jsonl
- 음성: 라이브 HLS 오디오 → 10초 세그먼트 → STT → transcript.jsonl
  (HLS 오디오는 현재 CDN 401. 아래 `audio_test()`로 상태를 확인한다.)
- 비용: OpenRouter 응답의 usage.cost 를 usage.jsonl 에 적고, 월 한도(state.budget_usd)를 넘으면 멈춘다.

사용:
  python3 perceive.py --once            # 1회: 썸네일 + 오디오 시도
  python3 perceive.py --loop            # 상시 (state.perceive 가 true 일 때만 동작)
  python3 perceive.py --thumb [--describe]
  python3 perceive.py --audio-test      # HLS 오디오 접근 상태 진단
  python3 perceive.py --transcribe <wav>
  python3 perceive.py --report          # 이번 달 사용액/건수
"""
import base64
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.parse
import urllib.request

BASE = config.BASE
ENV_PATH = config.ENV_PATH
THUMB_DIR = os.path.join(BASE, "frames")
AUDIO_DIR = os.path.join(BASE, "audio")
SCENE = os.path.join(BASE, "scene.jsonl")
TRANSCRIPT = os.path.join(BASE, "transcript.jsonl")
USAGE = os.path.join(BASE, "usage.jsonl")
LOG = os.path.join(BASE, "perceive.log")

BJID = config.BJ_ID
VISION_MODEL = "google/gemini-3.5-flash-lite"
STT_MODEL = "google/gemini-3.5-flash"
NATIVE_SCRIPT = os.path.join(BASE, "vendor/recorder/soop_native.py")
NATIVE_PY = "/opt/homebrew/bin/python3.14"   # soop_native 는 3.12+ 필요
THUMB_EVERY = 300     # 초. 화면 설명은 기본으로 쓰지 않는다(운영자 2026-10-24: 예외적으로만).
SHOW_CUE = __import__("re").compile(r"(이거|이걸|이게|저거|요거)\s*(봐|보세|보라|뭐|좀 봐|보이)|봐봐|보이시|이것 봐")
AUDIO_SECS = 10       # 세그먼트 길이
MONTHLY_CAP = 100.0   # 달러


def secret(name):
    """키체인(secretsrc) → 환경변수 순으로 시크릿을 읽는다."""
    try:
        import secretsrc
        v = secretsrc.get(name)
        if v:
            return v
    except Exception:
        pass
    return os.environ.get(name, "")


def log(msg):
    try:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(time.strftime("%Y-%m-%d %H:%M:%S") + " " + msg + "\n")
    except Exception:
        pass


def env(key):
    try:
        for line in open(ENV_PATH, encoding="utf-8"):
            if line.startswith(key + "="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    except FileNotFoundError:
        pass
    return ""


KEY = env("OPENROUTER_API_KEY")


def load_state():
    try:
        return json.load(open(os.path.join(BASE, "state.json"), encoding="utf-8"))
    except Exception:
        return {}


def read_jsonl(path, tail=0):
    out = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except Exception:
                    continue
    except FileNotFoundError:
        return out
    return out[-tail:] if tail else out


# ------------------------------------------------------------------ cost ledger

def month_key(ts=None):
    return time.strftime("%Y-%m", time.localtime(ts or time.time()))


def month_cost(ts=None):
    mk = month_key(ts)
    total = 0.0
    calls = 0
    for r in read_jsonl(USAGE):
        if r.get("month") == mk:
            total += float(r.get("cost") or 0)
            calls += 1
    return total, calls


def budget_ok():
    cost, _ = month_cost()
    return cost < MONTHLY_CAP, cost


def record_usage(kind, model, usage):
    rec = {"ts": int(time.time()), "month": month_key(), "kind": kind, "model": model,
           "cost": float((usage or {}).get("cost") or 0),
           "in_tok": (usage or {}).get("prompt_tokens"), "out_tok": (usage or {}).get("completion_tokens")}
    with open(USAGE, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return rec["cost"]


# ------------------------------------------------------------------ SOOP

def live_info(bjid=BJID):
    body = urllib.parse.urlencode({"bid": bjid, "type": "live",
                                   "player_type": "html5", "mode": "landing"}).encode()
    req = urllib.request.Request("https://live.sooplive.co.kr/afreeca/player_live_api.php",
                                 data=body, headers={"User-Agent": "Mozilla/5.0"})
    try:
        d = json.loads(urllib.request.urlopen(req, timeout=10).read())
    except Exception as e:
        return {"live": False, "error": str(e)}
    ch = d.get("CHANNEL") or {}
    return {"live": ch.get("RESULT") == 1, "title": ch.get("TITLE"), "bno": ch.get("BNO"),
            "ts": ch.get("TS"), "chatno": ch.get("CHATNO")}


def thumb_url(bjid=BJID, bno=None):
    # SOOP 라이브 썸네일. 방송 중이면 수 초~수십 초 주기로 갱신된다.
    if bno:
        return f"https://liveimg.sooplive.com/m/{bno}"
    return f"https://liveimg.sooplive.com/m/{bjid}"


def grab_thumb(bjid=BJID):
    info = live_info(bjid)
    if not info.get("live"):
        return None, info
    os.makedirs(THUMB_DIR, exist_ok=True)
    url = thumb_url(bjid, info.get("bno"))
    ts = int(time.time())
    path = os.path.join(THUMB_DIR, f"{ts}.jpg")
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        data = urllib.request.urlopen(req, timeout=10).read()
        if len(data) < 2000:
            return None, {**info, "error": f"thumbnail too small ({len(data)}B)"}
        open(path, "wb").write(data)
        return path, info
    except Exception as e:
        return None, {**info, "error": str(e)}


def grab_audio(seconds=AUDIO_SECS, out=None):
    """SOOP 네이티브 수신(vlife recorder)으로 오디오를 얻는다.

    player_live_api의 CDN HLS는 익명 토큰을 거부하지만(401), vlife가 쓰는 네이티브
    프로토콜 경로는 실제 방송 미디어를 받는다. 받은 HLS 파트에서 오디오만 뽑는다.
    수신이 ProtocolError로 자주 끊겨서 min-height와 길이를 바꿔가며 재시도한다.
    """
    cookie = ensure_cookies()
    last_err = ""
    for attempt, (mh, add) in enumerate((("360", 0), ("720", 6), ("480", 12)), start=1):
        work = os.path.join(AUDIO_DIR, f"nat-{int(time.time())}-{attempt}")
        # soop_native 는 출력 디렉터리를 직접 만들어야 한다(존재하면 FileExistsError).
        if os.path.isdir(work):
            shutil.rmtree(work, ignore_errors=True)
        os.makedirs(AUDIO_DIR, exist_ok=True)
        cmd = [NATIVE_PY, NATIVE_SCRIPT, f"https://play.sooplive.com/{BJID}",
               "--output", work, "--seconds", str(seconds + add), "--min-height", mh,
               "--segment-seconds", "4"]
        if cookie:
            cmd += ["--cookies", cookie]
        try:
            p = subprocess.run(cmd, capture_output=True, text=True, timeout=seconds + 240)
        except subprocess.TimeoutExpired:
            last_err = "네이티브 수신 타임아웃"
            continue
        parts = sorted(glob.glob(os.path.join(work, "part-*", "index.m3u8")))
        if not parts:
            last_err = "네이티브 수신 실패: " + (p.stderr or p.stdout or "")[-200:].replace("\n", " ")
            continue
        break
    else:
        return None, last_err or "네이티브 수신 실패(모든 시도)"
    wav = out or os.path.join(work, "audio.wav")
    args = ["/opt/homebrew/bin/ffmpeg", "-hide_banner", "-loglevel", "error", "-y"]
    for part in parts:
        args += ["-i", part]
    if len(parts) > 1:
        args += ["-filter_complex", f"concat=n={len(parts)}:v=0:a=1"]
    args += ["-vn", "-ac", "1", "-ar", "16000", wav]
    r = subprocess.run(args, capture_output=True, text=True, timeout=180)
    if r.returncode != 0 or not os.path.exists(wav) or os.path.getsize(wav) < 2000:
        return None, "오디오 추출 실패: " + (r.stderr or "")[-200:]
    return wav, "ok"


def ensure_cookies(max_age=86400):
    """SOOP 로그인 쿠키(Netscape)를 준비한다. 하루 지나면 다시 읽는다."""
    path = os.path.join(BASE, ".soop_cookies.txt")
    try:
        if os.path.exists(path) and time.time() - os.path.getmtime(path) < max_age:
            return path
    except OSError:
        pass
    try:
        subprocess.run(["/usr/bin/python3", os.path.join(BASE, "read_cookies.py")],
                       capture_output=True, text=True, timeout=120)
        auth = json.load(open(os.path.join(BASE, ".soop_auth.json"), encoding="utf-8"))
        if not auth:
            return None
        exp = int(time.time()) + 7 * 86400
        lines = ["# Netscape HTTP Cookie File"]
        for k, v in auth.items():
            lines.append(f".sooplive.com\tTRUE\t/\tTRUE\t{exp}\t{k}\t{v}")
        open(path, "w", encoding="utf-8").write("\n".join(lines) + "\n")
        os.chmod(path, 0o600)
        return path
    except Exception as e:
        log(f"[쿠키 실패] {e}")
        return None


# ------------------------------------------------------------------ models

def _chat(model, content, max_tokens=120):
    ok, cost = budget_ok()
    if not ok:
        raise RuntimeError(f"월 한도 초과: ${cost:.2f} / ${MONTHLY_CAP:.0f}")
    body = json.dumps({"model": model, "messages": [{"role": "user", "content": content}],
                       "max_tokens": max_tokens, "temperature": 0}).encode()
    req = urllib.request.Request("https://openrouter.ai/api/v1/chat/completions", data=body,
                                 headers={"Authorization": f"Bearer {KEY}",
                                          "Content-Type": "application/json"})
    r = json.loads(urllib.request.urlopen(req, timeout=120).read())
    msg = r["choices"][0]["message"]
    return (msg.get("content") or "").strip(), r.get("usage") or {}


def describe_image(path):
    b64 = base64.b64encode(open(path, "rb").read()).decode()
    mime = "image/jpeg"
    text, usage = _chat(VISION_MODEL, [
        {"type": "text", "text": ("SOOP 방송 화면 캡처다. 시청자 채팅을 쓰는 사람에게 필요한 장면 정보만 "
                                  "한국어 한 줄, 40자 이내로. 게임/화면/인물 상태 중심. 추측은 하지 말고 보이는 것만.")},
        {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}}], max_tokens=80)
    cost = record_usage("vision", VISION_MODEL, usage)
    return text, cost


def _openai_key():
    for k in ("OPENAI_API_KEY", "VOICE_TOOLS_OPENAI_KEY"):
        try:
            v = secret(k)
        except Exception:
            v = None
        if v:
            return v
    return ""


def transcribe_openai(path):
    """OpenAI gpt-4o-transcribe 로 한국어 말소리만 받아쓴다."""
    key = _openai_key()
    if not key:
        raise RuntimeError("OPENAI_API_KEY 없음")
    model = os.environ.get("STT_GPT_MODEL", "gpt-4o-transcribe")
    boundary = "----hogi" + str(int(time.time() * 1000))
    data = open(path, "rb").read()
    parts = []

    def field(name, value):
        parts.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n{value}\r\n".encode())

    field("model", model)
    field("language", "ko")
    field("response_format", "json")
    field("temperature", "0")
    field("prompt", "한국어 방송 말소리 받아쓰기. 노래 가사·음악·효과음·자막은 옮기지 않는다.")
    parts.append(
        (f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"a.wav\"\r\n"
         f"Content-Type: audio/wav\r\n\r\n").encode() + data + b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode())
    body = b"".join(parts)
    req = urllib.request.Request(
        "https://api.openai.com/v1/audio/transcriptions", data=body,
        headers={"Authorization": f"Bearer {key}",
                 "Content-Type": f"multipart/form-data; boundary={boundary}"},
        method="POST")
    r = json.loads(urllib.request.urlopen(req, timeout=180).read())
    text = (r.get("text") or "").strip()
    secs = max(1.0, os.path.getsize(path) / 32000.0)  # 16k mono 16bit
    cost = record_usage("stt", model, {"seconds": secs, "minutes": secs / 60.0})
    return clean_transcript(text), cost


def transcribe(path):
    """GPT(OpenAI) 우선. 키가 없으면 OpenRouter 오디오 모델로 폴백."""
    try:
        return transcribe_openai(path)
    except Exception as e:
        last = e
    b64 = base64.b64encode(open(path, "rb").read()).decode()
    text, usage = _chat(STT_MODEL, [
        {"type": "text", "text": ("SOOP 한국어 방송 오디오다. 들리는 말소리만 그대로 받아쓰기한다. "
                                 "노래 가사·음악·효과음·자막은 절대 옮기지 않는다. "
                                 "말소리가 없거나 확신이 없으면 정확히 'NONE'만 출력한다.")},
        {"type": "input_audio", "input_audio": {"data": b64, "format": "wav"}}], max_tokens=200)
    cost = record_usage("stt", STT_MODEL, usage)
    log(f"[STT 폴백] openai 실패({last}) → {STT_MODEL}")
    return clean_transcript(text), cost


def clean_transcript(text):
    """전사 후처리: 말소리가 아닌 것(가사·효과음·환각)을 버린다."""
    t = (text or "").strip().strip('"').strip("'")
    if t.upper().startswith("NONE") or not t:
        return ""
    t = " ".join(t.split())
    hangul = sum(1 for c in t if "가" <= c <= "힣")
    if len(t) < 3 or hangul < max(2, len(t) // 3):
        return ""
    if re.search(r"\[(음악|박수|웃음|노래|효과음)\]", t):
        return ""
    return t


def append(path, rec):
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


# ------------------------------------------------------------------ loop

def tick_frame():
    path, info = grab_thumb()
    if not path:
        return None
    try:
        text, cost = describe_image(path)
    except Exception as e:
        log(f"[화면 실패] {e}")
        return None
    rec = {"ts": int(time.time()), "image": os.path.basename(path), "text": text, "cost": cost}
    append(SCENE, rec)
    log(f"[화면] {text} (${cost:.5f})")
    return rec


def tick_audio():
    info = live_info()
    if not info.get("live"):
        return None
    wav, why = grab_audio()
    if not wav:
        log(f"[음성 불가] {why}")
        return None
    try:
        text, cost = transcribe(wav)
    except Exception as e:
        log(f"[STT 실패] {e}")
        return None
    rec = {"ts": int(time.time()), "text": text, "cost": cost}
    append(TRANSCRIPT, rec)
    log(f"[음성] {text} (${cost:.5f})")
    try:
        profile = os.path.dirname(wav)
        shutil.rmtree(profile, ignore_errors=True)
    except Exception:
        pass
    return rec


def recent(kind, seconds=120, path=None, field="text"):
    cutoff = time.time() - seconds
    rows = [r for r in read_jsonl(path) if r.get("ts", 0) >= cutoff and (r.get(field) or "").strip()]
    return rows[-3:]


def main():
    global BJID
    args = sys.argv[1:]
    if "--bjid" in args:
        BJID = args[args.index("--bjid") + 1]
        args = [a for i, a in enumerate(args) if a != "--bjid" and (i == 0 or args[i - 1] != "--bjid")]
    if not args:
        args = ["--once"]
    if args[0] == "--report":
        cost, calls = month_cost()
        kinds = {}
        for r in read_jsonl(USAGE):
            if r.get("month") == month_key():
                kinds[r["kind"]] = kinds.get(r["kind"], 0) + 1
        print(f"{month_key()}: ${cost:.4f} / ${MONTHLY_CAP:.0f} · 호출 {calls}건 · {kinds}")
        return
    if args[0] == "--audio-test":
        wav, why = grab_audio(5)
        info = live_info()
        print("live:", info.get("live"), "| title:", info.get("title"))
        print("TS:", (info.get("ts") or "")[:90])
        print("audio:", wav or f"실패 - {why}")
        if wav:
            text, cost = transcribe(wav)
            print("STT:", repr(text), f"${cost:.5f}")
        return
    if args[0] == "--thumb":
        path, info = grab_thumb()
        print("live:", info.get("live"), "| title:", info.get("title"), "| img:", path, info.get("error") or "")
        if path and "--describe" in args:
            text, cost = describe_image(path)
            print("scene:", text, f"${cost:.5f}")
        return
    if args[0] == "--describe":
        text, cost = describe_image(args[1])
        print(text, f"${cost:.5f}")
        return
    if args[0] == "--transcribe":
        text, cost = transcribe(args[1])
        print(repr(text), f"${cost:.5f}")
        return
    if args[0] == "--once":
        tick_frame()
        tick_audio()
        return
    if args[0] == "--loop":
        log("=== perceive 시작 ===")
        last_frame = 0
        audio_retry_at = 0
        while True:
            try:
                st = load_state()
                if not st.get("perceive"):
                    time.sleep(15)
                    continue
                if time.time() - last_frame >= THUMB_EVERY:
                    tick_frame()
                    last_frame = time.time()
                if time.time() >= audio_retry_at:
                    if tick_audio() is None:
                        audio_retry_at = time.time() + 300  # 실패하면 5분 뒤 재시도
            except KeyboardInterrupt:
                raise
            except Exception as e:
                log(f"[오류] {e}")
            time.sleep(2)
    print("usage: --once | --loop | --thumb [--describe] | --audio-test | --transcribe <wav> | --report")


if __name__ == "__main__":
    main()
