"""환경설정 — 모든 값은 환경변수로 덮어쓴다.

개인 식별자(BJ id, 계정 닉네임, Discord 채널)는 코드에 넣지 않는다.
`.env.example`을 복사해 `.env`를 만들고 값을 채운다. `.env`는 git에 올리지 않는다.
"""
import os
import re

HOME = os.path.expanduser("~")

# 데이터·상태 파일이 쌓이는 디렉터리 (로그, 후보, 판정, 전사, 캐시)
BASE = os.environ.get("HOGI_BASE") or os.path.join(HOME, "hogi-chat")

# 시크릿·토큰을 읽는 .env 경로
ENV_PATH = os.environ.get("HOGI_ENV") or os.path.join(BASE, ".env")

# 방송 채널 (SOOP BJ id)
BJ_ID = os.environ.get("HOGI_BJ_ID", "h66rogi")

# 채팅에서 BJ로 취급할 표시 닉네임들
BJ_NICKS = tuple(x.strip() for x in
                 os.environ.get("HOGI_BJ_NICKS", "후로기,후록,후로7I").split(",") if x.strip())

# 내 계정(로그인 계정)의 userId와 표시 닉네임들
SELF_IDS = tuple(x.strip() for x in os.environ.get("HOGI_SELF_IDS", "").split(",") if x.strip())
SELF_NICKS = tuple(x.strip() for x in os.environ.get("HOGI_SELF_NICKS", "").split(",") if x.strip())
# 프롬프트에서 시청자에게 보이는 내 이름 (비어 있으면 첫 닉네임)
SELF_DISPLAY = os.environ.get("HOGI_SELF_DISPLAY") or (SELF_NICKS[0] if SELF_NICKS else "me")

# 운영자 표기 (프롬프트·주석에 쓰는 이름). 특정 개인 이름을 쓰지 않는다.
OWNER_NAME = os.environ.get("HOGI_OWNER_NAME", "운영자")

# Discord: 후보 카드를 올릴 채널, 판정을 인정할 운영자 계정
CHANNEL_ID = os.environ.get("HOGI_CHANNEL_ID", "")
OWNER_DISCORD_ID = os.environ.get("HOGI_OWNER_DISCORD_ID", "")

# 생성·판정 모델 기본값 (코드에서 개별 override 가능)
MODEL = os.environ.get("HOGI_MODEL", "anthropic/claude-opus-5-5")
JUDGE_MODEL = os.environ.get("HOGI_JUDGE_MODEL", "anthropic/claude-opus-5.5")

# 화면 묘사 금지어 등 방송 규칙에서 참조하는 값
STYLE_SEED_FILE = os.path.join(BASE, "style_seed.jsonl")


def address_re():
    """내 닉네임을 부르는 표현을 잡는 정규식. 닉네임이 없으면 아무것도 잡지 않는다."""
    keys = [re.escape(n) for n in SELF_NICKS if len(n) >= 2]
    if not keys:
        return re.compile(r"(?!)")
    return re.compile("|".join(keys))


def env(key, default=None):
    """환경변수 → .env 순으로 읽는다. (시크릿은 secretsrc가 키체인까지 본다.)"""
    v = os.environ.get(key)
    if v:
        return v
    try:
        with open(ENV_PATH, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line.startswith(key + "="):
                    return line.split("=", 1)[1].strip().strip('"').strip("'")
    except FileNotFoundError:
        pass
    return default
