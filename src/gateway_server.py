#!/usr/bin/env python3
import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
import config  # noqa: E402  (repo root)
"""후로기 콘솔용 Discord 봇 interaction 엔드포인트.

Discord가 서명한 요청만 처리한다(Ed25519, 앱 공개키).
  - PING(type 1) → PONG
  - 버튼(type 3): ✅ 승인 / ❌ 거부 / 📝 첨언(모달)
  - 모달 제출(type 5): 첨언 저장(+선택한 판정이 있으면 함께 적용)

환경변수(.env 또는 셸 환경):
  DISCORD_HOGI_PUBLIC_KEY  앱 공개키(hex)
  DISCORD_HOGI_BOT_TOKEN   앱 봇 토큰(메시지 갱신용)

실행: /usr/bin/python3 gateway_server.py [--port 8793]
"""
import json
import threading
import os
import sys
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import store

BASE = store.BASE
ENV_PATH = config.ENV_PATH
PORT = 8793
LOG = os.path.join(BASE, "gateway.log")


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


try:
    import secretsrc
except Exception:
    secretsrc = None


def secret(key, default=None):
    if secretsrc:
        v = secretsrc.get(key)
        if v:
            return v
    return os.environ.get(key) or env(key) or default


PUBLIC_KEY = secret("DISCORD_HOGI_PUBLIC_KEY")
TOKEN = secret("DISCORD_HOGI_BOT_TOKEN")

# 후보 키: 키체인 값과 .env 값이 다를 수 있어 둘 다 시도한다(첫 성공 키를 채택).
_CANDIDATE_KEYS = [k for k in {PUBLIC_KEY, env("DISCORD_HOGI_PUBLIC_KEY")} if k]

try:
    from nacl.exceptions import BadSignatureError
    from nacl.signing import VerifyKey
except Exception as _e:  # PyNaCl 없으면 전부 거부(fail closed)
    VerifyKey = None
    BadSignatureError = Exception
    _NACL_ERR = str(_e)


def verify(signature_hex, timestamp, body):
    if not _CANDIDATE_KEYS:
        log("[설정 없음] DISCORD_HOGI_PUBLIC_KEY")
        return False
    if VerifyKey is None:
        log(f"[PyNaCl 없음] {_NACL_ERR}")
        return False
    msg = timestamp.encode() + body
    try:
        sig = bytes.fromhex(signature_hex)
    except Exception:
        return False
    for i, pk in enumerate(_CANDIDATE_KEYS):
        try:
            VerifyKey(bytes.fromhex(pk)).verify(msg, sig)
            if i != 0:
                log(f"[키 전환] 후보키[{i}] {pk[:8]}… 가 이 앱 키다")
            return True
        except BadSignatureError:
            log(f"[키 불일치] 후보키[{i}] {pk[:8]}… body={len(body)}B ts={timestamp[:12]}")
            continue
        except Exception as e:
            log(f"[검증 오류] 후보키[{i}] {pk[:8]}… {type(e).__name__}: {e}")
    return False


def _seen_in_relay(text, seconds=8):
    """방송 채팅에 실제로 올라갔는지 relay 수집본으로 확인한다(에코 파싱이 불안정해서)."""
    import time as _t
    cutoff = _t.time() - seconds
    path = os.path.join(BASE, "recent.jsonl")
    try:
        for line in open(path, encoding="utf-8"):
            try:
                d = json.loads(line)
            except Exception:
                continue
            if d.get("t", 0) >= cutoff and (d.get("text") or "").strip() == text.strip():
                return True
    except FileNotFoundError:
        pass
    return False


LAST_SEND = {"reason": ""}


def live_send(text):
    """방송 채팅으로 보낸다. 중복·간격·상한 가드는 ws_send 한 곳에서만 건다.

    성공 판정은 relay 수집본에 같은 문장이 올라왔는지로 한다(에코는 남의 채팅이 섞여 불안정).
    실패 사유는 LAST_SEND에 남겨 카드 문구에 그대로 쓴다.
    """
    LAST_SEND["reason"] = ""
    try:
        sys.path.insert(0, BASE)
        import importlib
        import ws_send
        importlib.reload(ws_send)
        r = ws_send.send_message(text)
        if r.get("reason"):
            LAST_SEND["reason"] = {"duplicate_within_10m": "10분 내 같은 문장", "offline": "방송 꺼짐",
                                   "join_failed": "채팅방 입장 실패"}.get(r["reason"], r["reason"])
            log(f"[전송 차단] {text!r} → {LAST_SEND['reason']}")
            return False
        for _ in range(8):
            if _seen_in_relay(text):
                return True
            time.sleep(1)
        LAST_SEND["reason"] = "보냈지만 방송 채팅에서 확인 안 됨"
        return False
    except Exception as e:
        LAST_SEND["reason"] = f"오류 {type(e).__name__}"
        log(f"[LIVE 실패] {e}")
        return False


