#!/usr/bin/env python3
"""내 과거 채팅을 모아 말투 시드(style_seed.jsonl)를 만든다.

두 입력을 지원한다.

1) JSONL 파일 (각 줄: {"user_id": ..., "nick": ..., "text": ..., "t": ...})
   python3 scripts/build_style_seed.py --jsonl chats.jsonl --user-id me

2) PostgreSQL 쿼리 결과 (표준입력으로 TAB 구분: 시각, user_id, text)
   psql -tA -F $'\\t' -c "select created_at, user_id, message_text from chat where user_id='me'" \\
     | python3 scripts/build_style_seed.py --tsv --user-id me

결과는 `HOGI_BASE/style_seed.jsonl`에 `{"t": "YYYY-MM-DD HH:MM:SS", "text": "..."}`로 쌓인다.
AI가 대신 보낸 문장(후보 승인분)은 넣지 않는다. 말투 시드는 내가 실제로 친 말이어야 한다.
"""
import argparse
import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import config  # noqa: E402

OUT = os.path.join(config.BASE, "style_seed.jsonl")

# 말투 통계를 흐리는 줄: 명령어, 한 글자, 반복 도배, URL
DROP = re.compile(r"^\s*$|^[/!.]\s*\w|^[ㄱ-ㅎㅏ-ㅣ]$|(.)\1{6,}|https?://")


def clean(text):
    t = (text or "").replace("\r", "").strip()
    if len(t) < 2 or DROP.search(t):
        return None
    return t


def collect(rows, user_id=None, nick=None):
    """rows: (t, user_id, nick, text) 튜플 목록. 내 발화만 골라 중복을 제거한다."""
    seen, out = set(), []
    for t, uid, nk, text in rows:
        if user_id and uid and uid != user_id:
            continue
        if nick and not user_id and nk != nick:
            continue
        c = clean(text)
        if not c or c in seen:
            continue
        seen.add(c)
        out.append({"t": t, "text": c})
    return out


def read_jsonl(path):
    rows = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            d = json.loads(line)
            rows.append((d.get("t") or d.get("ts") or "", d.get("user_id"), d.get("nick"), d.get("text")))
    return rows


def read_tsv():
    rows = []
    for line in sys.stdin:
        parts = line.rstrip("\n").split("\t")
        if len(parts) < 3:
            continue
        rows.append((parts[0], parts[1], "", parts[2]))
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--jsonl")
    ap.add_argument("--tsv", action="store_true")
    ap.add_argument("--user-id")
    ap.add_argument("--nick")
    args = ap.parse_args()

    if args.jsonl:
        rows = read_jsonl(args.jsonl)
    elif args.tsv:
        rows = read_tsv()
    else:
        ap.error("--jsonl 또는 --tsv 중 하나가 필요하다")

    seed = collect(rows, args.user_id, args.nick)
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "a", encoding="utf-8") as f:
        for rec in seed:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    print(f"appended {len(seed)} lines → {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
