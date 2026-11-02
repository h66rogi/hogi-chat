#!/usr/bin/env python3
import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
import config  # noqa: E402  (repo root)
"""로컬 Discord 게이트웨이 클라이언트 — 공개 HTTPS 엔드포인트 없이 버튼/모달을 처리한다.

WebSocket으로 Discord 게이트웨이에 붙어 INTERACTION_CREATE 를 직접 받고, 응답은
REST callback 으로 보낸다. Cloudflare 뒤에 두면 Discord가 PING을 못 넣는 문제를 피한다.

환경변수(.env 또는 셸 환경): DISCORD_HOGI_BOT_TOKEN
실행: /usr/bin/python3 bot_gateway.py
"""
import json
import os
import sys
import threading
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import store  # noqa: E402

try:
    import websocket  # websocket-client
except Exception:
    websocket = None

ENV_PATH = config.ENV_PATH
BASE = store.BASE
LOG = os.path.join(BASE, "bot_gateway.log")
GATEWAY = "wss://gateway.discord.gg/?v=10&encoding=json"
API = "https://discord.com/api/v10"


def log(msg):
    try:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(time.strftime("%Y-%m-%d %H:%M:%S") + " " + str(msg) + "\n")
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


try:
    import secretsrc
except Exception:
    secretsrc = None

TOKEN = (secretsrc.get("DISCORD_HOGI_BOT_TOKEN") if secretsrc else None) or env("DISCORD_HOGI_BOT_TOKEN")


