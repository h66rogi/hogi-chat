#!/usr/bin/env python3
"""전사 모델 비교: 같은 방송 오디오를 여러 모델에 넣고, 소리를 듣는 심사 모델이 모델명을 가린 채 채점한다.

사용(서버): python3 stt_compare.py [초=90]
결과: /tmp/stt-compare/result.json, 표준출력 요약. 방송·Discord에는 아무것도 보내지 않는다.
"""

import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))))
import config  # noqa: E402  (repo root)
import base64
import glob
import json
import os
import random
import subprocess
import sys
import threading
import time

import websocket

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)
import stt_oci as base  # noqa: E402

OUT = "/tmp/stt-compare"
SECS = int(sys.argv[1]) if len(sys.argv) > 1 else 90
FILTER = "highpass=f=80,speechnorm=e=6:r=0.0001:l=1"  # 실시간 전사와 같은 전처리
HINT = "후로기 인터넷 방송. 한국어 말소리만 받아쓴다. 노래 가사, 배경음악, 효과음은 옮기지 않는다."
JUDGE = os.environ.get("HOGI_STT_JUDGE", "google/gemini-4-argon")


def capture():
    """실시간 전사 서비스가 받고 있는 2초 조각을 그대로 복사해 모은다.

    같은 서버에서 SOOP 수신 세션을 하나 더 열면 IncompleteReadError로 거절된다(실측).
    """
    import shutil
    live_dir = "/tmp/hogi-stt-live"
    subprocess.run(["rm", "-rf", OUT])
    os.makedirs(OUT + "/segs")
    got, t_end = [], time.time() + SECS
    while time.time() < t_end:
        parts = sorted(glob.glob(live_dir + "/part-*"))
        for i, part in enumerate(parts):
            segs = sorted(glob.glob(part + "/segment-*.ts"))
            for sg in (segs if i < len(parts) - 1 else segs[:-1]):
                key = os.path.basename(part) + "/" + os.path.basename(sg)
                if key in got:
                    continue
                try:
                    shutil.copy(sg, f"{OUT}/segs/{len(got):05d}.ts")
                    got.append(key)
                except FileNotFoundError:
                    pass
        time.sleep(0.4)
    files = sorted(glob.glob(OUT + "/segs/*.ts"))
    if not files:
        raise RuntimeError("실시간 전사 서비스 조각을 못 받음")
    lst = OUT + "/list.txt"
    with open(lst, "w") as f:
        f.writelines(f"file '{x}'\n" for x in files)
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "concat", "-safe", "0", "-i", lst,
                    "-vn", "-af", FILTER, "-ac", "1", "-ar", "16000", OUT + "/a16.wav"], check=True)
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", OUT + "/a16.wav",
                    "-ac", "1", "-ar", "24000", "-f", "s16le", OUT + "/a24.pcm"], check=True)
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", OUT + "/a16.wav",
                    "-b:a", "64k", OUT + "/a.mp3"], check=True)
    dur = os.path.getsize(OUT + "/a16.wav") / 32000
    print(f"녹음 {dur:.1f}초 (조각 {len(files)}개)", flush=True)
    return dur


def openai_file(model):
    import urllib.request
    k = base.env("OPENAI_API_KEY")
    bd = "----cmp" + str(time.time_ns())
    parts = []
    fields = [("model", model), ("language", "ko"), ("response_format", "json"), ("prompt", HINT)]
    if "gpt-transcribe" == model:
        fields = [("model", model), ("response_format", "json"), ("prompt", HINT)]
    for n, v in fields:
        parts.append(f"--{bd}\r\nContent-Disposition: form-data; name=\"{n}\"\r\n\r\n{v}\r\n".encode())
    parts.append((f"--{bd}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"a.wav\"\r\n"
                  "Content-Type: audio/wav\r\n\r\n").encode() + open(OUT + "/a16.wav", "rb").read() + b"\r\n")
    parts.append(f"--{bd}--\r\n".encode())
    req = urllib.request.Request("https://api.openai.com/v1/audio/transcriptions", data=b"".join(parts),
                                 headers={"Authorization": f"Bearer {k}",
                                          "Content-Type": f"multipart/form-data; boundary={bd}"})
    return json.loads(urllib.request.urlopen(req, timeout=180).read()).get("text", "")


def openrouter_audio(model):
    base.MODEL = model
    text, cost = base.transcribe(OUT + "/a16.wav")
    return text


def live(model, delay):
    """실시간 세션에 같은 소리를 실제 속도로 흘려 넣고(서비스와 같은 VAD) 문장들을 모은다."""
    import stt_live as L
    got, lock = {}, threading.Lock()
    order = []

    def capture_write(text, span):
        with lock:
            got[span[0]] = text

    L.write = capture_write
    L.MODEL, L.DELAY = model, delay
    tr = L.Transcriber()
    if not tr.connect():
        return "(세션 실패)"
    vad = L.Vad(tr)
    pcm = open(OUT + "/a24.pcm", "rb").read()
    step = 24000 * 2 // 5  # 0.2초씩
    t0 = time.time()
    for i in range(0, len(pcm), step):
        vad.feed(pcm[i:i + step], t0 + (i + step) / 48000)
        time.sleep(0.2)
    if vad.in_turn:
        tr.commit(vad.turn_start, time.time(), vad.turn_secs)
    time.sleep(8)
    tr.close()
    return " ".join(got[k] for k in sorted(got))


