#!/usr/bin/env python3
"""후로기(SOOP h66rogi) 라이브 채팅 → AI 판단 → Discord #hogi_chat 실시간 중계.

- SOOP 채팅 WebSocket 프로토콜로 익명 수신 (읽기 전용, 채팅 전송 안 함)
- 6초 배치로 AI 판단: 시청자에게 알릴 가치가 있는 메시지만 선별
- state.json 의 enabled 로 켜고 끈다. enabled=false 면 수집만 하고 전송하지 않는다.
- dry_run=true 면 판단 결과만 로그에 남기고 Discord 로 보내지 않는다.
"""

import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
import config  # noqa: E402  (repo root)
import json
import os
import re
import sys
import threading
import time
import urllib.parse
import urllib.request

import websocket

HOME = os.path.expanduser("~")
BASE = config.BASE
STATE_PATH = os.path.join(BASE, "state.json")
RECENT_PATH = os.path.join(BASE, "recent.jsonl")
TITLE_PATH = os.path.join(BASE, "title.txt")
LOG_PATH = os.path.join(BASE, "relay.log")
ENV_PATH = config.ENV_PATH

BJ_ID = config.BJ_ID
BATCH_SECONDS = 6
MAX_BATCH = 40

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36")

ESC, TAB, F = 0x1B, 0x09, "\f"
HDR_LEN = 14

SVC_CHAT = 5
FLAG_BJ = 4
FLAG_MANAGER = 256


def log(msg):
    line = time.strftime("%Y-%m-%d %H:%M:%S") + " " + msg
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def load_env():
    env = {}
    if os.path.exists(ENV_PATH):
        for raw in open(ENV_PATH, encoding="utf-8"):
            raw = raw.strip()
            if not raw or raw.startswith("#") or "=" not in raw:
                continue
            k, v = raw.split("=", 1)
            env[k.strip()] = v.strip().strip('"').strip("'")
    return env


ENV = load_env()
CANDIDATES_PATH = os.path.join(BASE, "candidates.jsonl")


def reaction_watch():
    """✅/❌ 리액션을 5초마다 확인해 즉시 처리한다."""
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    while True:
        time.sleep(5)
        try:
            import reaction as reaction_mod
            reaction_mod.process()
        except Exception as e:
            log(f"[리액션 오류] {e}")


def load_state():
    try:
        return json.load(open(STATE_PATH, encoding="utf-8"))
    except Exception:
        return {"enabled": False, "dry_run": True, "channel_id": ""}


# ── SOOP 프로토콜 ─────────────────────────────────────────────
def build_packet(cmd, body=""):
    b = body.encode("utf-8") if isinstance(body, str) else body
    return f"\x1b\t{cmd:04d}{len(b):06d}00".encode("utf-8") + b


def parse_packets(raw):
    if isinstance(raw, str):
        raw = raw.encode("utf-8", errors="replace")
    out, pos = [], 0
    while pos + HDR_LEN <= len(raw):
        if raw[pos] != ESC or raw[pos + 1] != TAB:
            pos += 1
            continue
        try:
            cmd = int(raw[pos + 2:pos + 6])
            blen = int(raw[pos + 6:pos + 12])
        except (ValueError, IndexError):
            pos += 1
            continue
        body = raw[pos + HDR_LEN:pos + HDR_LEN + blen].decode("utf-8", errors="replace")
        if body.startswith(F):
            body = body[1:]
        if body.endswith(F):
            body = body[:-1]
        out.append((cmd, body.split(F) if body else []))
        pos += HDR_LEN + blen
    return out


def channel_info(bj_id):
    url = f"https://live.sooplive.co.kr/afreeca/player_live_api.php?bjid={bj_id}"
    data = urllib.parse.urlencode({"bid": bj_id, "bno": "0", "type": "live",
                                   "player_type": "html5"}).encode()
    req = urllib.request.Request(url, data=data, headers={
        "User-Agent": UA, "Referer": f"https://play.sooplive.com/{bj_id}"})
    ch = (json.loads(urllib.request.urlopen(req, timeout=20).read()).get("CHANNEL") or {})
    return {
        "chatno": ch.get("CHATNO") or "",
        "domain": ch.get("CHDOMAIN") or "",
        "port": int(ch.get("CHPT") or 0),
        "ftk": ch.get("FTK") or "",
        "title": ch.get("TITLE") or "",
    }


