#!/usr/bin/env python3
import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
import config  # noqa: E402  (repo root)
"""Aside 프로필의 sooplive AuthTicket/UserTicket 쿠키를 읽어 파일로 저장한다.

값은 출력하지 않고, 0600 파일에만 쓴다. 채팅 WebSocket 로그인에 쓰기 위한 준비 단계.
"""
import glob
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import tempfile

OUT = os.path.join(os.path.expanduser("~"),
                   "hogi-chat/.soop_auth.json")


def get_key():
    pw = subprocess.run(["security", "find-generic-password", "-s", "Aside Safe Storage", "-w"],
                        capture_output=True, text=True)
    if pw.returncode != 0 or not pw.stdout.strip():
        return None
    return hashlib.pbkdf2_hmac("sha1", pw.stdout.strip().encode(), b"saltysalt", 1003, 16)


def decrypt(enc, key):
    from Crypto.Cipher import AES
    if enc[:3] not in (b"v10", b"v11"):
        return enc.decode("utf-8", "replace")
    dec = AES.new(key, AES.MODE_CBC, b" " * 16).decrypt(enc[3:])
    pad = dec[-1] if dec else 0
    if 0 < pad <= 16:
        dec = dec[:-pad]
    # Chromium 130+ 는 복호화 결과 앞에 SHA256(host_key) 32바이트를 붙인다.
    if len(dec) > 32:
        try:
            dec[:32].decode("ascii")
        except Exception:
            dec = dec[32:]
    return dec.decode("utf-8", "replace")


def main():
    key = get_key()
    if not key:
        print("keychain_unavailable")
        return 1
    found = {}
    for src in glob.glob(os.path.expanduser("~/Library/Application Support/Aside/*/Cookies")):
        tmp = os.path.join(tempfile.gettempdir(), "ck_read.db")
        shutil.copy2(src, tmp)
        con = sqlite3.connect(tmp)
        for name, enc in con.execute(
                "select name, encrypted_value from cookies "
                "where host_key like '%sooplive%' and name in ('AuthTicket','UserTicket','BbsTicket')"):
            if name not in found:
                try:
                    found[name] = decrypt(enc, key)
                except Exception as e:
                    print("decrypt_error", name, type(e).__name__, str(e)[:120])
        con.close()
        os.remove(tmp)
        if len(found) >= 3:
            break
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(found, f)
    os.chmod(OUT, 0o600)
    print("saved", sorted(found.keys()), "| lengths",
          {k: len(v) for k, v in found.items()})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