def api(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(API + path, data=data, method=method,
                                 headers={"Authorization": f"Bot {TOKEN}",
                                          "Content-Type": "application/json",
                                          "User-Agent": "DiscordBot (https://github.com/h66rogi/hogi-chat, 1.0)"})
    with urllib.request.urlopen(req, timeout=20) as r:
        raw = r.read()
        return json.loads(raw) if raw else {}


def live_send(text):
    try:
        import importlib
        import ws_send
        importlib.reload(ws_send)
        r = ws_send.send_message(text)
        return bool(r.get("chat")) and r["chat"][0] == 0
    except Exception as e:
        log(f"[LIVE 실패] {e}")
        return False


def components_for(mid, disabled=False):
    return [{"type": 1, "components": [
        {"type": 2, "style": 3, "label": "승인", "emoji": {"name": "✅"},
         "custom_id": f"v:approved:{mid}", "disabled": disabled},
        {"type": 2, "style": 4, "label": "거부", "emoji": {"name": "❌"},
         "custom_id": f"v:rejected:{mid}", "disabled": disabled},
        {"type": 2, "style": 2, "label": "첨언", "emoji": {"name": "📝"},
         "custom_id": f"note:{mid}", "disabled": disabled}]}]


def candidate(mid):
    for r in store.candidates():
        if str(r.get("message_id")) == str(mid):
            return r
    return None


def respond(interaction_id, token, body):
    try:
        api("POST", f"/interactions/{interaction_id}/{token}/callback", body)
    except Exception as e:
        log(f"[응답 실패] {e}")


def on_component(data):
    cid = data.get("custom_id", "")
    mid = cid.split(":")[-1]
    if cid.startswith("note:"):
        return {"type": 9, "data": {
            "title": "첨언 남기기", "custom_id": f"notesubmit:{mid}",
            "components": [{"type": 1, "components": [
                {"type": 4, "custom_id": "note_text", "label": "이 문장에 대한 메모",
                 "style": 2, "max_length": 200, "required": True}]}]}}
    if cid.startswith("v:"):
        verdict = cid.split(":")[1]
        rec = candidate(mid)
        if not rec:
            return {"type": 4, "data": {"content": "후보를 찾을 수 없음", "flags": 64}}
        if rec.get("verdict"):
            return {"type": 4, "data": {"content": f"이미 판정됨: {rec['verdict']}", "flags": 64}}
        st = store.load_state()
        ok, msg = store.set_verdict(mid, verdict, sender=live_send if st.get("live") else None,
                                    note=rec.get("note"))
        rec2 = candidate(mid) or {}
        label = "승인" if verdict == "approved" else "거부"
        extra = ""
        if verdict == "approved":
            extra = " · 방송 전송됨" if rec2.get("live_sent") else " · 전송 안 함(전송 OFF 또는 실패)"
        text = rec.get("text", "")
        content = f"{text}\n— {label}{extra}" if ok else f"{text}\n— 실패: {msg}"
        return {"type": 7, "data": {"content": content[:1900],
                                    "components": components_for(mid, disabled=True)}}
    return {"type": 4, "data": {"content": "알 수 없는 버튼", "flags": 64}}


def on_modal(data):
    mid = data.get("custom_id", "").split(":")[-1]
    note = ""
    for row in data.get("components", []):
        for c in row.get("components", []):
            if c.get("custom_id") == "note_text":
                note = (c.get("value") or "").strip()
    if not note:
        return {"type": 4, "data": {"content": "빈 첨언", "flags": 64}}
    store.add_note(mid, note)
    rec = candidate(mid) or {}
    return {"type": 4, "data": {"content": f"첨언 저장됨 ({rec.get('verdict') or '대기'}): {note}"[:1900],
                                "flags": 64}}


def handle(ws, payload):
    t = payload.get("t")
    d = payload.get("d") or {}
    if t != "INTERACTION_CREATE":
        return
    itype = d.get("type")
    idata = d.get("data") or {}
    if itype == 3:
        body = on_component(idata)
    elif itype == 5:
        body = on_modal(idata)
    else:
        return
    respond(d.get("id"), d.get("token"), body)
    log(f"[interaction] type={itype} custom_id={idata.get('custom_id')}")


class Client:
    def __init__(self):
        self.seq = None
        self.hb = None
        self.stop = False

    def run(self):
        if websocket is None:
            log("[중단] websocket-client 없음")
            return
        if not TOKEN:
            log("[중단] DISCORD_HOGI_BOT_TOKEN 없음")
            return
        ws = websocket.WebSocket()
        ws.connect(GATEWAY, timeout=30)
        hello = json.loads(ws.recv())
        interval = hello["d"]["heartbeat_interval"] / 1000.0
        self.hb = threading.Thread(target=self._heartbeat, args=(ws, interval), daemon=True)
        self.hb.start()
        ws.send(json.dumps({"op": 2, "d": {
            "token": TOKEN,
            "intents": 0,  # interaction 전용
            "properties": {"os": "macos", "browser": "hogi", "device": "hogi"}}}))
        log("[게이트웨이] IDENTIFY 전송")
        while not self.stop:
            try:
                msg = ws.recv()
            except Exception as e:
                log(f"[수신 오류] {e}")
                break
            if not msg:
                continue
            try:
                p = json.loads(msg)
            except Exception:
                continue
            if p.get("s"):
                self.seq = p["s"]
            op, t = p.get("op"), p.get("t")
            if op == 10:
                continue
            if t == "READY":
                log(f"[게이트웨이] READY user={(p['d'].get('user') or {}).get('username')}")
            elif t == "INTERACTION_CREATE":
                try:
                    handle(ws, p)
                except Exception as e:
                    log(f"[처리 오류] {e}")
        try:
            ws.close()
        except Exception:
            pass

    def _heartbeat(self, ws, interval):
        while not self.stop:
            time.sleep(interval)
            try:
                ws.send(json.dumps({"op": 1, "d": self.seq}))
            except Exception:
                return


def main():
    if not TOKEN:
        print("DISCORD_HOGI_BOT_TOKEN 이 없습니다. .env에 추가한 뒤 다시 실행하세요.")
        log("[중단] 토큰 없음")
        return
    while True:
        try:
            Client().run()
        except Exception as e:
            log(f"[재접속] {e}")
        time.sleep(5)


if __name__ == "__main__":
    main()
