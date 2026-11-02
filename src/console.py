#!/usr/bin/env python3
"""채팅 후보 승인 콘솔 (로컬 웹 UI).

http://localhost:8791
  - 후보마다 "왜 이 문장이 나왔는지" 컨텍스트 근거를 함께 보여준다
  - ✅ 승인 / ❌ 거부, 생성·지각·전송 토글, 프롬프트 전문, 리플레이 결과 탭

사용: python3 console.py [--port 8791] [--host 127.0.0.1]
"""

import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
import config  # noqa: E402  (repo root)
import glob
import json
import re
import os
import sys
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import store

PORT = 8791
HOST = "127.0.0.1"  # 리버스 프록시 뒤에 두거나 사설망에서만 접근한다


def live_send(text):
    """SOOP 실제 채팅 전송 (WebSocket, 로그인 쿠키)."""
    try:
        sys.path.insert(0, store.BASE)
        import importlib
        import ws_send
        importlib.reload(ws_send)
        r = ws_send.send_message(text)
        return bool(r.get("ok"))
    except Exception as e:
        store._log(f"[LIVE 실패] {e}")
        return False


PAGE = r"""<!doctype html>
<html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>hogi-chat 콘솔</title>
<style>
:root{--bg:#111316;--card:#1a1d21;--line:#2a2f35;--fg:#e6e8ea;--dim:#8b929a;--ok:#3ba55d;--no:#ed4245;--acc:#5865f2}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.5 -apple-system,BlinkMacSystemFont,"Apple SD Gothic Neo",sans-serif}
header{position:sticky;top:0;z-index:5;background:#0d0f11ee;backdrop-filter:blur(8px);border-bottom:1px solid var(--line);padding:10px 14px}
.row{display:flex;gap:10px;align-items:center;flex-wrap:wrap}
h1{font-size:15px;margin:0 12px 0 0;font-weight:600;letter-spacing:-.2px}
.stat{color:var(--dim);font-size:13px}
.stat b{color:var(--fg)}
.tabs{display:flex;gap:6px;margin-top:8px}
.tab{padding:4px 10px;border:1px solid var(--line);border-radius:999px;font-size:13px;color:var(--dim);cursor:pointer;background:transparent}
.tab.on{background:var(--acc);border-color:var(--acc);color:#fff}
.toggle{margin-left:auto;display:flex;gap:14px;font-size:13px;color:var(--dim)}
.toggle label{display:flex;gap:6px;align-items:center;cursor:pointer}
main{padding:12px 14px 80px;max-width:960px;margin:0 auto}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:12px 14px;margin-bottom:10px}
.card.focus{border-color:var(--acc);box-shadow:0 0 0 1px var(--acc)}
.line{font-size:19px;font-weight:600;letter-spacing:-.2px;margin:2px 0 6px}
.meta{font-size:12px;color:var(--dim);margin-bottom:6px}
.prov{margin-top:8px;border-top:1px dashed var(--line);padding-top:8px;font-size:13px}
.blk{margin:6px 0}
.blk .k{font-size:11px;letter-spacing:.04em;color:var(--dim);text-transform:uppercase}
.blk ul{margin:3px 0 0;padding-left:16px;color:#c3c9d0}
.blk li{white-space:pre-wrap}
.blk .none{color:#6f767d;font-size:12px}
.chips{display:flex;gap:6px;flex-wrap:wrap;margin-top:3px}
.chip{font-size:12px;border:1px solid var(--line);border-radius:999px;padding:1px 8px;color:#c3c9d0}
.btns{display:flex;gap:8px;margin-top:10px;flex-wrap:wrap;align-items:center}
input.note{flex:1;min-width:140px;font:inherit;font-size:13px;padding:6px 10px;border-radius:8px;border:1px solid var(--line);background:#0f1113;color:var(--fg)}
input.note::placeholder{color:#6f767d}
button{font:inherit;font-size:14px;padding:6px 14px;border-radius:8px;border:1px solid var(--line);background:#22262b;color:var(--fg);cursor:pointer}
button.ok{background:var(--ok);border-color:var(--ok);color:#fff}
button.no{background:var(--no);border-color:var(--no);color:#fff}
button.ghost{background:transparent;color:var(--dim)}
button:disabled{opacity:.45;cursor:default}
.badge{font-size:12px;padding:2px 8px;border-radius:999px;border:1px solid var(--line);color:var(--dim)}
.badge.approved{color:#3ba55d;border-color:#3ba55d55}
.badge.rejected{color:#ed4245;border-color:#ed424555}
.hint{color:var(--dim);font-size:12px;text-align:center;padding:20px 0;line-height:1.8}
.toast{position:fixed;left:50%;transform:translateX(-50%);bottom:18px;background:#22262b;border:1px solid var(--line);padding:8px 16px;border-radius:999px;font-size:13px;opacity:0;transition:.2s}
.toast.show{opacity:1}
pre.prompt{white-space:pre-wrap;background:#0f1113;border:1px solid var(--line);border-radius:8px;padding:10px;font-size:12px;color:#cfd5db;max-height:340px;overflow:auto;margin-top:8px}
details summary{cursor:pointer;color:var(--dim);font-size:12px}
.rp{border:1px solid var(--line);border-radius:10px;padding:10px 12px;margin-bottom:10px;background:#171a1e}
.rp .t{font-size:12px;color:var(--dim);margin-bottom:6px}
</style></head><body>
<header>
  <div class="row">
    <h1>hogi-chat 콘솔</h1>
    <span class="stat">대기 <b id="s-pend">-</b></span>
    <span class="stat">승인 <b id="s-appr">-</b></span>
    <span class="stat">거부 <b id="s-rej">-</b></span>
    <span class="stat">전송 <b id="s-live">-</b></span>
    <span class="toggle">
      <label><input type="checkbox" id="t-gen"> 생성</label>
      <label><input type="checkbox" id="t-perc"> 지각</label>
      <label><input type="checkbox" id="t-live"> 실제 전송</label>
    </span>
  </div>
  <div class="tabs">
    <div class="tab on" data-f="pending">대기</div>
    <div class="tab" data-f="approved">승인</div>
    <div class="tab" data-f="rejected">거부</div>
    <div class="tab" data-f="all">전체</div>
    <div class="tab" data-f="replay">리플레이</div>
  </div>
</header>
<main id="list"><div class="hint">불러오는 중…</div></main>
<div class="toast" id="toast"></div>
<script>
let filter="pending", items=[], cursor=0;
const $=s=>document.querySelector(s);
function ago(ts){ if(!ts) return ""; const d=Math.max(0,Date.now()/1000-ts);
  if(d<60) return Math.round(d)+"초 전"; if(d<3600) return Math.round(d/60)+"분 전";
  if(d<86400) return Math.round(d/3600)+"시간 전"; return Math.round(d/86400)+"일 전"; }
function esc(s){ return (s||"").replace(/[&<>]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;"}[c])); }
function toast(m){ const t=$("#toast"); t.textContent=m; t.classList.add("show"); setTimeout(()=>t.classList.remove("show"),1400); }
function ul(arr, empty){ if(!arr||!arr.length) return `<div class="none">${empty||"없음"}</div>`;
  return `<ul>${arr.map(x=>`<li>${esc(x)}</li>`).join("")}</ul>`; }

function provHtml(x){
  const T=x.trace;
  if(!T){
    const ctx=(x.context||[]).map(c=>`${c.nick}: ${c.text}`).join("\n");
    return `<div class="prov"><div class="blk"><div class="k">근거 기록 없음 (이 기능 이전 후보)</div>
      <pre class="prompt">${esc(ctx)||"(맥락 없음)"}</pre></div></div>`;
  }
  const chat=(T.chat||[]).slice(-10).map(c=>`${c.nick}: ${c.text}`);
  const q=(T.quarantined||[]), im=(T.impostor||[]), ad=(T.addressed||[]);
  const trustChips = `<div class="chips">
      <span class="chip">${T.mode==="answer"?"지목됨 → 답변 모드":"리액션 모드"}</span>
      <span class="chip">${T.trust==="mixed"?"주의(격리 있음)":"깨끗함"}</span>
      ${q.length?`<span class="chip">지시형 격리 ${q.length}</span>`:""}
      ${im.length?`<span class="chip">사칭 의심 ${im.length}</span>`:""}</div>`;
  return `<div class="prov">
    <div class="blk"><div class="k">지목·신뢰</div>${trustChips}
      ${ad.length?ul(ad.map(m=>`${m.nick}: ${m.text}`),"없음"):""}</div>
    <div class="blk"><div class="k">격리된 줄 (프롬프트 제외)</div>
      ${q.length||im.length?ul([...q.map(x=>`지시형 · ${x.nick}: ${x.text}`),...im.map(x=>`사칭 · ${x.nick}: ${x.text}`)]):'<div class="none">없음</div>'}</div>
    <div class="blk"><div class="k">화면 (캡처 설명)</div>${ul(T.scene,"없음")}</div>
    <div class="blk"><div class="k">BJ 발화 (자동 전사)</div>${ul(T.speech,"없음")}</div>
    <div class="blk"><div class="k">화제어</div><div class="chips">${(T.hot||[]).map(h=>`<span class="chip">${esc(h)}</span>`).join("")||'<span class="chip">없음</span>'}</div></div>
    <div class="blk"><div class="k">과거 발화 참고 (RAG)</div>${ul(T.rag,"없음")}</div>
    <div class="blk"><div class="k">회피: 이미 올린 후보</div>${ul(T.prior,"없음")}</div>
    <div class="blk"><div class="k">승인 예시</div>${ul(T.approved,"없음 (시드 복사분은 제외)")}</div>
    <div class="blk"><div class="k">거부 예시</div>${ul(T.rejected,"없음")}</div>
    <div class="blk"><div class="k">채팅 흐름 (최근 10줄)</div>${ul(chat,"없음")}</div>
    <div class="blk"><div class="k">메타</div><div class="chips"><span class="chip">${esc(T.model||"")}</span><span class="chip">채팅 ${(T.chat||[]).length}줄</span><span class="chip">${esc(T.title||"")}</span></div></div>
    <details><summary>프롬프트 전문 보기</summary><pre class="prompt" data-mid="${x.message_id}">열면 불러옵니다…</pre></details>
  </div>`;
}

async function loadPrompt(mid, el){
  if(!el || el.dataset.done) return;
  const r=await fetch("/api/prompt?message_id="+encodeURIComponent(mid)); const d=await r.json();
  el.textContent = d.ok ? d.prompt : (d.error||"프롬프트 기록 없음");
  el.dataset.done="1";
}

async function load(){
  if(filter==="replay") return loadReplays();
  const r=await fetch("/api/data"); const d=await r.json();
  $("#s-pend").textContent=d.stats.pending; $("#s-appr").textContent=d.stats.approved;
  $("#s-rej").textContent=d.stats.rejected; $("#s-live").textContent=d.stats.live_sent;
  $("#t-gen").checked=!!d.state.enabled; $("#t-perc").checked=!!d.state.perceive; $("#t-live").checked=!!d.state.live;
  items=d.items.filter(x=> filter==="all"? true : filter==="pending"? x.verdict===null : x.verdict===filter);
  render();
}

function render(){
  const list=$("#list");
  if(!items.length){ list.innerHTML='<div class="hint">후보 없음</div>'; return; }
  if(cursor>=items.length) cursor=0;
  list.innerHTML=items.map((x,i)=>{
    const bad=x.verdict? `<span class="badge ${x.verdict}">${x.verdict==="approved"?"승인":"거부"}</span>`:"";
    const live=x.live_sent? `<span class="badge approved">전송됨${x.send_ms?` ${x.send_ms}ms`:""}</span>`:"";
    return `<div class="card ${i===cursor?"focus":""}" data-i="${i}">
      <div class="meta">${ago(x.ts)} · ${esc(x.title)} ${bad} ${live}</div>
      <div class="line">${esc(x.text)}</div>
      ${provHtml(x)}
      ${x.note?`<div class="blk"><div class="k">내 첨언</div><div class="chips"><span class="chip">${esc(x.note)}</span></div></div>`:""}
      <div class="btns">
        <input class="note" id="n-${x.message_id}" placeholder="첨언 (선택) — 예: 너무 딱딱함" ${x.verdict?"disabled":""}>
        <button class="ok" ${x.verdict?"disabled":""} onclick="vote('${x.message_id}','approved')">✅ 승인</button>
        <button class="no" ${x.verdict?"disabled":""} onclick="vote('${x.message_id}','rejected')">❌ 거부</button>
      </div>
    </div>`;
  }).join("");
}

async function loadReplays(){
  const r=await fetch("/api/replays"); const d=await r.json();
  $("#s-pend").textContent=d.stats.pending; $("#s-appr").textContent=d.stats.approved;
  $("#s-rej").textContent=d.stats.rejected; $("#s-live").textContent=d.stats.live_sent;
  const list=$("#list");
  if(!d.runs.length){ list.innerHTML='<div class="hint">리플레이 기록 없음<br>python3 replay.py --media &lt;파일|HLS 디렉터리&gt; --count 3</div>'; return; }
  list.innerHTML=d.runs.map(run=>`<div class="rp">
    <div class="t">${esc(run.name)} · 후보 ${run.ok}/${run.total}</div>
    ${run.items.map(it=>`<div class="line" style="font-size:17px">${esc(it.line||"(실패)")}</div>
      <div class="blk"><div class="k">화면</div>${ul(it.scene,"없음")}</div>
      <div class="blk"><div class="k">BJ 발화</div>${ul(it.speech,"없음")}</div>`).join("")}
  </div>`).join("");
}

async function vote(mid,v){
  const inp=document.getElementById("n-"+mid);
  const note=inp&&inp.value.trim()?inp.value.trim():null;
  const r=await fetch("/api/verdict",{method:"POST",headers:{"content-type":"application/json"},body:JSON.stringify({message_id:mid,verdict:v,note:note})});
  const d=await r.json();
  if(!d.ok){ toast(d.error||"실패"); return; }
  const it=items.find(x=>String(x.message_id)===String(mid)); if(it){ it.verdict=v; it.live_sent=d.live_sent; it.send_ms=d.send_ms; it.note=note; }
  toast(v==="approved" ? (d.live_sent===false?"승인(전송 실패)":"승인") : "거부");
  if(filter==="pending"){ items=items.filter(x=>x.verdict===null); if(cursor>=items.length) cursor=0; }
  await load();
}
document.querySelectorAll(".tab").forEach(t=>t.onclick=()=>{
  document.querySelectorAll(".tab").forEach(x=>x.classList.remove("on")); t.classList.add("on");
  filter=t.dataset.f; cursor=0; load();
});
$("#t-live").onchange=async e=>{ await fetch("/api/state",{method:"POST",headers:{"content-type":"application/json"},body:JSON.stringify({live:e.target.checked})}); toast("실제 전송 "+(e.target.checked?"ON":"OFF")); };
$("#t-perc").onchange=async e=>{ await fetch("/api/state",{method:"POST",headers:{"content-type":"application/json"},body:JSON.stringify({perceive:e.target.checked})}); toast("지각 "+(e.target.checked?"ON":"OFF")); };
$("#t-gen").onchange=async e=>{ await fetch("/api/state",{method:"POST",headers:{"content-type":"application/json"},body:JSON.stringify({enabled:e.target.checked})}); toast("생성 "+(e.target.checked?"ON":"OFF")); };
document.addEventListener("keydown",e=>{
  if(filter==="replay") return;
  if(e.key==="j"||e.key==="ArrowDown"){ cursor=Math.min(cursor+1,items.length-1); render(); }
  if(e.key==="k"||e.key==="ArrowUp"){ cursor=Math.max(cursor-1,0); render(); }
  const it=items[cursor]; if(!it) return;
  if((e.key==="a"||e.key==="A") && !it.verdict) vote(it.message_id,"approved");
  if((e.key==="r"||e.key==="R") && !it.verdict) vote(it.message_id,"rejected");
});
document.addEventListener("toggle",e=>{
  if(e.target.tagName==="DETAILS" && e.target.open){
    const pre=e.target.querySelector("pre.prompt"); if(pre) loadPrompt(pre.dataset.mid, pre);
  }
},true);
load(); setInterval(()=>{ if(filter!=="replay") load(); }, 20000);
</script></body></html>
"""


