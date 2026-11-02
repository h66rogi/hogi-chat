#!/usr/bin/env python3
"""Discord 버튼 엔드포인트용 Cloudflare quick tunnel 감시자 (서버에서 실행).

quick tunnel URL은 cloudflared가 다시 뜰 때마다 바뀐다. 그래서 이 감시자가
1) cloudflared(도커)를 띄우고 2) 새 URL을 읽어 3) 게이트웨이 PING이 터널로 통하는지 확인한 뒤
4) Discord `PATCH /applications/@me` 로 Interactions Endpoint URL을 스스로 갱신한다.
포털에 손으로 들어갈 필요가 없다. cloudflared가 죽으면 다시 띄우고 같은 절차를 반복한다.
"""

import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))))
import config  # noqa: E402  (repo root)
import json
import os
import re
import subprocess
import time
import urllib.error
import urllib.request

BASE = os.path.dirname(os.path.abspath(__file__))
LOG = os.path.join(BASE, "tunnel.log")
STATE = os.path.join(BASE, "tunnel_url.txt")
IMAGE = os.environ.get("HOGI_CLOUDFLARED_IMAGE", "cloudflare/cloudflared:2026.10.3")
TARGET = os.environ.get("HOGI_GATEWAY_URL", "http://127.0.0.1:8793")
NAME = "hogi-tunnel"
URL_RE = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")


def log(msg):
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + msg + "\n")


def env(key):
    v = os.environ.get(key)
    if v:
        return v
    with open(config.ENV_PATH, encoding="utf-8") as f:
        for line in f:
            if line.startswith(key + "="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    return ""


def reachable(url, tries=30):
    """터널을 거쳐 게이트웨이가 응답하는지(GET 200) 본다. DNS 전파에 몇 초 걸린다."""
    for _ in range(tries):
        try:
            req = urllib.request.Request(url + "/", headers={"User-Agent": "hogi-tunnel/1.0"})
            if urllib.request.urlopen(req, timeout=8).status == 200:
                return True
        except Exception:
            pass
        time.sleep(3)
    return False


def set_discord_endpoint(url):
    body = json.dumps({"interactions_endpoint_url": url + "/"}).encode()
    req = urllib.request.Request("https://discord.com/api/v10/applications/@me", data=body,
                                 method="PATCH",
                                 headers={"Authorization": "Bot " + env("DISCORD_HOGI_BOT_TOKEN"),
                                          "Content-Type": "application/json",
                                          "User-Agent": "DiscordBot (hogi-tunnel, 1.0)"})
    try:
        d = json.loads(urllib.request.urlopen(req, timeout=30).read())
        return True, d.get("interactions_endpoint_url")
    except urllib.error.HTTPError as e:
        return False, f"{e.code} {e.read().decode(errors='ignore')[:200]}"


def run_once():
    subprocess.run(["docker", "rm", "-f", NAME], capture_output=True)
    p = subprocess.Popen(["docker", "run", "--rm", "--name", NAME, "--network", "host", IMAGE,
                          "tunnel", "--no-autoupdate", "--url", TARGET],
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    url = None
    for line in p.stdout:
        if url is None:
            m = URL_RE.search(line)
            if m:
                url = m.group(0)
                log(f"[터널] {url}")
                if not reachable(url):
                    log("[터널] 게이트웨이까지 닿지 않음, 재시작")
                    p.terminate()
                    break
                ok, info = set_discord_endpoint(url)
                log(f"[Discord 엔드포인트] {'갱신' if ok else '실패'}: {info}")
                if ok:
                    with open(STATE, "w", encoding="utf-8") as f:
                        f.write(url + "\n")
                else:
                    p.terminate()
                    break
    p.wait()
    log(f"[터널 종료] exit={p.returncode}")


def main():
    log("=== tunnel 감시 시작 ===")
    while True:
        try:
            run_once()
        except Exception as e:
            log(f"[오류] {type(e).__name__}: {e}")
        time.sleep(10)


if __name__ == "__main__":
    main()