def judge(cands):
    import urllib.request
    keys = list(cands)
    random.shuffle(keys)
    labels = {chr(65 + i): k for i, k in enumerate(keys)}
    listing = "\n\n".join(f"[{lab}]\n{cands[k]}" for lab, k in labels.items())
    prompt = ("이 오디오는 한국어 인터넷 방송 일부다. 방송인의 말소리를 기준으로 아래 받아쓰기들의 정확도를 채점하라. "
              "노래 가사·배경음악은 받아쓰지 않아야 맞다(가사를 적었으면 오류로 센다).\n"
              "각 후보마다: 틀린 단어 수, 빠뜨린 말 수, 없는 말을 지어낸 수, 가사 혼입 여부, 100점 만점 점수, 한 줄 평.\n"
              "그리고 네가 들은 말소리를 최대한 정확히 받아쓴 참고 원고도 써라.\n\n" + listing +
              '\n\nJSON만 출력: {"reference": "...", "scores": {"A": {"wrong": 0, "missing": 0, "invented": 0, '
              '"lyrics": false, "score": 0, "note": "..."}, ...}}')
    b64 = base64.b64encode(open(OUT + "/a.mp3", "rb").read()).decode()
    body = json.dumps({"model": JUDGE, "temperature": 0, "max_tokens": 6000,
                       "messages": [{"role": "user", "content": [
                           {"type": "text", "text": prompt},
                           {"type": "input_audio", "input_audio": {"data": b64, "format": "mp3"}}]}]}).encode()
    req = urllib.request.Request("https://openrouter.ai/api/v1/chat/completions", data=body,
                                 headers={"Authorization": f"Bearer {base.key()}", "Content-Type": "application/json"})
    d = json.loads(urllib.request.urlopen(req, timeout=300).read())
    txt = d["choices"][0]["message"].get("content") or ""
    import re
    v = json.loads(re.search(r"\{.*\}", txt, re.S).group(0))
    return {"labels": labels, "reference": v.get("reference"),
            "scores": {labels[k]: s for k, s in (v.get("scores") or {}).items() if k in labels}}


def main():
    dur = capture()
    jobs = {
        "실시간 gpt-live-transcribe (medium, 현재)": lambda: live("gpt-live-transcribe", "medium"),
        "실시간 gpt-live-transcribe (high)": lambda: live("gpt-live-transcribe", "high"),
        "파일 gpt-4o-transcribe": lambda: openai_file("gpt-4o-transcribe"),
        "파일 gpt-transcribe": lambda: openai_file("gpt-transcribe"),
        "OpenRouter gpt-audio-mini": lambda: openrouter_audio("openai/gpt-audio-mini"),
        "OpenRouter gpt-audio": lambda: openrouter_audio("openai/gpt-audio"),
        "OpenRouter gemini-3.8-flash": lambda: openrouter_audio("google/gemini-3.8-flash"),
    }
    res = {}

    def run(name, fn):
        t = time.time()
        try:
            res[name] = {"text": base.clean(fn()) or "(빈 결과)", "sec": round(time.time() - t, 1)}
        except Exception as e:
            res[name] = {"text": f"(실패 {type(e).__name__}: {str(e)[:100]})", "sec": round(time.time() - t, 1)}
        print("완료", name, res[name]["sec"], flush=True)

    # 실시간 두 개와 OpenRouter 세 개는 모듈 전역(MODEL·write)을 바꾸므로 각 묶음 안에서는 차례로 돈다.
    groups = [[n for n in jobs if n.startswith("실시간")], [n for n in jobs if n.startswith("OpenRouter")]]
    groups += [[n] for n in jobs if n.startswith("파일")]

    def run_group(names):
        for n in names:
            run(n, jobs[n])

    ths = [threading.Thread(target=run_group, args=(g,)) for g in groups]
    for t in ths:
        t.start()
    for t in ths:
        t.join()
    j = judge({n: r["text"] for n, r in res.items()})
    out = {"duration": dur, "results": res, "judge_model": JUDGE, **j}
    json.dump(out, open(OUT + "/result.json", "w"), ensure_ascii=False, indent=1)
    print("\n참고 원고(심사 모델):", j["reference"])
    for n in sorted(res, key=lambda n: -(j["scores"].get(n, {}).get("score") or 0)):
        s = j["scores"].get(n, {})
        print(f"\n## {n} | {s.get('score')}점 | 틀림 {s.get('wrong')} 빠짐 {s.get('missing')} 지어냄 {s.get('invented')} "
              f"가사 {s.get('lyrics')} | {s.get('note')}\n{res[n]['text']}")


if __name__ == "__main__":
    main()
