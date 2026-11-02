#!/usr/bin/env python3
import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
import config  # noqa: E402  (repo root)
"""Discord 답장으로 후보를 바로 승인/거부/첨언한다.

후보 메시지에 답장(reply)으로
  - `ㅇㅇ`, `승인`, `ok`, `a`        → 승인 (live 면 즉시 방송 전송)
  - `ㄴ`, `ㄴㄴ`, `거부`, `r`        → 거부
  - `ㅇㅇ 이건 좀 짧게`             → 승인 + 첨언
  - 그 외 문장                      → 첨언만 기록(판정은 그대로 대기)

봇 REST 로 채널 메시지를 폴링한다(게이트웨이 이벤트·공개 엔드포인트 불필요).
운영자 계정(DISCORD_OWNER_ID)이 쓴 메시지만 인정한다.

사용: python3 replycmd.py [--once]
"""
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request

import store

BASE = store.BASE
ENV_PATH = config.ENV_PATH
CURSOR = os.path.join(BASE, "reply_cursor.json")
LOG = os.path.join(BASE, "replycmd.log")

APPROVE_WORDS = ("ㅇㅇ", "ㅇ", "승인", "ok", "ㅇㅋ", "a", "A")
REJECT_WORDS = ("ㄴ", "ㄴㄴ", "노", "거부", "r", "R", "아니")


def log(msg):
    try:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(time.strftime("%Y-%m-%d %H:%M:%S") + " " + msg + "\n")
    except Exception:
        pass


try:
    import secretsrc
except Exception:
    secretsrc = None


def env(key):
    """키체인(secretsrc) → $HOGI_ENV(.env) → 환경변수 순으로 조회."""
    if secretsrc is not None:
        v = secretsrc.get(key)
        if v:
            return v
    try:
        for line in open(ENV_PATH, encoding="utf-8"):
            if line.startswith(key + "="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    except FileNotFoundError:
        pass
    return os.environ.get(key, "")


TOKEN = env("DISCORD_BOT_TOKEN")
OWNER = env("DISCORD_OWNER_ID") or config.OWNER_DISCORD_ID


def api(path):
    req = urllib.request.Request(f"https://discord.com/api/v10{path}",
                                 headers={"Authorization": f"Bot {TOKEN}",
                                          "User-Agent": "DiscordBot (https://github.com/h66rogi/hogi-chat, 1.0)"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read())


def parse_reply(text):
    """(verdict|None, note|None) — 판정 단어는 문장 맨 앞에서만 인정한다."""
    t = (text or "").strip()
    if not t:
        return None, None
    head, _, rest = t.partition(" ")
    head_up = head.strip().rstrip(".!~")
    rest = rest.strip()
    if head_up in APPROVE_WORDS or head_up.lower() in APPROVE_WORDS:
        return "approved", (rest or None)
    if head_up in REJECT_WORDS or head_up.lower() in REJECT_WORDS:
        return "rejected", (rest or None)
    return None, t  # 판정 없이 첨언만


def live_send(text):
    try:
        sys.path.insert(0, BASE)
        import importlib
        import ws_send
        importlib.reload(ws_send)
        r = ws_send.send_message(text)
        return bool(r.get("chat")) and r["chat"][0] == 0
    except Exception as e:
        log(f"[LIVE 실패] {e}")
        return False


def load_cursor():
    try:
        return json.load(open(CURSOR, encoding="utf-8"))
    except Exception:
        return {"last_id": "0"}


def save_cursor(cur):
    tmp = CURSOR + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cur, f)
    os.replace(tmp, CURSOR)


def process(once=False):
    st = store.load_state()
    ch = st.get("channel_id")
    if not ch or not TOKEN:
        return
    cur = load_cursor()
    after = cur.get("last_id", "0")
    try:
        msgs = api(f"/channels/{ch}/messages?limit=25" + (f"&after={after}" if after != "0" else ""))
    except Exception as e:
        log(f"[조회 실패] {e}")
        return
    msgs = sorted(msgs, key=lambda m: int(m["id"]))
    cands = {str(c.get("message_id")): c for c in store.candidates()}
    for m in msgs:
        cur["last_id"] = m["id"]
        author = (m.get("author") or {}).get("id")
        if str(author) != str(OWNER):
            continue
        ref = (m.get("message_reference") or {}).get("message_id")
        if not ref:
            continue
        cand = cands.get(str(ref))
        if not cand:
            continue
        verdict, note = parse_reply(m.get("content"))
        if verdict is None and not note:
            continue
        if cand.get("verdict") is not None and verdict is not None:
            log(f"[무시] 이미 판정됨 {ref} ({cand['verdict']})")
            continue
        if verdict is not None:
            sender = live_send if st.get("live") else None
            ok, msg = store.set_verdict(ref, verdict, sender=sender, note=note)
            log(f"[{verdict}] {cand.get('text')} note={note!r} → {ok} {msg}")
        elif note:
            store.add_note(ref, note)
            log(f"[첨언] {cand.get('text')} note={note!r}")
    save_cursor(cur)


def main():
    if "--once" in sys.argv:
        process(once=True)
        return
    log("=== replycmd 시작 ===")
    while True:
        try:
            process()
        except Exception as e:
            log(f"[오류] {e}")
        time.sleep(5)


if __name__ == "__main__":
    main()
