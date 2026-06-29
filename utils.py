"""
Utilities: HMAC signed URLs, async cleanup scheduling, and formatting helpers.

Signed-URL scheme
-----------------
A token is `"{expires_ts}.{signature}"` where
    signature = HMAC_SHA256(secret, f"{filename}:{expires_ts}")
The expiry is embedded in the token, so verification needs only the filename,
the token, and the secret. Constant-time comparison guards against timing
attacks.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import logging
import re
import time
from pathlib import Path
from urllib.parse import quote

logger = logging.getLogger("file2link.utils")

# Length of the HMAC signature kept in tokens. 20 hex chars = 80 bits, plenty
# for short-lived signed links and far shorter URLs than a full 64-char digest.
SIG_LEN = 20


# --- HMAC signed URLs -------------------------------------------------------

def _hmac_hex(message: str, secret: str) -> str:
    return hmac.new(
        secret.encode("utf-8"), message.encode("utf-8"), hashlib.sha256
    ).hexdigest()[:SIG_LEN]


def _sign(filename: str, expires_ts: int, secret: str) -> str:
    return _hmac_hex(f"{filename}:{expires_ts}", secret)


# --- Filename cleanup -------------------------------------------------------

def clean_filename(name: str) -> str:
    """Strip uploader promo tags (@handles, [..]/(..) ad blocks, site names)."""
    p = Path(name)
    stem, ext = p.stem, p.suffix
    # Remove bracketed promo blocks that contain @ (e.g. [@Series_World_TM]).
    stem = re.sub(r"[\[\(\{][^\]\)\}]*@[^\]\)\}]*[\]\)\}]", "", stem)
    # Remove leftover @handles.
    stem = re.sub(r"@\S+", "", stem)
    # Remove bare site names like Foo.com / bar.net.
    stem = re.sub(r"\b[\w-]+\.(?:com|net|org|me|io|tv|info|xyz)\b", "", stem, flags=re.I)
    # Collapse runs of separators into a single dot.
    stem = re.sub(r"[\s._\-]{2,}", ".", stem)
    stem = stem.strip(" ._-")
    if not stem:
        stem = "file"
    return stem + ext


def make_token(filename: str, secret: str, expires: int = 86400) -> str:
    """Return a token string valid for `expires` seconds from now."""
    expires_ts = int(time.time()) + int(expires)
    sig = _sign(filename, expires_ts, secret)
    return f"{expires_ts}.{sig}"


def generate_signed_url(
    filename: str, base_url: str, secret: str, expires: int = 86400
) -> str:
    """Build a full signed download URL."""
    token = make_token(filename, secret, expires)
    return f"{base_url.rstrip('/')}/download/{quote(filename)}?token={token}"


def verify_signed_url(filename: str, token: str, secret: str) -> bool:
    """Return True if the token is valid for `filename` and not expired."""
    try:
        expires_str, sig = token.split(".", 1)
        expires_ts = int(expires_str)
    except (ValueError, AttributeError):
        return False

    if time.time() >= expires_ts:
        return False

    expected = _sign(filename, expires_ts, secret)
    return hmac.compare_digest(expected, sig)


# --- Streaming tokens (single files, no download) ---------------------------
# Token encodes the Telegram message reference so the server can stream the
# file on demand. Fully stateless: payload + HMAC signature, no DB needed.

def generate_stream_url(
    filename: str,
    chat_id: int,
    message_id: int,
    size: int,
    base_url: str,
    secret: str,
    expires: int = 86400,
) -> str:
    exp = int(time.time()) + int(expires)
    payload = f"{chat_id}:{message_id}:{size}:{exp}"
    raw = base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=")
    sig = _hmac_hex(f"{filename}:{payload}", secret)
    token = f"{raw}.{sig}"
    return f"{base_url.rstrip('/')}/stream/{quote(filename)}?token={token}"


def verify_stream_token(filename: str, token: str, secret: str) -> dict | None:
    """Return {chat_id, message_id, size} if valid and unexpired, else None."""
    try:
        raw, sig = token.split(".", 1)
        pad = "=" * (-len(raw) % 4)
        payload = base64.urlsafe_b64decode(raw + pad).decode()
        chat_id, message_id, size, exp = payload.split(":")
        exp = int(exp)
    except Exception:  # noqa: BLE001
        return None

    if time.time() >= exp:
        return None

    expected = _hmac_hex(f"{filename}:{payload}", secret)
    if not hmac.compare_digest(expected, sig):
        return None

    return {"chat_id": int(chat_id), "message_id": int(message_id), "size": int(size)}


# --- Cleanup scheduling -----------------------------------------------------

async def schedule_cleanup(filepath, delay: int = 86400) -> None:
    """Delete a file (or directory) after `delay` seconds. Fire-and-forget."""
    try:
        await asyncio.sleep(delay)
        _delete_path(Path(filepath))
        logger.info("Auto-cleanup removed: %s", filepath)
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001
        logger.exception("cleanup failed for %s", filepath)


def _delete_path(path: Path) -> None:
    if not path.exists():
        return
    if path.is_dir():
        for child in sorted(path.glob("**/*"), reverse=True):
            try:
                child.unlink() if child.is_file() else child.rmdir()
            except OSError:
                pass
        try:
            path.rmdir()
        except OSError:
            pass
    else:
        try:
            path.unlink()
        except OSError:
            pass


def delete_path(path) -> None:
    """Synchronously delete a file or directory tree (used on /cancel)."""
    _delete_path(Path(path))


# --- Formatting -------------------------------------------------------------

def format_size(num_bytes: int) -> str:
    """Human readable file size, e.g. '1.2 GB'."""
    if num_bytes is None:
        return "unknown"
    size = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            if unit == "B":
                return f"{int(size)} {unit}"
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


def format_duration(seconds: float) -> str:
    """Human readable duration, e.g. '45 min' or '1 h 20 min'."""
    seconds = int(seconds or 0)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h} h {m} min"
    if m:
        return f"{m} min"
    return f"{s} sec"
