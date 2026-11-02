#!/usr/bin/env python3
import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
import config  # noqa: E402  (repo root)
"""비슷한 상황 검색(RAG): '그 직전 방 상황 → 운영자가 실제로 친 말' 쌍에서 지금과 닮은 순간을 찾는다.

연구 노트(research-note.md) B단계. 2026-10-24 운영자 지시("연구결과 다시 봐라, 근본 방식 변화")로 도입.
- 데이터: rogimarble 백업 덤프의 collector_chat_inbox(방 전체 채팅) + 수집기 recent.jsonl(백업 이후분).
- 쌍: 운영자 채팅 한 줄마다 직전 90초 안의 다른 사람 채팅(최대 12줄)을 상황으로 붙인다.
- AI가 대신 보낸 줄(candidates.jsonl에서 승인·전송된 문장)은 운영자 발화에서 뺀다(자기 복제 방지).
- 색인: OpenAI text-embedding-3-small 256차원. 상황 텍스트만 임베딩한다.

사용: python3 retrieval.py build <chat.sql>   # 쌍 만들기 + 임베딩
      python3 retrieval.py query "지금 상황 텍스트"
"""
import json
import os
import sys
import time
import urllib.request

import numpy as np

BASE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(BASE, "data")
MOMENTS = os.path.join(DATA, "moments.jsonl")
EMB = os.path.join(DATA, "moments_emb.npy")
EMB_MODEL = "text-embedding-3-small"
DIMS = 256
SELF_IDS = set(config.SELF_IDS)
SELF_NICKS = set(config.SELF_NICKS)
WINDOW = 90
MAX_CTX = 12


