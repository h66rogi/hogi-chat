#!/usr/bin/env python3
import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
import config  # noqa: E402  (repo root)
"""시크릿 조회 우선순위: macOS 키체인(vault) → .env → 환경변수.

승인 창이 뜨지 않는다. 새 비밀값은 `python3 vault.py set <name>` 로 넣으면
코드 수정 없이 바로 반영된다.
"""
import os
import sys

SCRIPTS = os.environ.get("HOGI_VAULT_DIR", os.path.join(os.path.expanduser("~"), "hogi-chat", "vault"))
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)

try:
    import vault as _vault
except Exception:
    _vault = None

ENV_PATH = config.ENV_PATH


def _env(key):
    try:
        for line in open(ENV_PATH, encoding="utf-8"):
            if line.startswith(key + "="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    except FileNotFoundError:
        pass
    return None


def load_env():
    out = {}
    try:
        for line in open(ENV_PATH, encoding="utf-8"):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip().strip('"').strip("'")
    except FileNotFoundError:
        pass
    return out


def get(key, default=None):
    """vault(대소문자 무관) → .env → 환경변수 순으로 찾는다."""
    if _vault:
        for name in (key, key.lower(), key.upper()):
            try:
                v = _vault.get(name)
            except Exception:
                v = None
            if v:
                return v
    v = _env(key)
    if v:
        return v
    return os.environ.get(key, default)
