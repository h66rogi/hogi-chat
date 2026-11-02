#!/usr/bin/env python3
import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
import config  # noqa: E402  (repo root)
"""SOOP 채팅 WebSocket 로그인 + 전송. 테스트는 점 하나만."""
import json
import os
import sys
import threading
import time
import urllib.parse
import urllib.request

import websocket

BASE = config.BASE
AUTH = json.load(open(os.path.join(BASE, ".soop_auth.json"), encoding="utf-8"))
TK = AUTH.get("AuthTicket", "")
BJ_ID = config.BJ_ID
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36")
F = "\f"
ESC, TAB, HDR_LEN = 0x1B, 0x09, 14
MSG_UUID = "0000-0000-0000-0000-000000000000"


def pk(cmd, body=""):
    b = body.encode("utf-8") if isinstance(body, str) else body
    return f"\x1b\t{cmd:04d}{len(b):06d}00".encode("utf-8") + b


def parse(raw):
    if isinstance(raw, str):
        raw = raw.encode("utf-8", "replace")
    out, pos = [], 0
    while pos + HDR_LEN <= len(raw):
        if raw[pos] != ESC or raw[pos + 1] != TAB:
            pos += 1; continue
        try:
            cmd = int(raw[pos+2:pos+6]); blen = int(raw[pos+6:pos+12]); ret = int(raw[pos+12:pos+14])
        except Exception:
            pos += 1; continue
        body = raw[pos+HDR_LEN:pos+HDR_LEN+blen].decode("utf-8", "replace")
        out.append((cmd, ret, body.strip(F).split(F) if body.strip(F) else []))
        pos += HDR_LEN + blen
    return out


def _cookie_header():
    """로그인 쿠키(AuthTicket·UserTicket·BbsTicket). 없으면 빈 문자열."""
    ck = {}
    try:
        for line in open(os.path.join(BASE, ".soop_cookies.txt"), encoding="utf-8"):
            p = line.rstrip("\n").split("\t")
            if len(p) >= 7 and not line.startswith("#"):
                ck[p[5]] = p[6]
    except FileNotFoundError:
        for k in ("AuthTicket", "UserTicket", "BbsTicket"):
            if AUTH.get(k):
                ck[k] = AUTH[k]
    return "; ".join(f"{k}={v}" for k, v in ck.items())


def chan():
    """로그인 쿠키를 실어 방송 정보를 받는다.

    쿠키 없이 받으면 FTK가 비로그인용이라 채팅방에 '구독 안 한 사람'으로 들어간다
    (2026-10-24 운영자 지적: 보낸 채팅에 구독 표시가 안 붙음). 쿠키를 실으면 같은 응답에
    USERID·SUB_PAY_CNT·TIER_TYPE이 오고 FTK도 내 계정용으로 바뀐다.
    """
    url = f"https://live.sooplive.com/afreeca/player_live_api.php?bjid={BJ_ID}"
    data = urllib.parse.urlencode({"bid": BJ_ID, "bno": "0", "type": "live", "player_type": "html5",
                                   "mode": "landing", "from_api": "0", "pwd": "",
                                   "stream_type": "common", "quality": "HD"}).encode()
    req = urllib.request.Request(url, data=data, headers={
        "User-Agent": UA, "Referer": f"https://play.sooplive.com/{BJ_ID}",
        "Origin": "https://play.sooplive.com", "Cookie": _cookie_header()})
    return json.loads(urllib.request.urlopen(req, timeout=20).read())["CHANNEL"]


def _uuid():
    path = os.path.join(BASE, ".soop_uuid")
    try:
        return open(path).read().strip()
    except FileNotFoundError:
        import uuid
        u = uuid.uuid4().hex
        with open(path, "w") as f:
            f.write(u)
        return u


def join_payload(ch):
    """웹 플레이어와 같은 로그인 입장 패킷 본문(FTK + 시청 정보)."""
    S, E0, E1 = "\x06", "\x11", "\x12"
    vp = (ch.get("VIEWPRESET") or [{}])
    log = {"set_bps": ch.get("BPS", ""), "view_bps": vp[0].get("bps", ""), "quality": "normal",
           "uuid": _uuid(), "geo_cc": ch.get("geo_cc", "KR"), "geo_rc": ch.get("geo_rc", ""),
           "acpt_lang": ch.get("acpt_lang", "ko_KR"), "svc_lang": ch.get("svc_lang", "ko_KR"),
           "subscribe": 0, "lowlatency": 0, "mode": "landing"}
    q = "".join(f"{S}&{S}{k}{S}={S}{v}" for k, v in log.items())
    return (f"{F}{ch['CHATNO']}{F}{ch['FTK']}{F}0{F}"
            f"log{E0}{q}{E1}pwd{E0}{E1}auth_info{E0}NULL{E1}pver{E0}2{E1}access_system{E0}html5{E1}{F}")


def _guard(text, minutes=10):
    """도배 가드: 10분 내 같은 문장은 보내지 않는다."""
    path = os.path.join(BASE, "sent.jsonl")
    cutoff = time.time() - minutes * 60
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                try:
                    d = json.loads(line)
                except Exception:
                    continue
                if d.get("t", 0) >= cutoff and (d.get("text") or "").strip() == text.strip():
                    return False
    except FileNotFoundError:
        pass
    return True