# ── 판단 ─────────────────────────────────────────────────────
JUNK = re.compile(r"^(ㅎ+ㅇ*|ㅇㅇ|ㄱㅅ|ㅋㅋ+|ㅎㅎ+|ㅠㅠ+|ㅜㅜ+|\.+|!+|\?+|❤+|👍+)$")
URLRE = re.compile(r"(https?://|www\.|\.com|\.kr|\.tv)")
REPEAT = re.compile(r"(.)\1{6,}")
AD = re.compile(r"(광고|홍보|구매|할인|쿠폰|입장코드|텔레그램|카톡|오픈채팅|수익|재테크|부업)")


def rule_filter(text):
    t = text.strip()
    if len(t) < 2:
        return False
    if JUNK.match(t):
        return False
    if REPEAT.search(t):
        return False
    if URLRE.search(t) and AD.search(t):
        return False
    return True


def ai_filter(msgs):
    """msgs: [{'nick','text'}]. 반환: 유지할 index 리스트."""
    key = ENV.get("OPENROUTER_API_KEY")
    if not key or not msgs:
        return list(range(len(msgs)))
    numbered = "\n".join(f"{i+1}. {m['nick']}: {m['text']}" for i, m in enumerate(msgs))
    prompt = (
        "아래는 라이브 방송 채팅이다. 시청자에게 알려줄 가치가 있는 것만 고른다.\n"
        "포함: 방송 내용에 대한 반응, 질문, 정보, 상황에 맞는 드립.\n"
        "제외: 단순 인사·초성·이모티콘만, 도배, 스팸·광고, 의미 없는 반복, 맥락 없는 욕설.\n"
        "JSON만 출력: {\"keep\":[번호,...]}\n\n" + numbered
    )
    body = json.dumps({
        "model": "deepseek/deepseek-v4.1-flash-1017",
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0,
        "max_tokens": 1200,
    }).encode()
    req = urllib.request.Request("https://openrouter.ai/api/v1/chat/completions", data=body,
                                 headers={"Authorization": f"Bearer {key}",
                                          "Content-Type": "application/json"})
    try:
        resp = json.loads(urllib.request.urlopen(req, timeout=60).read())
        msg = resp["choices"][0]["message"]
        text = (msg.get("content") or "") + "\n" + (msg.get("reasoning") or "")
        found = re.findall(r'"keep"\s*:\s*\[([0-9,\s]*)\]', text)
        if not found:
            raise ValueError("keep 배열 없음")
        keep = json.loads("[" + found[-1] + "]")
        return [int(k) - 1 for k in keep if 1 <= int(k) <= len(msgs)]
    except Exception as e:
        log(f"[AI 실패] {e} — 규칙 필터로 대체")
        return list(range(len(msgs)))