def apply_verdict(mid, verdict, note=None):
    st = store.load_state()
    sender = live_send if st.get("live") else None
    return store.set_verdict(mid, verdict, sender=sender, note=note)


def candidate_text(mid):
    for r in store.candidates():
        if str(r.get("message_id")) == str(mid):
            return r.get("text"), r.get("verdict"), r.get("live_sent"), r.get("note")
    return None, None, None, None


def components_for(mid, disabled=False):
    return [{
        "type": 1,
        "components": [
            {"type": 2, "style": 3, "label": "승인", "emoji": {"name": "✅"},
             "custom_id": f"v:approved:{mid}", "disabled": disabled},
            {"type": 2, "style": 4, "label": "거부", "emoji": {"name": "❌"},
             "custom_id": f"v:rejected:{mid}", "disabled": disabled},
            {"type": 2, "style": 2, "label": "첨언", "emoji": {"name": "📝"},
             "custom_id": f"note:{mid}", "disabled": disabled},
        ],
    }]


def _edit_original(interaction, content, mid):
    app_id = interaction.get("application_id")
    token = interaction.get("token")
    body = json.dumps({"content": content[:1900],
                       "components": components_for(mid, disabled=True)}).encode()
    req = urllib.request.Request(
        f"https://discord.com/api/v10/webhooks/{app_id}/{token}/messages/@original",
        data=body, method="PATCH",
        headers={"Content-Type": "application/json", "User-Agent": "DiscordBot (hogi, 1.0)"})
    try:
        urllib.request.urlopen(req, timeout=20).read()
    except Exception as e:
        log(f"[원본 수정 실패] {e}")


INFLIGHT = set()
INFLIGHT_LOCK = threading.Lock()


def _finish_approve(mid, text, note, interaction):
    t0 = time.time()
    try:
        ok, msg = apply_verdict(mid, "approved", note=note)
        _, _, sent, _ = candidate_text(mid)
        if ok:
            content = f"{text}\n— 승인 · " + ("방송 전송됨" if sent else f"전송 안 됨: {LAST_SEND['reason'] or '원인 미상'}")
        else:
            content = f"{text}\n— 처리 실패: {msg}"
    except Exception as e:
        content = f"{text}\n— 처리 실패: {e}"
    finally_line = content.splitlines()[-1]
    log(f"[승인 완료] {text!r} {time.time() - t0:.1f}s → {finally_line}")
    _edit_original(interaction, content, mid)
    with INFLIGHT_LOCK:
        INFLIGHT.discard(mid)


PICKS_PATH = os.path.join(BASE, "status_picks.json")
PICKS_LOCK = threading.Lock()


def _bot_api(method, path, payload=None):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request("https://discord.com/api/v10" + path, data=data, method=method,
                                 headers={"Authorization": f"Bot {TOKEN}", "Content-Type": "application/json",
                                          "User-Agent": "DiscordBot (hogi, 1.0)"})
    body = urllib.request.urlopen(req, timeout=20).read()
    return json.loads(body) if body else {}


def _claim_pick(token, idx):
    """상태 카드에서 고른 후보를 찾아 '사용됨'으로 표시. 한 카드에서 하나만 보낸다(중복 전송 방지)."""
    with PICKS_LOCK:
        try:
            data = json.load(open(PICKS_PATH, encoding="utf-8"))
        except Exception:
            return None, "후보 기록 없음"
        rec = data.get(token)
        if not rec or idx >= len(rec.get("candidates") or []):
            return None, "지난 카드라 후보를 찾을 수 없음"
        if rec.get("used") is not None:
            return None, f"이 카드에서 이미 `{rec['candidates'][rec['used']]}`를 골랐음"
        rec["used"] = idx
        tmp = PICKS_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        os.replace(tmp, PICKS_PATH)
        return rec, None