def _remember(text):
    with open(os.path.join(BASE, "sent.jsonl"), "a", encoding="utf-8") as f:
        f.write(json.dumps({"t": int(time.time()), "text": text}, ensure_ascii=False) + "\n")


def _rate_ok(min_gap=3):
    """전송 간 최소 간격만 건다(연타 방지, 운영자 지시로 20초에서 3초, 2026-10-24).

    시간당 상한은 운영자 지시로 뺐다(2026-10-24 "시간당 20건 초과 빼자"). 매 건 사람이 고르므로 상한이 의미 없다.
    같은 문장 10분 재전송 금지는 _guard에서 따로 유지한다.
    """
    path = os.path.join(BASE, "sent.jsonl")
    now = time.time()
    last = 0.0
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                try:
                    last = max(last, float(json.loads(line).get("t", 0)))
                except Exception:
                    continue
    except FileNotFoundError:
        return True, ""
    if now - last < min_gap:
        return False, f"최소 간격 {min_gap}초 미달"
    return True, ""


def send_message(text, wait=3, dry=False):
    """로그인해서 text를 보낸다. 결과 dict 반환. 같은 문장은 10분 내 재전송 금지.

    dry=True면 로그인·입장까지만 하고 채팅은 보내지 않는다(계정 상태 점검용).
    """
    if dry:
        pass
    elif not _guard(text):
        return {"ok": False, "reason": "duplicate_within_10m"}
    ok, why = _rate_ok() if not dry else (True, "")
    if not ok:
        return {"ok": False, "reason": why}
    ch = chan()
    if str(ch.get("RESULT")) != "1":
        return {"ok": False, "reason": "offline"}
    chatno, ftk = ch["CHATNO"], ch["FTK"]
    res_user = {"userid": ch.get("USERID"), "sub_months": ch.get("SUB_PAY_CNT"), "tier": ch.get("TIER_TYPE")}
    url = f"wss://{ch['CHDOMAIN']}:{int(ch['CHPT'])+1}/Websocket/{BJ_ID}"
    res = {"ok": False, "login": None, "join": None, "chat": None, "user": None, "account": res_user}
    joined = threading.Event()
    finished = threading.Event()

    def on_open(ws):
        ws.send(pk(1, f"{F}{TK}{F}{F}16{F}"), opcode=2)

    def on_message(ws, m):
        for cmd, ret, fields in parse(m):
            if cmd == 1:
                res["login"] = (ret, fields[0] if fields else "")
                if fields and "(" in fields[0]:
                    res["user"] = fields[0].split("(")[0]
                ws.send(pk(2, join_payload(ch)), opcode=2)
            elif cmd == 2:
                res["join"] = ret
                # 로그인 입장 뒤 ENTER_INFO(0012)를 보내야 내 계정 상태(구독 등)가 채팅방에 적용된다.
                raw = m if isinstance(m, bytes) else m.encode("utf-8", "replace")
                i = raw.find(b"\x1b\t0002")
                parts = raw[i + HDR_LEN:].decode("utf-8", "replace").split(F) if i >= 0 else []
                syn_ack = parts[7] if len(parts) > 7 else ""
                res["syn_ack"] = bool(syn_ack)
                if syn_ack:
                    ws.send(pk(12, f"{F}{syn_ack}{F}0{F}"), opcode=2)
                joined.set()
            elif cmd == 12:
                res["enter_info"] = (ret, fields[:3])
            elif cmd == 5:
                res["chat"] = (ret, fields[0] if fields else "")
                finished.set()
            elif cmd in (35, 36, 57):
                res["chat"] = (cmd, fields[0] if fields else "")
                finished.set()

    ws = websocket.WebSocketApp(url, on_open=on_open, on_message=on_message,
                                header=["Origin: https://play.sooplive.com", f"User-Agent: {UA}"],
                                subprotocols=["chat"])
    threading.Thread(target=ws.run_forever, daemon=True).start()
    joined.wait(5)
    if not joined.is_set():
        ws.close()
        return {**res, "reason": "join_failed"}
    time.sleep(0.8)
    if dry:
        time.sleep(1.5)
        ws.close()
        return res
    ws.send(pk(5, f"{F}{text}{F}0{F}"), opcode=2)
    _remember(text)  # 전송 이력은 여기서만 1회 기록(게이트웨이에서 또 쓰면 상한이 두 배로 빨리 찬다)
    finished.wait(wait)
    ws.close()
    # 실제 전송 성공 판정: SOOP는 정상 전송에도 cmd5 ret=0을 돌려준다(2026-10-24 실측).
    # ret 0/5 모두 성공으로 보고, 본문이 그대로 돌아왔는지로 확인한다.
    res["ok"] = bool(res["chat"]) and res["chat"][0] in (0, 5) and res["chat"][1] == text
    return res


if __name__ == "__main__":
    text = sys.argv[1] if len(sys.argv) > 1 else "."
    r = send_message(text)
    print(json.dumps(r, ensure_ascii=False))
