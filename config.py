"""
Central configuration. Loads .env once and exposes settings + paths.
All other modules import from here so secrets/paths live in one place.
"""

from __future__ import annotations

import os
from pathlib import Path

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass


def _get_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


def _get_bool(name: str, default: bool) -> bool:
    val = os.getenv(name)
    if val is None:
        return default
    return val.strip().lower() in {"1", "true", "yes", "on"}


# --- Telegram (MTProto / Pyrogram) ------------------------------------------
# api_id / api_hash from https://my.telegram.org  ->  "API development tools".
API_ID = _get_int("API_ID", 0)
API_HASH = os.getenv("API_HASH", "")
BOT_TOKEN = os.getenv("BOT_TOKEN", "PUT-YOUR-BOT-TOKEN-HERE")
SESSION_NAME = os.getenv("SESSION_NAME", "file2link_bot")

# Restrict usage to specific Telegram user IDs (comma separated). Empty = open.
_allowed = os.getenv("ALLOWED_USER_IDS", "").strip()
ALLOWED_USER_IDS = (
    {int(x) for x in _allowed.split(",") if x.strip()} if _allowed else set()
)

# Private "log" channel where incoming files are copied for free, permanent
# storage. The bot must be an admin there. Format: -100xxxxxxxxxx. Empty =
# stream from the user's original message instead (less robust).
LOG_CHANNEL_ID = _get_int("LOG_CHANNEL_ID", 0)

# --- Download server / signed links -----------------------------------------
# Public base URL users will reach (used to build download links).
BASE_URL = os.getenv("BASE_URL", "http://127.0.0.1:8080").rstrip("/")
HTTP_HOST = os.getenv("HTTP_HOST", "0.0.0.0")
# Koyeb / most PaaS inject the listening port via $PORT; fall back to HTTP_PORT.
HTTP_PORT = _get_int("PORT", _get_int("HTTP_PORT", 8080))

# Secret used to sign/verify HMAC download tokens. CHANGE THIS in production.
SECRET_KEY = os.getenv("SECRET_KEY", "change-me-to-a-long-random-secret")

# Link / file lifetime in seconds. Default = 24 hours.
EXPIRY_SECONDS = _get_int("EXPIRY_SECONDS", 24 * 60 * 60)

# --- Storage ----------------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent
DOWNLOAD_DIR = Path(os.getenv("DOWNLOAD_DIR", BASE_DIR))

FILES_DIR = DOWNLOAD_DIR / "files"        # single uploaded files
SESSIONS_DIR = DOWNLOAD_DIR / "sessions"  # temp episodes per user
OUTPUT_DIR = DOWNLOAD_DIR / "output"      # merged output files

# --- FFmpeg / limits --------------------------------------------------------
FFMPEG_BINARY = os.getenv("FFMPEG_BINARY", "ffmpeg")
FFPROBE_BINARY = os.getenv("FFPROBE_BINARY", "ffprobe")
MAX_BATCH_SIZE = _get_int("MAX_BATCH_SIZE", 50)

# Telegram's hard ceiling: 4 GB per file (files >2 GB must be uploaded by a
# Premium user). MTProto can download up to this limit.
MAX_FILE_BYTES = _get_int("MAX_FILE_BYTES", 4 * 1024 * 1024 * 1024)


# --- Cloudflare R2 (zero-egress storage for merged files) -------------------
# If enabled, merged files are uploaded to R2 and served via presigned URLs,
# so user downloads cost no egress. Create a bucket + API token in the
# Cloudflare dashboard (R2 -> Manage R2 API Tokens).
R2_ENABLED = _get_bool("R2_ENABLED", False)
R2_ACCOUNT_ID = os.getenv("R2_ACCOUNT_ID", "")
R2_ACCESS_KEY_ID = os.getenv("R2_ACCESS_KEY_ID", "")
R2_SECRET_ACCESS_KEY = os.getenv("R2_SECRET_ACCESS_KEY", "")
R2_BUCKET = os.getenv("R2_BUCKET", "")

# If true, single files are also stored on R2 (download once -> upload -> serve
# free from R2). Best for popular files downloaded by many users. If false,
# single files use instant on-demand streaming (per-download host egress).
R2_SINGLE_FILES = _get_bool("R2_SINGLE_FILES", False)


# Ensure directories exist.
for _d in (FILES_DIR, SESSIONS_DIR, OUTPUT_DIR):
    _d.mkdir(parents=True, exist_ok=True)