def _finish_pick(mid, text, interaction, card):
    t0 = time.time()
    try:
        ok, msg = apply_verdict(mid, "approved", note="상태 카드에서 직접 고름")
        _, _, sent, _ = candidate_text(mid)
        if ok:
            content = f"{text}\n— 직접 고름 · " + ("방송 전송됨" if sent else f"전송 안 됨: {LAST_SEND['reason'] or '전송 OFF'}")
        else:
            content = f"{text}\n— 처리 실패: {msg}"
    except Exception as e:
        content = f"{text}\n— 처리 실패: {e}"
    log(f"[직접 고름 완료] {text!r} {time.time() - t0:.1f}s → {content.splitlines()[-1]}")
    app_id, token = interaction.get("application_id"), interaction.get("token")
    req = urllib.request.Request(
        f"https://discord.com/api/v10/webhooks/{app_id}/{token}/messages/@original",
        data=json.dumps({"content": content[:1900]}).encode(), method="PATCH",
        headers={"Content-Type": "application/json", "User-Agent": "DiscordBot (hogi, 1.0)"})
    try:
        urllib.request.urlopen(req, timeout=20).read()
    except Exception as e:
        log(f"[직접 고름 결과 표시 실패] {e}")
    # 상태 카드 버튼은 비활성화하고 고른 것만 초록색으로 남긴다(카드가 이미 교체됐으면 무시)
    try:
        rows = card.get("components") or []
        for row in rows:
            for b in row.get("components", []):
                b["disabled"] = True
                if b.get("custom_id", "").endswith(":" + mid.rsplit("-", 1)[-1]) and b.get("label") == text:
                    b["style"] = 3
        _bot_api("PATCH", f"/channels/{card['channel_id']}/messages/{card['id']}", {"components": rows})
    except Exception as e:
        log(f"[상태 카드 버튼 정리 실패] {e}")
    with INFLIGHT_LOCK:
        INFLIGHT.discard(mid)


def handle_pick(cid, interaction):
    owner = secret("DISCORD_OWNER_ID")
    uid = ((interaction.get("member") or {}).get("user") or interaction.get("user") or {}).get("id")
    if owner and str(uid) != str(owner):
        return {"type": 4, "data": {"content": "권한 없음", "flags": 64}}
    _, token, idx = cid.split(":")
    rec, err = _claim_pick(token, int(idx))
    if err:
        return {"type": 4, "data": {"content": err, "flags": 64}}
    text = rec["candidates"][int(idx)]
    mid = f"pick-{token}-{idx}"
    store.append_candidate({"ts": int(time.time()), "message_id": mid, "text": text, "source": "status_pick",
                            "context": rec.get("context") or [], "trace": {"judge": rec.get("judge"),
                                                                           "picked_from": rec["candidates"]},
                            "verdict": None})
    with INFLIGHT_LOCK:
        INFLIGHT.add(mid)
    if not store.load_state().get("live"):
        try:
            ok, msg = apply_verdict(mid, "approved", note="상태 카드에서 직접 고름")
        finally:
            with INFLIGHT_LOCK:
                INFLIGHT.discard(mid)
        return {"type": 4, "data": {"content": f"{text}\n— 직접 고름 · 전송 OFF라 기록만 함"}}
    threading.Thread(target=_finish_pick, args=(mid, text, interaction, interaction.get("message") or {}),
                     daemon=True).start()
    return {"type": 4, "data": {"content": f"{text}\n— 직접 고름 · 방송 전송 중…",
                                "allowed_mentions": {"parse": []}}}


