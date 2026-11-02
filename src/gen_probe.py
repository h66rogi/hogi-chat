#!/usr/bin/env python3
"""후보 생성만 N번 돌려 본다(Discord·방송에는 아무것도 보내지 않음). 모델 비교용."""

import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
import config  # noqa: E402  (repo root)
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import compose  # noqa: E402

n = int(sys.argv[1]) if len(sys.argv) > 1 else 3
print("model:", compose.MODEL)
chats = compose.recent_chats()
print("chats:", len(chats), "| speech:", [s.get("text", "")[:40] for s in compose.recent_perception().get("speech", [])][-2:])
for i in range(n):
    t0 = time.time()
    line, prompt, why, ctx = compose.generate(chats)
    print(f"{i + 1}. {line!r} | {why} | {time.time() - t0:.1f}s")
