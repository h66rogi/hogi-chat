#!/usr/bin/env python3
import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
import config  # noqa: E402  (repo root)
"""다시보기(VOD)나 녹화본으로 컨텍스트 주입을 리플레이 테스트한다.

미디어(로컬 HLS 디렉터리·mp4·ts, 또는 라이브 캡처)에서
- 일정 간격으로 프레임을 뽑아 화면 설명
- 일정 구간으로 오디오를 잘라 전사
을 만들어 compose.py 에 그대로 넣고, 생성된 후보와 "왜 그 문장이 나왔는지" 근거 구조를 저장한다.
실제 후보 채널에는 아무것도 보내지 않는다.

사용:
  python3 replay.py --media /tmp/nat3 --chat recent.jsonl --count 3
  python3 replay.py --capture danz59 --seconds 60 --count 2
  python3 replay.py --media /path/vod.mp4 --window 120 --count 5
"""
import argparse
import glob
import json
import os
import subprocess
import sys
import time

BASE = os.path.join(os.path.expanduser("~"), "hogi-chat")
sys.path.insert(0, BASE)
os.environ.setdefault("HOGI_BASE", BASE)

import compose  # noqa: E402
import perceive  # noqa: E402

FFMPEG = "/opt/homebrew/bin/ffmpeg"
FFPROBE = "/opt/homebrew/bin/ffprobe"


def log(msg):
    print(msg, flush=True)


def duration(media):
    r = subprocess.run([FFPROBE, "-v", "error", "-show_entries", "format=duration",
                        "-of", "csv=p=0", media], capture_output=True, text=True, timeout=60)
    try:
        return float(r.stdout.strip())
    except ValueError:
        pass
    # HLS 재생목록이면 세그먼트 합으로 계산
    total = 0.0
    for line in open(media, encoding="utf-8", errors="ignore"):
        line = line.strip()
        if line.startswith("#EXTINF:"):
            try:
                total += float(line.split(":", 1)[1].split(",")[0])
            except ValueError:
                pass
    return total


def pick_inputs(media):
    """HLS 디렉터리면 파트별 index.m3u8, 파일이면 그 파일 하나."""
    if os.path.isdir(media):
        parts = sorted(glob.glob(os.path.join(media, "part-*", "index.m3u8")))
        if parts:
            return parts
        idx = os.path.join(media, "index.m3u8")
        return [idx] if os.path.exists(idx) else []
    return [media]


def extract_frames(src, outdir, every=20, limit=12):
    os.makedirs(outdir, exist_ok=True)
    dur = duration(src)
    # 짧은 클립이면 전체 구간에 limit 장을 고르게 뽑는다
    fps = max(1.0 / max(1, every), (limit / dur) if dur > 0 else 0)
    pattern = os.path.join(outdir, "f%03d.jpg")
    cmd = [FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-i", src,
           "-vf", f"fps={fps},scale=640:-1", "-frames:v", str(limit), pattern]
    subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    return sorted(glob.glob(os.path.join(outdir, "f*.jpg")))


def extract_audio_chunks(src, outdir, chunk=20, limit=8):
    os.makedirs(outdir, exist_ok=True)
    pattern = os.path.join(outdir, "a%03d.wav")
    cmd = [FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-i", src,
           "-vn", "-ac", "1", "-ar", "16000", "-f", "segment",
           "-segment_time", str(chunk), "-reset_timestamps", "1", pattern]
    subprocess.run(cmd, capture_output=True, text=True, timeout=900)
    files = sorted(glob.glob(os.path.join(outdir, "a*.wav")))
    return files[:limit]


def capture_live(bjid, seconds, workdir):
    cookie = perceive.ensure_cookies()
    cmd = [perceive.NATIVE_PY, perceive.NATIVE_SCRIPT, f"https://play.sooplive.com/{bjid}",
           "--output", workdir, "--seconds", str(seconds), "--min-height", "360",
           "--segment-seconds", "4"]
    if cookie:
        cmd += ["--cookies", cookie]
    log(f"[캡처] {bjid} {seconds}초 …")
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=seconds + 300)
    # 수신이 끊겨 짧게 끝나면 목표 길이까지 한 번 더 붙인다
    have = sum(duration(os.path.join(workdir, d, "index.m3u8"))
               for d in os.listdir(workdir) if os.path.isdir(os.path.join(workdir, d)))
    if 0 < have < seconds * 0.7:
        log(f"[캡처] {have:.1f}s만 받음 → 한 번 더")
        subprocess.run(cmd, capture_output=True, text=True, timeout=seconds + 300)
    parts = glob.glob(os.path.join(workdir, "part-*", "index.m3u8"))
    if not parts:
        tail = (r.stderr or r.stdout or "")[-300:].replace("\n", " ")
        log(f"[캡처 실패] {tail}")
    return workdir