def handle_component(data, interaction=None):
    cid = data.get("custom_id", "")
    if cid.startswith("pick:"):
        return handle_pick(cid, interaction or {})
    mid = cid.split(":")[-1]
    if cid.startswith("note:"):
        return {
            "type": 9,  # MODAL
            "data": {
                "title": "첨언 남기기",
                "custom_id": f"notesubmit:{mid}",
                "components": [{
                    "type": 1,
                    "components": [{
                        "type": 4, "custom_id": "note_text", "label": "이 문장에 대한 메모",
                        "style": 2, "max_length": 200, "required": True,
                        "placeholder": "예: 너무 딱딱함 / 이 표현 내가 안 씀",
                    }],
                }],
            },
        }
    if cid.startswith("v:"):
        verdict = cid.split(":")[1]
        text, cur, sent, note = candidate_text(mid)
        if text is None:
            return {"type": 4, "data": {"content": "후보를 찾을 수 없음", "flags": 64}}
        if cur is not None:
            return {"type": 4, "data": {"content": f"이미 판정됨: {cur}", "flags": 64}}
        with INFLIGHT_LOCK:
            if mid in INFLIGHT:
                return {"type": 4, "data": {"content": "처리 중", "flags": 64}}
            INFLIGHT.add(mid)
        label = "승인" if verdict == "approved" else "거부"
        if verdict == "approved" and store.load_state().get("live") and interaction:
            # 방송 전송 확인에 몇 초 걸려 Discord 3초 응답 기한을 넘긴다("상호작용 실패").
            # 그래서 먼저 '전송 중'으로 즉시 응답하고, 전송·판정은 뒤에서 끝낸 뒤 원본 메시지를 고친다.
            threading.Thread(target=_finish_approve, args=(mid, text, note, interaction),
                             daemon=True).start()
            return {"type": 7, "data": {"content": f"{text}\n— 승인 · 방송 전송 중…"[:1900],
                                        "components": components_for(mid, disabled=True)}}
        try:
            ok, msg = apply_verdict(mid, verdict, note=note)
        finally:
            with INFLIGHT_LOCK:
                INFLIGHT.discard(mid)
        _, _, sent2, _ = candidate_text(mid)
        extra = ""
        if verdict == "approved":
            extra = " · 방송 전송됨" if sent2 else " · 전송 안 함(전송 OFF 또는 실패)"
        content = f"{text}\n— {label}{extra}" if ok else f"{text}\n— 처리 실패: {msg}"
        return {"type": 7, "data": {"content": content[:1900],
                                    "components": components_for(mid, disabled=True)}}
    return {"type": 4, "data": {"content": "알 수 없는 버튼", "flags": 64}}


def handle_modal(data):
    cid = data.get("custom_id", "")
    mid = cid.split(":")[-1]
    fields = {c["components"][0]["custom_id"]: c["components"][0].get("value", "")
              for c in data.get("components", []) if c.get("components")}
    note = (fields.get("note_text") or "").strip()
    if not note:
        return {"type": 4, "data": {"content": "빈 첨언", "flags": 64}}
    store.add_note(mid, note)
    text, cur, sent, _ = candidate_text(mid)
    state = cur or "대기"
    return {"type": 4, "data": {"content": f"첨언 저장됨 ({state}): {note}"[:1900], "flags": 64}}


def send_message(channel_id, text, mid):
    """새 봇 토큰으로 후보 메시지를 버튼과 함께 보낸다."""
    body = json.dumps({"content": text[:1900],
                       "components": components_for(mid),
                       "allowed_mentions": {"parse": []}}).encode()
    req = urllib.request.Request(f"https://discord.com/api/v10/channels/{channel_id}/messages",
                                 data=body, headers={"Authorization": f"Bot {TOKEN}",
                                                     "Content-Type": "application/json",
                                                     "User-Agent": "DiscordBot (https://github.com/h66rogi/hogi-chat, 1.0)"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read())


INTERACTIONS = {}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        log(f"[GET] {self.path} host={self.headers.get('Host')} ua={(self.headers.get('User-Agent') or '')[:30]}")
        return self._send(200, {"ok": True, "service": "hogi-console gateway"})

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n)
        sig = self.headers.get("X-Signature-Ed25519", "")
        ts = self.headers.get("X-Signature-Timestamp", "")
        log(f"[요청] {self.command} {self.path} siglen={len(sig)} ts={ts} "
            f"body={len(body)}B ua={(self.headers.get('User-Agent') or '')[:40]} "
            f"cf-ray={(self.headers.get('CF-Ray') or '-')}")
        if not verify(sig, ts, body):
            log("[서명 실패] 요청 거부")
            return self._send(401, {"error": "invalid request signature"})
        try:
            data = json.loads(body or b"{}")
        except Exception:
            return self._send(400, {"error": "bad json"})
        t = data.get("type")
        log(f"[상호작용] type={t} custom_id={(data.get('data') or {}).get('custom_id')} "
            f"user={(data.get('member') or {}).get('user', {}).get('id')}")
        if t == 1:
            return self._send(200, {"type": 1})
        if t == 3:
            res = handle_component(data.get("data") or {}, data)
            log(f"[버튼 응답] type={res.get('type')} content={(res.get('data') or {}).get('content', '')[:60]!r}")
            return self._send(200, res)
        if t == 5:
            return self._send(200, handle_modal(data.get("data") or {}))
        return self._send(400, {"error": f"unsupported type {t}"})


def main():
    global PORT
    if "--port" in sys.argv:
        PORT = int(sys.argv[sys.argv.index("--port") + 1])
    log(f"=== gateway 시작 :{PORT} key={'set' if PUBLIC_KEY else 'MISSING'} token={'set' if TOKEN else 'MISSING'}")
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
