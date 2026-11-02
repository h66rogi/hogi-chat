#!/usr/bin/env python3
"""#hogi_chat 에 올린 후보의 리액션을 읽어 승인/거부를 기록한다.

✅ = 실제 방송 채팅으로 보낸다 (SOOP 로그인 세션 필요)
❌ = 폐기
판정은 training.jsonl 에 쌓여 다음 생성 프롬프트의 예시로 쓰인다.

사용: python3 reaction.py [--once]
"""

import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
import config  # noqa: E402  (repo root)
import json
import os
import sys
import time
import urllib.parse
import urllib.request

HOME = os.path.expanduser("~")
BASE = config.BASE
CAND = os.path.join(BASE, "candidates.jsonl")
TRAIN = os.path.join(BASE, "training.jsonl")
LOG = os.path.join(BASE, "reaction.log")
ENV_PATH = config.ENV_PATH

APPROVE = "✅"
REJECT = "❌"


def log(msg):
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(time.strftime("%Y-%m-%d %H:%M:%S") + " " + msg + "\n")


def load_env():
    env = {}
    try:
        for raw in open(ENV_PATH, encoding="utf-8"):
            raw = raw.strip()
            if raw and not raw.startswith("#") and "=" in raw:
                k, v = raw.split("=", 1)
                env[k.strip()] = v.strip().strip('"').strip("'")
    except FileNotFoundError:
        pass
    env.update({k: v for k, v in os.environ.items() if k.startswith(("DISCORD_", "HOGI_"))})
    return env


ENV = load_env()


def api(path):
    req = urllib.request.Request(
        f"https://discord.com/api/v10{path}",
        headers={"Authorization": f"Bot {ENV['DISCORD_BOT_TOKEN']}",
                 "User-Agent": "DiscordBot (https://github.com/h66rogi/hogi-chat, 1.0)"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read())


def reactions_of(channel_id, message_id):
    """메시지 1회 조회로 리액션 이모지를 보고, 있을 때만 사용자 목록을 확인한다."""
    owner = ENV.get("DISCORD_OWNER_ID") or config.OWNER_DISCORD_ID
    try:
        msg = api(f"/channels/{channel_id}/messages/{message_id}")
    except Exception:
        return set()
    present = set()
    for r in msg.get("reactions") or []:
        name = (r.get("emoji") or {}).get("name")
        if name in (APPROVE, REJECT) and (r.get("count") or 0) > 0:
            present.add(name)
    names = set()
    for emoji in present:
        q = urllib.parse.quote(emoji)
        try:
            users = api(f"/channels/{channel_id}/messages/{message_id}/reactions/{q}?limit=50")
        except Exception:
            continue
        for u in users or []:
            if str(u.get("id")) == str(owner):
                names.add(emoji)
    return names


def live_send(text):
    """SOOP 채팅으로 WebSocket 직접 전송 (로그인 쿠키 사용, 브라우저 불필요)."""
    try:
        sys.path.insert(0, BASE)
        import importlib
        import ws_send
        importlib.reload(ws_send)
        r = ws_send.send_message(text)
        ok = bool(r.get("ok"))
        log(f"[LIVE {'ok' if ok else 'fail'}] {text} :: {json.dumps(r, ensure_ascii=False)[:200]}")
        return ok
    except Exception as e:
        log(f"[LIVE 실패] {e}")
        return False


def process():
    """후보별 리액션을 확인해 store 로 판정을 기록한다 (중복 append·동시쓰기 방지)."""
    try:
        import store
    except Exception:
        store = None
    if not os.path.exists(CAND):
        return
    st = {}
    try:
        st = json.load(open(os.path.join(BASE, "state.json"), encoding="utf-8"))
    except Exception:
        pass
    ch = st.get("channel_id")
    for rec in store.candidates() if store else []:
        if rec.get("verdict") is not None or not rec.get("message_id") or not ch:
            continue
        if rec.get("ts", 0) <= time.time() - 3600:
            continue
        names = reactions_of(ch, rec["message_id"])
        if APPROVE in names:
            store.set_verdict(rec["message_id"], "approved",
                              sender=live_send if st.get("live") else None)
        elif REJECT in names:
            store.set_verdict(rec["message_id"], "rejected")


def main():
    if "--once" in sys.argv:
        process()
        return
    while True:
        try:
            process()
        except Exception as e:
            log(f"[오류] {e}")
        time.sleep(45)


if __name__ == "__main__":
    main()
