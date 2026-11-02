#!/usr/bin/env python3
"""후보 판정 저장소. 콘솔(console.py)과 리액션 감시(reaction.py)가 같이 쓴다.

- candidates.jsonl 을 flock 으로 잠그고 읽고-고쳐-쓴다 (동시 쓰기 경합 방지)
- training.jsonl 은 message_id 기준으로 중복 append 를 막는다
- 승인 + live 면 실제 방송 채팅으로 보낸다 (sender 콜백)
"""

import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
import config  # noqa: E402  (repo root)
import fcntl
import json
import os
import time
import traceback

BASE = config.BASE
CAND = os.path.join(BASE, "candidates.jsonl")
TRAIN = os.path.join(BASE, "training.jsonl")
LOCK = os.path.join(BASE, ".store.lock")
LOG = os.path.join(BASE, "console.log")


def _log(msg):
    try:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(time.strftime("%Y-%m-%d %H:%M:%S") + " " + msg + "\n")
    except Exception:
        pass


class _Lock:
    def __enter__(self):
        self.fh = open(LOCK, "w")
        fcntl.flock(self.fh, fcntl.LOCK_EX)
        return self

    def __exit__(self, *a):
        fcntl.flock(self.fh, fcntl.LOCK_UN)
        self.fh.close()


def read_jsonl(path):
    out = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except Exception:
                    continue
    except FileNotFoundError:
        pass
    return out


def candidates():
    return read_jsonl(CAND)


def trainings():
    return read_jsonl(TRAIN)


def _caller():
    """판정을 기록한 호출자(파일:줄:함수)를 남긴다."""
    try:
        stack = traceback.extract_stack()
        for fr in reversed(stack[:-2]):
            if os.path.basename(fr.filename) != "store.py":
                return f"{os.path.basename(fr.filename)}:{fr.lineno}:{fr.name}"
    except Exception:
        pass
    return "?"


def _write_candidates(rows):
    tmp = CAND + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    os.replace(tmp, CAND)


def append_candidate(rec):
    """후보 한 줄 추가. 판정 기록과 같은 락을 잡아 덮어쓰기 경합을 막는다."""
    with _Lock():
        with open(os.path.join(BASE, "candidates.jsonl"), "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def set_verdict(message_id, verdict, sender=None, note=None):
    """verdict: approved | rejected. sender(text) -> bool (실제 방송 전송). note: 첨언."""
    if verdict not in ("approved", "rejected"):
        return False, "bad verdict"
    with _Lock():
        rows = candidates()
        target = None
        for r in rows:
            if str(r.get("message_id")) == str(message_id):
                target = r
                break
        if target is None:
            return False, "candidate not found"
        if target.get("verdict") == verdict:
            if note:
                target["note"] = note
                _write_candidates(rows)
            return True, "already " + verdict
        if target.get("verdict") is not None:
            return False, "already decided: " + str(target.get("verdict"))
        target["verdict"] = verdict
        target["decided_at"] = int(time.time())
        if note:
            target["note"] = note
        if verdict == "approved" and sender is not None:
            t0 = time.time()
            try:
                target["live_sent"] = bool(sender(target.get("text", "")))
            except Exception as e:
                target["live_sent"] = False
                _log(f"[LIVE 실패] {e}")
            target["send_ms"] = int((time.time() - t0) * 1000)
        _write_candidates(rows)
        seen = {str(t.get("message_id")) for t in trainings()}
        if str(message_id) not in seen:
            with open(TRAIN, "a", encoding="utf-8") as f:
                f.write(json.dumps(target, ensure_ascii=False) + "\n")
        _log(f"[{verdict}] {target.get('text')} note={note!r} live_sent={target.get('live_sent')} "
             f"{target.get('send_ms','-')}ms caller={_caller()}")
        return True, verdict


def add_note(message_id, note):
    """판정 없이 첨언만 남긴다(학습 신호·판정 힌트로 쓴다)."""
    if not note:
        return False, "empty note"
    with _Lock():
        rows = candidates()
        for r in rows:
            if str(r.get("message_id")) == str(message_id):
                r["note"] = note
                r["noted_at"] = int(time.time())
                _write_candidates(rows)
                _log(f"[첨언] {r.get('text')} :: {note}")
                return True, "ok"
        return False, "candidate not found"


def stats():
    rows = candidates()
    appr = [r for r in rows if r.get("verdict") == "approved"]
    rej = [r for r in rows if r.get("verdict") == "rejected"]
    pend = [r for r in rows if r.get("verdict") is None]
    return {
        "candidates": len(rows),
        "pending": len(pend),
        "approved": len(appr),
        "rejected": len(rej),
        "live_sent": sum(1 for r in rows if r.get("live_sent")),
        "last_ts": max((r.get("ts", 0) for r in rows), default=0),
    }


def load_state():
    try:
        return json.load(open(os.path.join(BASE, "state.json"), encoding="utf-8"))
    except Exception:
        return {}


def save_state(**kw):
    st = load_state()
    st.update(kw)
    tmp = os.path.join(BASE, "state.json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, indent=2)
    os.replace(tmp, os.path.join(BASE, "state.json"))
    _log(f"[state] {kw}")
    return st