def discord_send(channel_id, text):
    token = ENV.get("DISCORD_BOT_TOKEN")
    if not token:
        log("[전송 실패] DISCORD_BOT_TOKEN 없음")
        return False
    body = json.dumps({"content": text[:1900], "allowed_mentions": {"parse": []}}).encode()
    req = urllib.request.Request(f"https://discord.com/api/v10/channels/{channel_id}/messages",
                                 data=body, headers={"Authorization": f"Bot {token}",
                                                     "Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=20)
        return True
    except Exception as e:
        log(f"[전송 실패] {e}")
        return False


# ── 배치 처리 ─────────────────────────────────────────────────
BUFFER = []
LOCK = threading.Lock()


def flush():
    with LOCK:
        batch = BUFFER[:MAX_BATCH]
        del BUFFER[:len(batch)]
    if not batch:
        return
    st = load_state()
    passed = [m for m in batch if rule_filter(m["text"])]
    if not passed:
        return
    keep_idx = ai_filter(passed)
    selected = [passed[i] for i in keep_idx if i < len(passed)]
    if not selected:
        return
    if not st.get("enabled"):
        log(f"[꺼짐] {len(selected)}건 판단됨, 전송 안 함 · 예: " +
            " / ".join(m["text"][:40] for m in selected[:3]))
        return
    if st.get("dry_run"):
        for m in selected:
            log(f"[DRY] {m['nick']}: {m['text'][:120]}")
        return
    ch = st.get("channel_id")
    if not ch:
        log("[전송 실패] channel_id 없음")
        return
    for m in selected:
        ok = discord_send(ch, f"**{m['nick']}**: {m['text']}"[:1900])
        log(("[전송] " if ok else "[실패] ") + f"{m['nick']}: {m['text'][:100]}")
        time.sleep(1.1)


def flusher():
    while True:
        time.sleep(BATCH_SECONDS)
        try:
            flush()
        except Exception as e:
            log(f"[flush 오류] {e}")


# ── 수집 루프 ─────────────────────────────────────────────────
def session():
    info = channel_info(BJ_ID)
    if not info["chatno"]:
        return False
    try:
        with open(TITLE_PATH, "w", encoding="utf-8") as f:
            f.write(info["title"] or "")
    except Exception:
        pass
    url = f"wss://{info['domain']}:{info['port'] + 1}/Websocket/{BJ_ID}"
    log(f"[연결] {info['title'] or '제목없음'} · {url}")
    stop = threading.Event()

    def on_open(ws):
        ws.send(build_packet(1, f"{F}{F}{F}16{F}"), opcode=2)

    def on_message(ws, message):
        raw = message if isinstance(message, bytes) else message.encode("utf-8", errors="replace")
        for cmd, fields in parse_packets(raw):
            if cmd == 1:
                log(f"[조인 응답] {str(fields)[:80]}")
                ws.send(build_packet(2, f"{F}{info['chatno']}{F}{info['ftk']}{F}0{F}{F}{F}"), opcode=2)
            elif cmd == SVC_CHAT:
                text = (fields[0] if fields else "").replace("\r", "").strip()
                nick = fields[5] if len(fields) > 5 else (fields[1] if len(fields) > 1 else "?")
                if text:
                    if os.environ.get("HOGI_LEGACY_FORWARD") == "1":
                        with LOCK:
                            BUFFER.append({"nick": nick, "text": text})
                    try:
                        with open(RECENT_PATH, "a", encoding="utf-8") as f:
                            rec = {"t": int(time.time()), "nick": nick, "text": text}
                            uid = fields[1] if len(fields) > 1 else ""
                            if uid.split("(")[0] in config.SELF_IDS:
                                # 내 계정 채팅은 표시 상태(플래그·구독 개월)를 남겨 브라우저 전송과 비교한다.
                                rec["uid"] = uid
                                rec["flag"] = fields[6] if len(fields) > 6 else ""
                                rec["sub"] = fields[7] if len(fields) > 7 else ""
                            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
                    except Exception:
                        pass
            elif cmd == 7:
                stop.set()

    def on_close(ws, code, msg):
        stop.set()

    def on_error(ws, err):
        if str(err) != "0":
            log(f"[ws 오류] {err}")

    def ping(ws):
        while not stop.is_set():
            time.sleep(60)
            try:
                ws.send(build_packet(0, F), opcode=2)
            except Exception:
                break

    ws = websocket.WebSocketApp(url, on_open=on_open, on_message=on_message,
                                on_close=on_close, on_error=on_error,
                                header=[f"Origin: https://play.sooplive.com", f"User-Agent: {UA}"],
                                subprotocols=["chat"])
    threading.Thread(target=ping, args=(ws,), daemon=True).start()
    ws.run_forever()
    log("[연결 종료] 10초 후 재시도")
    return True


def main():
    os.makedirs(BASE, exist_ok=True)
    # 단일 인스턴스 보장: 두 개가 동시에 돌면 같은 채팅이 2번씩 수집된다.
    import fcntl
    lock_path = os.path.join(BASE, ".relay.lock")
    lock_fh = open(lock_path, "w")
    try:
        fcntl.flock(lock_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        log("[중복 실행] 다른 relay가 이미 돌고 있어 종료")
        return
    if not BJ_ID:
        log("[설정 오류] HOGI_BJ_ID가 비어 있다. .env 또는 환경변수로 채널 id를 지정한다.")
        sys.exit(2)
    log(f"=== 채팅 수집 시작 (channel={BJ_ID}, state={load_state()}) ===")
    # 이 프로세스는 수집(recent.jsonl)만 한다. 후보 생성·승인은 compose.py가 맡는다.
    # HOGI_LEGACY_FORWARD=1이면 예전 '시청자 채팅 선별 전달' 모드도 함께 돈다.
    if os.environ.get("HOGI_LEGACY_FORWARD") == "1":
        threading.Thread(target=flusher, daemon=True).start()
        threading.Thread(target=reaction_watch, daemon=True).start()
    while True:
        try:
            if not session():
                time.sleep(60)
            else:
                time.sleep(10)
        except KeyboardInterrupt:
            log("=== 종료 ===")
            sys.exit(0)
        except Exception as e:
            log(f"[오류] {e} — 15초 후 재시도")
            time.sleep(15)


if __name__ == "__main__":
    main()