def build_perception(media, workdir, every=20, chunk=20, max_frames=6, max_chunks=4):
    parts = pick_inputs(media)
    if not parts:
        raise SystemExit(f"미디어를 찾지 못함: {media}")
    src = parts[0]
    log(f"[미디어] {src} (파트 {len(parts)}개, 길이 {duration(src):.1f}s)")
    scene, speech = [], []
    frames = extract_frames(src, os.path.join(workdir, "frames"), every, max_frames)
    for i, f in enumerate(frames):
        try:
            text, cost = perceive.describe_image(f)
        except Exception as e:
            log(f"  화면 실패 {f}: {e}")
            continue
        scene.append({"ts": int(time.time()) - (len(frames) - i) * every, "text": text, "cost": cost})
        log(f"  화면 +{i*every}s: {text} (${cost:.5f})")
    audios = extract_audio_chunks(src, os.path.join(workdir, "audio"), chunk, max_chunks)
    for i, a in enumerate(audios):
        try:
            text, cost = perceive.transcribe(a)
        except Exception as e:
            log(f"  음성 실패 {a}: {e}")
            continue
        if not text:
            text = "(무음/말소리 없음)"
        speech.append({"ts": int(time.time()) - (len(audios) - i) * chunk, "text": text, "cost": cost})
        log(f"  음성 +{i*chunk}s: {text} (${cost:.5f})")
    return scene, speech


def load_chat(path, limit=24):
    rows = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except Exception:
                continue
            if "nick" in d and "text" in d:
                rows.append(d)
    return rows[-limit:]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--media", help="HLS 디렉터리·mp4·ts 파일")
    ap.add_argument("--capture", help="라이브 캡처할 BJ id (예: danz59)")
    ap.add_argument("--seconds", type=int, default=60, help="--capture 길이")
    ap.add_argument("--chat", default=os.path.join(BASE, "recent.jsonl"))
    ap.add_argument("--count", type=int, default=3, help="생성할 후보 수")
    ap.add_argument("--every", type=int, default=20, help="프레임 간격(초)")
    ap.add_argument("--chunk", type=int, default=20, help="오디오 구간(초)")
    ap.add_argument("--max-frames", type=int, default=6)
    ap.add_argument("--max-chunks", type=int, default=4)
    ap.add_argument("--out", help="결과 디렉터리")
    args = ap.parse_args()

    stamp = time.strftime("%Y%m%d-%H%M%S")
    out = args.out or os.path.join(BASE, "replay", stamp)
    os.makedirs(out, exist_ok=True)
    log(f"[출력] {out}")

    media = args.media
    if args.capture:
        media = os.path.join(out, f"capture-{stamp}")   # soop_native 는 새 디렉터리여야 한다
        capture_live(args.capture, args.seconds, media)

    scene, speech = build_perception(media, out, args.every, args.chunk,
                                     args.max_frames, args.max_chunks)
    perc = {"scene": scene, "speech": speech}

    chats = load_chat(args.chat)
    log(f"[채팅] {len(chats)}줄 사용")

    results = []
    original_prior = compose.prior_candidates

    def prior_with_replay(minutes=25, limit=12):
        mine = [r["line"] for r in results if r.get("line")]
        return (mine + original_prior(minutes, limit))[-limit:]

    compose.prior_candidates = prior_with_replay
    for i in range(args.count):
        line, prompt, why, ctx = compose.generate(chats, perc=perc)
        rec = {"i": i + 1, "ts": int(time.time()), "line": line, "why": why,
               "prompt": prompt, "context": ctx}
        results.append(rec)
        (log(f"[후보 {i+1}] {line!r} ({why})") if line else log(f"[후보 {i+1}] 실패: {why}"))

    with open(os.path.join(out, "results.jsonl"), "w", encoding="utf-8") as f:
        for r in results:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    with open(os.path.join(out, "perception.json"), "w", encoding="utf-8") as f:
        json.dump(perc, f, ensure_ascii=False, indent=2)
    ok = [r for r in results if r["line"]]
    log("")
    log(f"완료: 후보 {len(ok)}/{len(results)} 생성 · 근거 포함 저장 {out}/results.jsonl")
    for r in ok:
        log(f"  - {r['line']}")


if __name__ == "__main__":
    main()