def env(name):
    v = os.environ.get(name)
    if v:
        return v
    try:
        for line in open(config.ENV_PATH, encoding="utf-8"):
            if line.startswith(name + "="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    except FileNotFoundError:
        pass
    return ""


def embed(texts):
    """여러 텍스트를 256차원 단위벡터로."""
    out = []
    for i in range(0, len(texts), 256):
        body = json.dumps({"model": EMB_MODEL, "input": [t[:2000] or "." for t in texts[i:i + 256]],
                           "dimensions": DIMS}).encode()
        req = urllib.request.Request("https://api.openai.com/v1/embeddings", data=body,
                                     headers={"Authorization": "Bearer " + env("OPENAI_API_KEY"),
                                              "Content-Type": "application/json"})
        d = json.loads(urllib.request.urlopen(req, timeout=120).read())
        out += [x["embedding"] for x in sorted(d["data"], key=lambda x: x["index"])]
        try:
            tok = (d.get("usage") or {}).get("total_tokens") or 0
            with open(os.path.join(BASE, "usage.jsonl"), "a", encoding="utf-8") as f:
                f.write(json.dumps({"ts": int(time.time()), "ym": time.strftime("%Y-%m"), "kind": "embed",
                                    "model": EMB_MODEL, "cost": round(tok * 0.02 / 1e6, 6), "in": tok}) + "\n")
        except Exception:
            pass
    a = np.array(out, dtype=np.float32)
    a /= np.linalg.norm(a, axis=1, keepdims=True) + 1e-9
    return a


def _ts(s):
    import datetime
    s = s + "00" if s.endswith("+00") else s
    for fmt in ("%Y-%m-%d %H:%M:%S.%f%z", "%Y-%m-%d %H:%M:%S%z"):
        try:
            return datetime.datetime.strptime(s, fmt).timestamp()
        except ValueError:
            continue
    return None


def load_dump(path):
    rows, seen, inside = [], set(), False
    for line in open(path, encoding="utf-8"):
        if line.startswith("COPY public.collector_chat_inbox"):
            inside = True
            continue
        if not inside:
            continue
        if line.startswith("\\."):
            break
        p = line.rstrip("\n").split("\t")
        if len(p) < 11 or p[4] in seen:
            continue
        seen.add(p[4])
        try:
            pl = json.loads(p[8].replace("\\\\", "\\"))
        except Exception:
            continue
        t = _ts(p[10])
        msg = (pl.get("message") or "").strip()
        if t and msg:
            rows.append({"t": t, "uid": pl.get("userId") or "", "nick": pl.get("userDisplayName") or "",
                         "text": msg})
    return rows


def load_recent(after):
    rows = []
    try:
        for line in open(os.path.join(BASE, "recent.jsonl"), encoding="utf-8"):
            try:
                d = json.loads(line)
            except Exception:
                continue
            if d.get("t", 0) > after and (d.get("text") or "").strip():
                uid = (d.get("uid") or "").split("(")[0]
                if not uid and d.get("nick") in SELF_NICKS:
                    uid = (config.SELF_IDS or ("self",))[0]
                rows.append({"t": float(d["t"]), "uid": uid, "nick": d.get("nick", ""), "text": d["text"].strip()})
    except FileNotFoundError:
        pass
    return rows


def ai_sent_texts():
    """AI 후보로 승인·전송된 문장. 운영자 실제 발화에서 제외한다."""
    out = set()
    for name in ("candidates.jsonl", "sent.jsonl"):
        try:
            for line in open(os.path.join(BASE, name), encoding="utf-8"):
                try:
                    d = json.loads(line)
                except Exception:
                    continue
                if name == "sent.jsonl" or d.get("verdict") == "approved":
                    if d.get("text"):
                        out.add(d["text"].strip())
        except FileNotFoundError:
            pass
    return out


def is_self(r):
    return r["uid"] in SELF_IDS or r["nick"] in SELF_NICKS


def build(dump_path):
    rows = load_dump(dump_path)
    last = max(r["t"] for r in rows)
    rows += load_recent(last)
    rows.sort(key=lambda r: r["t"])
    ai = ai_sent_texts()
    moments = []
    for i, r in enumerate(rows):
        if not is_self(r) or r["text"] in ai or r["text"] in (".", ".."):
            continue
        ctx = []
        j = i - 1
        while j >= 0 and r["t"] - rows[j]["t"] <= WINDOW and len(ctx) < MAX_CTX:
            if not is_self(rows[j]):
                ctx.append(rows[j]["text"])
            j -= 1
        if not ctx:
            continue
        ctx.reverse()
        # 같은 순간에 연달아 친 줄은 앞 줄도 함께 기억한다(연타 반응 재현용)
        moments.append({"t": r["t"], "reply": r["text"], "ctx": ctx})
    os.makedirs(DATA, exist_ok=True)
    with open(MOMENTS, "w", encoding="utf-8") as f:
        for m in moments:
            f.write(json.dumps(m, ensure_ascii=False) + "\n")
    emb = embed([" / ".join(m["ctx"]) for m in moments])
    np.save(EMB, emb)
    print(f"방 채팅 {len(rows)}줄, 운영자 발화 {sum(1 for r in rows if is_self(r))}줄, "
          f"AI 전송분 제외 후 상황 쌍 {len(moments)}개, 임베딩 {emb.shape}")


_CACHE = {}


def _load():
    st = os.path.getmtime(EMB)
    if _CACHE.get("mtime") != st:
        _CACHE["moments"] = [json.loads(l) for l in open(MOMENTS, encoding="utf-8")]
        _CACHE["emb"] = np.load(EMB)
        _CACHE["mtime"] = st
    return _CACHE["moments"], _CACHE["emb"]


def query(text, k=10, qvec=None):
    """지금 상황과 닮은 과거 순간 k개. (유사도, 순간) 목록과 질의 벡터를 돌려준다."""
    moments, emb = _load()
    q = qvec if qvec is not None else embed([text])[0]
    sims = emb @ q
    top = np.argsort(-sims)[: k * 3]
    out, seen = [], set()
    for i in top:
        m = moments[int(i)]
        if m["reply"] in seen:
            continue
        seen.add(m["reply"])
        out.append((float(sims[int(i)]), m))
        if len(out) >= k:
            break
    return out, q


if __name__ == "__main__":
    if sys.argv[1] == "build":
        build(sys.argv[2])
    elif sys.argv[1] == "query":
        res, _ = query(sys.argv[2], k=int(sys.argv[3]) if len(sys.argv) > 3 else 10)
        for s, m in res:
            print(f"{s:.3f} | {m['reply']} | 상황: {' / '.join(m['ctx'][-4:])[:120]}")
