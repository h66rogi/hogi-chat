#!/usr/bin/env python3
import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
import config  # noqa: E402  (repo root)
"""마지막 후보 카드의 작성 봇과 버튼을 Discord API로 확인한다."""
import json
import os
import sys
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import compose  # noqa: E402
import store  # noqa: E402

rows = [r for r in store.candidates() if r.get("message_id")]
ch = store.load_state()["channel_id"]
tok = compose.secret("DISCORD_HOGI_BOT_TOKEN")
for r in rows[-int(sys.argv[1] if len(sys.argv) > 1 else 3):]:
    req = urllib.request.Request(f"https://discord.com/api/v10/channels/{ch}/messages/{r['message_id']}",
                                 headers={"Authorization": "Bot " + tok, "User-Agent": "DiscordBot (hogi,1.0)"})
    m = json.loads(urllib.request.urlopen(req, timeout=15).read())
    labels = [c.get("label") for row in m.get("components", []) for c in row.get("components", [])]
    print(r.get("text"), "| author:", m["author"]["username"], "| buttons:", labels, "| verdict:", r.get("verdict"))
