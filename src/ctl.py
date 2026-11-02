#!/usr/bin/env python3
"""후로기 채팅 중계 제어.

사용:
  hogi-chat on        전송 켜기 (수집·판단·디스코드 전송)
  hogi-chat off       전송 끄기 (프로세스는 살아 있고 로그만)
  hogi-chat dry       판단만 하고 전송 안 함
  hogi-chat start     수집 프로세스 시작 (macOS launchd)
  hogi-chat stop      수집 프로세스 종료
  hogi-chat status    현재 상태·최근 로그

`start`/`stop`은 ~/Library/LaunchAgents의 plist를 쓴다. 서버에서는 systemd 유닛을 쓴다.
"""

import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
import config  # noqa: E402  (repo root)
import json
import os
import subprocess
import sys
import time

HOME = os.path.expanduser("~")
BASE = config.BASE
STATE = os.path.join(BASE, "state.json")
LOG = os.path.join(BASE, "relay.log")
PLIST = os.path.join(HOME, "Library/LaunchAgents/com.example.hogi-chat.plist")
LABEL = "com.example.hogi-chat"

DEFAULT = {
    "enabled": False,
    "dry_run": True,
    "channel_id": config.CHANNEL_ID,
    "bj_id": config.BJ_ID,
}


def load():
    try:
        st = json.load(open(STATE, encoding="utf-8"))
    except Exception:
        st = dict(DEFAULT)
    for k, v in DEFAULT.items():
        st.setdefault(k, v)
    return st


def save(st):
    os.makedirs(BASE, exist_ok=True)
    json.dump(st, open(STATE, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    os.chmod(STATE, 0o600)


def running():
    r = subprocess.run(["launchctl", "print", f"gui/{os.getuid()}/{LABEL}"],
                       capture_output=True, text=True)
    return r.returncode == 0


def start():
    st = load()
    save(st)
    if running():
        print("이미 실행 중")
        return
    if not os.path.exists(PLIST):
        print("plist 없음:", PLIST)
        return
    subprocess.run(["launchctl", "bootstrap", f"gui/{os.getuid()}", PLIST], check=False)
    time.sleep(1)
    print("시작:", "ok" if running() else "실패 (로그 확인)")


def stop():
    subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}/{LABEL}"], check=False)
    print("종료")


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    st = load()
    if cmd == "on":
        st["enabled"], st["dry_run"] = True, False
        save(st)
        print("전송 ON — 수집 중이면 곧 디스코드로 갑니다")
    elif cmd == "off":
        st["enabled"] = False
        save(st)
        print("전송 OFF — 수집은 계속, 전송만 멈춤")
    elif cmd == "dry":
        st["enabled"], st["dry_run"] = True, True
        save(st)
        print("DRY — 판단 결과만 로그에 남김")
    elif cmd == "live":
        what = sys.argv[2] if len(sys.argv) > 2 else ""
        if what == "on":
            st["live"] = True
            save(st)
            print("LIVE ON — ✅ 승인하면 실제 방송 채팅으로 보냅니다")
        elif what == "off":
            st["live"] = False
            save(st)
            print("LIVE OFF — 생성문은 디스코드로만")
        else:
            print("사용: hogi-chat live on|off")
    elif cmd == "start":
        start()
    elif cmd == "stop":
        stop()
    elif cmd == "status":
        print(f"enabled={st['enabled']} dry_run={st['dry_run']} "
              f"channel={st['channel_id']} bj={st['bj_id']}")
        print("process:", "running" if running() else "stopped")
        if os.path.exists(LOG):
            print("--- 최근 로그 ---")
            for line in open(LOG, encoding="utf-8").readlines()[-8:]:
                print(line.rstrip())
    else:
        print(__doc__)
        sys.exit(1)


if __name__ == "__main__":
    main()