def payload():
    """카드 목록. 무거운 trace는 대기 중 카드에만 싣고, 판정 끝난 건 80장까지만 보낸다.

    예전에는 400장 전부의 trace(채팅 24줄·RAG·프롬프트 근거)를 매 20초 보내서 응답이 수백 KB였고
    콘솔이 쓸 수 없을 만큼 느려질 수 있다.
    """
    rows = store.candidates()
    pending = [r for r in rows if r.get("verdict") is None][-30:]
    decided = [r for r in rows if r.get("verdict") is not None][-80:]
    keep = []
    for r in sorted(pending + decided, key=lambda x: x.get("ts") or 0):
        is_pending = r.get("verdict") is None
        keep.append({
            "message_id": r.get("message_id"),
            "ts": r.get("ts"),
            "text": r.get("text"),
            "title": r.get("title"),
            "context": (r.get("context") or [])[-8:] if is_pending else [],
            "trace": r.get("trace") if is_pending else None,
            "verdict": r.get("verdict"),
            "note": r.get("note"),
            "send_ms": r.get("send_ms"),
            "live_sent": bool(r.get("live_sent")),
        })
    keep.reverse()
    return {"items": keep, "stats": store.stats(), "state": store.load_state(),
            "now": int(time.time())}


def replay_runs(limit=6):
    runs = []
    for d in sorted(glob.glob(os.path.join(store.BASE, "replay", "*")), reverse=True)[:limit]:
        path = os.path.join(d, "results.jsonl")
        if not os.path.exists(path):
            continue
        scen, spec = [], []
        try:
            p = json.load(open(os.path.join(d, "perception.json"), encoding="utf-8"))
            scen = [s.get("text") for s in p.get("scene", [])][-2:]
            spec = [s.get("text") for s in p.get("speech", [])][-2:]
        except Exception:
            pass
        items, total = [], 0
        for line in open(path, encoding="utf-8"):
            try:
                r = json.loads(line)
            except Exception:
                continue
            total += 1
            if r.get("line"):
                items.append({"line": r["line"], "scene": scen, "speech": spec})
        runs.append({"name": os.path.basename(d), "total": total, "ok": len(items), "items": items})
    return runs


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False).encode()
        elif isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        path, q = u.path, urllib.parse.parse_qs(u.query)
        if path == "/":
            return self._send(200, PAGE, "text/html; charset=utf-8")
        if path == "/api/data":
            return self._send(200, payload())
        if path == "/api/stats":
            return self._send(200, store.stats())
        if path == "/api/replays":
            return self._send(200, {"runs": replay_runs(), "stats": store.stats()})
        if path == "/api/prompt":
            mid = (q.get("message_id") or [""])[0]
            if not mid or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", mid):
                return self._send(400, {"ok": False, "error": "bad message_id"})
            p = os.path.join(store.BASE, "prompts", f"{mid}.txt")
            if not os.path.exists(p):
                return self._send(404, {"ok": False, "error": "프롬프트 기록 없음(구버전 후보)"})
            return self._send(200, {"ok": True, "prompt": open(p, encoding="utf-8").read()})
        return self._send(404, {"ok": False, "error": "not found"})

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        n = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            return self._send(400, {"ok": False, "error": "bad json"})
        if path == "/api/verdict":
            st = store.load_state()
            sender = live_send if st.get("live") else None
            ok, msg = store.set_verdict(body.get("message_id"), body.get("verdict"),
                                        sender=sender, note=(body.get("note") or None))
            target = None
            for r in store.candidates():
                if str(r.get("message_id")) == str(body.get("message_id")):
                    target = r
                    break
            return self._send(200 if ok else 409,
                              {"ok": ok, "error": None if ok else msg,
                               "live_sent": (target or {}).get("live_sent"),
                               "send_ms": (target or {}).get("send_ms")})
        if path == "/api/state":
            kw = {}
            for k in ("live", "enabled", "perceive"):
                if k in body:
                    kw[k] = bool(body[k])
            return self._send(200, {"ok": True, "state": store.save_state(**kw)})
        return self._send(404, {"ok": False, "error": "not found"})


def main():
    global PORT, HOST
    if "--port" in sys.argv:
        PORT = int(sys.argv[sys.argv.index("--port") + 1])
    if "--host" in sys.argv:
        HOST = sys.argv[sys.argv.index("--host") + 1]
    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"hogi-chat 콘솔 http://{HOST}:{PORT}  (base={store.BASE})", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
