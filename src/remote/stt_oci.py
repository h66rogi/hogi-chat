#!/usr/bin/env python3
import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))))
import config  # noqa: E402  (repo root)
"""OCI에서 도는 BJ 전사 수집기 (후로기 채팅 릴레이용).

맥미니·집 회선에서는 SOOP 오디오가 1초 만에 끊겨서 쓸 수 없다(2026-10-24 실측:
9세션 45초에 0.5초). OCI(vlife-rec-1)에서는 같은 recorder로 30초에 오디오 712KB가
정상으로 들어온다. 그래서 수집·전사는 여기서 하고, 결과만 transcript.jsonl에 쌓는다.

흐름: soop_native(20초) → ffmpeg 16k mono wav → GPT 오디오(OpenRouter openai/gpt-audio-mini)
      → 정제 → transcript.jsonl
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
import urllib.request

BASE = os.path.dirname(os.path.abspath(__file__))
REC = os.environ.get("HOGI_RECORDER") or os.path.join(BASE, "vendor", "recorder", "soop_native.py")
COOKIES = os.path.join(BASE, "anon_cookies.txt")
OUT = os.path.join(BASE, "transcript.jsonl")
USAGE = os.path.join(BASE, "usage.jsonl")
SCENE = os.path.join(BASE, "scene.jsonl")
VISION_MODEL = os.environ.get("HOGI_VISION_MODEL", "google/gemini-3.5-flash")
# BJ가 무언가를 보여주며 말할 때만 화면을 본다(운영자 2026-10-24: 화면 인식은 예외적으로만).
SHOW_CUE = re.compile(r"(이거|이걸|이게|저거|요거)\s*(봐|보세|보라|뭐|좀 봐|보이)|봐봐|보이시|이것 봐|어때\s*\?")
LOG = os.path.join(BASE, "stt.log")
WORK = "/tmp/hogi-stt"
BJ = config.BJ_ID
SECONDS = int(os.environ.get("HOGI_STT_SECONDS", "20"))
MODEL = os.environ.get("HOGI_STT_MODEL", "openai/gpt-audio-mini")
MONTHLY_CAP = float(os.environ.get("HOGI_STT_CAP_USD", "30"))

PROMPT = ("한국어 인터넷 방송 오디오다. 방송인이 실제로 말한 문장만 한국어로 그대로 받아써라. "
          "노래 가사, 배경음악, 효과음, 시청자 TTS, 알림음은 옮기지 마라. 요약·설명·번역 금지. "
          "말소리가 없거나 확신이 없으면 정확히 NONE 한 단어만 출력해라.")


def log(msg):
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + msg + "\n")


def env(name):
    v = os.environ.get(name)
    if v:
        return v
    try:
        with open(config.ENV_PATH, encoding="utf-8") as f:
            for line in f:
                if line.startswith(name + "="):
                    return line.split("=", 1)[1].strip().strip('"').strip("'")
    except FileNotFoundError:
        pass
    return ""


OPENAI_STT = os.environ.get("HOGI_OPENAI_STT", "gpt-4o-transcribe")
STT_HINT = "후로기 인터넷 방송. 한국어 말소리만 받아쓴다. 노래 가사, 배경음악, 효과음은 옮기지 않는다."


def transcribe_openai(wav):
    """OpenAI 전용 전사 모델 직결(운영자가 크레딧 충전, 2026-10-24). 실패하면 None."""
    k = env("OPENAI_API_KEY")
    if not k:
        return None
    boundary = "----hogi" + str(int(time.time() * 1000))
    parts = []
    for name, val in (("model", OPENAI_STT), ("language", "ko"), ("response_format", "json"),
                      ("temperature", "0"), ("prompt", STT_HINT)):
        parts.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n{val}\r\n".encode())
    parts.append((f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"a.wav\"\r\n"
                  "Content-Type: audio/wav\r\n\r\n").encode() + open(wav, "rb").read() + b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode())
    req = urllib.request.Request("https://api.openai.com/v1/audio/transcriptions", data=b"".join(parts),
                                 headers={"Authorization": f"Bearer {k}",
                                          "Content-Type": f"multipart/form-data; boundary={boundary}"})
    try:
        d = json.loads(urllib.request.urlopen(req, timeout=120).read())
    except Exception as e:
        log(f"[OpenAI 전사 실패] {type(e).__name__}: {str(e)[:120]}")
        return None
    text = (d.get("text") or "").strip()
    secs = max(1.0, (os.path.getsize(wav) - 44) / 32000.0)
    cost = round(secs / 60.0 * 0.006, 6)  # gpt-4o-transcribe $0.006/분
    with open(USAGE, "a", encoding="utf-8") as f:
        f.write(json.dumps({"ts": int(time.time()), "ym": time.strftime("%Y-%m"), "kind": "stt",
                            "model": OPENAI_STT, "cost": cost}) + "\n")
    log(f"[원문 openai] {text[:200]!r}")
    return clean(text), cost


def key():
    v = os.environ.get("OPENROUTER_API_KEY")
    if v:
        return v
    with open(config.ENV_PATH, encoding="utf-8") as f:
        for line in f:
            if line.startswith("OPENROUTER_API_KEY="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    raise RuntimeError("OPENROUTER_API_KEY 없음")


def month_spent():
    total, ym = 0.0, time.strftime("%Y-%m")
    try:
        with open(USAGE, encoding="utf-8") as f:
            for line in f:
                try:
                    d = json.loads(line)
                except Exception:
                    continue
                if d.get("ym") == ym:
                    total += float(d.get("cost") or 0)
    except FileNotFoundError:
        pass
    return total


def grab():
    """방송 오디오 SECONDS초를 16k mono wav로 받는다. (wav, 오디오프레임수) 또는 (None, 사유)."""
    if os.path.isdir(WORK):
        shutil.rmtree(WORK, ignore_errors=True)
    os.makedirs(os.path.dirname(WORK), exist_ok=True)
    p = subprocess.run([sys.executable, REC, f"https://play.sooplive.com/{BJ}", "--output", WORK,
                        "--seconds", str(SECONDS), "--min-height", "360", "--segment-seconds", "4",
                        "--cookies", COOKIES], capture_output=True, text=True, timeout=SECONDS + 120)
    m = re.findall(r'"media_counts": \{[^}]*"audio": (\d+)', p.stdout or "")
    audio = int(m[-1]) if m else 0
    parts = sorted(glob.glob(os.path.join(WORK, "part-*", "index.m3u8")))
    if not parts or audio == 0:
        tail = (p.stdout or p.stderr or "")[-160:].replace("\n", " ")
        return None, f"수신 실패 audio={audio} {tail}"
    wav = WORK + ".wav"
    args = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y"]
    for part in parts:
        args += ["-i", part]
    if len(parts) > 1:
        args += ["-filter_complex", f"concat=n={len(parts)}:v=0:a=1[a]", "-map", "[a]"]
    else:
        args += ["-vn"]
    # 방송 음량이 낮아(실측 평균 -36.7dB) 모델이 말소리를 놓친다. 저역 컷 + 동적 정규화로 키운다.
    args += ["-af", "highpass=f=80,dynaudnorm=f=150:g=15", "-ac", "1", "-ar", "16000", wav]
    r = subprocess.run(args, capture_output=True, text=True, timeout=120)
    if r.returncode != 0 or not os.path.exists(wav) or os.path.getsize(wav) < 32000:
        return None, "오디오 추출 실패 " + (r.stderr or "")[-160:]
    return wav, audio


def transcribe(wav):
    b64 = base64.b64encode(open(wav, "rb").read()).decode()
    body = json.dumps({
        "model": MODEL,
        "modalities": ["text"],
        "temperature": 0,
        "max_tokens": 300,
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": PROMPT},
            {"type": "input_audio", "input_audio": {"data": b64, "format": "wav"}},
        ]}],
    }).encode()
    req = urllib.request.Request("https://openrouter.ai/api/v1/chat/completions", data=body,
                                 headers={"Authorization": f"Bearer {key()}",
                                          "Content-Type": "application/json"})
    d = json.loads(urllib.request.urlopen(req, timeout=120).read())
    text = (d["choices"][0]["message"].get("content") or "").strip()
    cost = float((d.get("usage") or {}).get("cost") or 0)
    with open(USAGE, "a", encoding="utf-8") as f:
        f.write(json.dumps({"ts": int(time.time()), "ym": time.strftime("%Y-%m"), "kind": "stt",
                            "model": MODEL, "cost": cost}) + "\n")
    log(f"[원문] {text[:200]!r}")
    return clean(text), cost


def scene_on_cue(text):
    """전사에 '이거 봐봐' 류가 있을 때만, 방금 받은 영상 마지막 프레임을 한 줄로 설명한다."""
    if not text or not SHOW_CUE.search(text):
        return None
    segs = sorted(glob.glob(os.path.join(WORK, "part-*", "segment-*.ts")))
    if not segs:
        return None
    jpg = WORK + ".jpg"
    r = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-sseof", "-1",
                        "-i", segs[-1], "-frames:v", "1", "-vf", "scale=960:-2", jpg],
                       capture_output=True, text=True, timeout=60)
    if r.returncode != 0 or not os.path.exists(jpg):
        return None
    b64 = base64.b64encode(open(jpg, "rb").read()).decode()
    body = json.dumps({
        "model": VISION_MODEL, "temperature": 0, "max_tokens": 80,
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": ("방송인이 방금 '" + text[:80] + "'라고 말하며 화면의 무언가를 보여줬다. "
                                      "방송인이 보여주는 대상이 무엇인지만 한국어 한 줄 30자 이내로. "
                                      "배경·색·의상·아바타 묘사는 쓰지 마라.")},
            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}}]}],
    }).encode()
    req = urllib.request.Request("https://openrouter.ai/api/v1/chat/completions", data=body,
                                 headers={"Authorization": f"Bearer {key()}",
                                          "Content-Type": "application/json"})
    d = json.loads(urllib.request.urlopen(req, timeout=60).read())
    desc = (d["choices"][0]["message"].get("content") or "").strip()
    cost = float((d.get("usage") or {}).get("cost") or 0)
    with open(USAGE, "a", encoding="utf-8") as f:
        f.write(json.dumps({"ts": int(time.time()), "ym": time.strftime("%Y-%m"), "kind": "vision",
                            "model": VISION_MODEL, "cost": cost}) + "\n")
    if desc:
        with open(SCENE, "a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": int(time.time()), "text": desc, "cue": text[:80], "cost": cost},
                               ensure_ascii=False) + "\n")
        log(f"[화면 예외] {desc!r} (cue={text[:40]!r})")
    return desc


def clean(text):
    t = (text or "").strip().strip('"').strip()
    if not t or t.upper().startswith("NONE"):
        return ""
    t = re.sub(r"\[[^\]]*\]|\([^)]*음악[^)]*\)", "", t).strip()
    hangul = len(re.findall(r"[가-힣]", t))
    if hangul < 3 or hangul / max(1, len(t.replace(" ", ""))) < 0.5:
        return ""
    return t


def once():
    if month_spent() >= MONTHLY_CAP:
        log(f"[예산 초과] 이번 달 ${month_spent():.2f} ≥ ${MONTHLY_CAP}")
        return None
    t0 = time.time()
    wav, info = grab()
    if not wav:
        log(f"[수신] {info}")
        return None
    r = transcribe_openai(wav)
    if r is None:
        text, cost = transcribe(wav)  # 폴백: OpenRouter GPT 오디오
    else:
        text, cost = r
    rec = {"ts": int(t0), "end": int(time.time()), "audio_frames": info, "text": text, "cost": cost}
    with open(OUT, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    log(f"[전사] {text!r} frames={info} ${cost:.5f} {time.time() - t0:.1f}s")
    try:
        scene_on_cue(text)
    except Exception as e:
        log(f"[화면 예외 실패] {type(e).__name__}: {e}")
    return rec


def main():
    if "--once" in sys.argv:
        print(json.dumps(once(), ensure_ascii=False))
        return
    if "--diag" in sys.argv:
        global PROMPT
        PROMPT = ("이 한국어 방송 오디오에 무엇이 들리는지 먼저 한 줄로 분류해라(말소리/음악/게임소리/무음 비율). "
                  "그 다음 줄부터 들리는 말소리를 들리는 그대로 전부 받아써라. 작게 들려도 최대한 받아써라.")
        path = sys.argv[sys.argv.index("--diag") + 1]
        text, cost = transcribe(path)
        print(text)
        return
    if "--wav" in sys.argv:
        # 같은 wav를 다른 모델로 다시 전사해 비교한다: --wav <path> [--model <id>]
        global MODEL
        path = sys.argv[sys.argv.index("--wav") + 1]
        if "--model" in sys.argv:
            MODEL = sys.argv[sys.argv.index("--model") + 1]
        text, cost = transcribe(path)
        print(json.dumps({"model": MODEL, "text": text, "cost": cost}, ensure_ascii=False))
        return
    log("=== stt loop 시작 ===")
    while True:
        try:
            if once() is None:
                time.sleep(30)
        except Exception as e:
            log(f"[오류] {type(e).__name__}: {e}")
            time.sleep(15)


if __name__ == "__main__":
    main()
