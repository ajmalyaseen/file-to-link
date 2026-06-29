"""
File-to-Link Telegram bot (Pyrogram / MTProto).

Single-file mode (default):
    Send any video/document  -> bot downloads it, replies with an HMAC-signed
    download link that expires in 24h. File auto-deleted after expiry.

Series merge mode:
    /newmerge  -> start a merge session
    send files -> queued as episodes (confirmed one by one)
    /merge     -> ffmpeg concat (-c copy) + chapter markers -> one signed link
    /cancel    -> abort session, delete temp files
"""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from pyrogram import Client, filters, idle
from pyrogram.types import Message

import config
import merger
import r2
import utils
import server
from server import build_server

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logging.getLogger("pyrogram").setLevel(logging.WARNING)
logger = logging.getLogger("file2link.bot")

# user_id -> {"files": [Path, ...], "dir": Path}
merge_sessions: Dict[int, dict] = {}

app = Client(
    config.SESSION_NAME,
    api_id=config.API_ID,
    api_hash=config.API_HASH,
    bot_token=config.BOT_TOKEN,
    workdir=str(config.BASE_DIR),
)


# --- helpers ----------------------------------------------------------------

def _authorized(user_id: int) -> bool:
    if not config.ALLOWED_USER_IDS:
        return True
    return user_id in config.ALLOWED_USER_IDS


def _media_info(message: Message) -> Optional[Tuple[str, int]]:
    """Return (file_name, file_size) for the media in a message, else None."""
    if message.document:
        d = message.document
        return (d.file_name or f"file_{d.file_unique_id}", d.file_size or 0)
    if message.video:
        v = message.video
        return (v.file_name or f"video_{v.file_unique_id}.mp4", v.file_size or 0)
    if message.audio:
        a = message.audio
        return (a.file_name or f"audio_{a.file_unique_id}.mp3", a.file_size or 0)
    return None


def _make_progress(status: Message, label: str):
    """Throttled progress callback: shows %, size, speed and ETA."""
    state = {"last": 0.0, "start": time.time()}

    async def progress(current: int, total: int) -> None:
        now = time.time()
        if not total:
            return
        if now - state["last"] < 3 and current != total:
            return
        state["last"] = now
        pct = current * 100 // total
        elapsed = max(now - state["start"], 0.001)
        speed = current / elapsed  # bytes/sec
        eta = (total - current) / speed if speed > 0 else 0
        try:
            await status.edit_text(
                f"{label}\n"
                f"{pct}%  ({utils.format_size(current)} / {utils.format_size(total)})\n"
                f"⚡ {utils.format_size(int(speed))}/s  ⏱ ETA {utils.format_duration(eta)}"
            )
        except Exception:  # noqa: BLE001 (ignore "message not modified")
            pass

    return progress


def _link_message(title: str, size: int, url: str) -> str:
    hours = config.EXPIRY_SECONDS // 3600
    return (
        f"🎬 {title}\n"
        f"📦 Size: {utils.format_size(size)}\n"
        f"⏱ Expires in: {hours} hours\n"
        f"🔗 {url}"
    )


# --- commands ---------------------------------------------------------------

@app.on_message(filters.command(["start", "help"]) & filters.private)
async def cmd_start(client: Client, message: Message) -> None:
    if not _authorized(message.from_user.id):
        await message.reply_text("Sorry, you're not authorized to use this bot.")
        return
    await message.reply_text(
        "👋 **File-to-Link Bot**\n\n"
        "I turn Telegram files into direct download links (up to ~2 GB each).\n\n"
        "**📁 Single file mode** (default)\n"
        "Just send me any video or document. I'll reply with a download link "
        "that works in any browser and expires in 24 hours.\n\n"
        "**🎬 Series merge mode**\n"
        "1. /newmerge — start a merge session\n"
        "2. Send your episodes one by one\n"
        "3. /merge — I merge them into one file (with chapter markers) and "
        "send a single download link\n"
        "4. /cancel — abort and delete temp files\n\n"
        "Every link auto-expires after 24 hours and the file is cleaned up."
    )


@app.on_message(filters.command("newmerge") & filters.private)
async def cmd_newmerge(client: Client, message: Message) -> None:
    uid = message.from_user.id
    if not _authorized(uid):
        return
    if uid in merge_sessions:
        n = len(merge_sessions[uid]["files"])
        await message.reply_text(
            f"⚠️ You already have an active merge session with {n} episode(s).\n"
            "Send /merge to finish it, or /cancel to start over."
        )
        return

    session_dir = config.SESSIONS_DIR / str(uid)
    utils.delete_path(session_dir)  # clear any stale leftovers
    session_dir.mkdir(parents=True, exist_ok=True)
    merge_sessions[uid] = {"files": [], "dir": session_dir}

    await message.reply_text(
        "🎬 **Merge session started!**\n\n"
        "Send your episode files one by one (in order). I'll confirm each one.\n"
        "When you're done, send /merge.\n"
        "Send /cancel anytime to abort."
    )


@app.on_message(filters.command("cancel") & filters.private)
async def cmd_cancel(client: Client, message: Message) -> None:
    uid = message.from_user.id
    session = merge_sessions.pop(uid, None)
    if session is None:
        await message.reply_text("No active merge session to cancel.")
        return
    utils.delete_path(session["dir"])
    await message.reply_text("❌ Merge session cancelled and temp files deleted.")


@app.on_message(filters.command("merge") & filters.private)
async def cmd_merge(client: Client, message: Message) -> None:
    uid = message.from_user.id
    if not _authorized(uid):
        return

    session = merge_sessions.get(uid)
    if session is None:
        await message.reply_text(
            "No active merge session. Send /newmerge first, then your episodes."
        )
        return

    files: List[Path] = session["files"]
    if len(files) < 2:
        await message.reply_text(
            "⚠️ You need at least 2 episodes to merge. "
            "Send more files, or /cancel."
        )
        return

    status = await message.reply_text(f"Merging {len(files)} episodes... ⚙️")
    try:
        merged_path = await merger.merge_videos(uid, files)
        size = merged_path.stat().st_size

        if r2.is_configured():
            await status.edit_text("☁️ Uploading merged file to storage...")
            key = f"{uid}_{int(time.time())}_merged.mkv"
            await r2.upload(merged_path, key)
            url = await r2.presigned_url(key, config.EXPIRY_SECONDS)
            # Local copy no longer needed; R2 serves the download.
            utils.delete_path(merged_path)
            asyncio.create_task(r2.schedule_delete(key, config.EXPIRY_SECONDS))
        else:
            url = utils.generate_signed_url(
                merged_path.name, config.BASE_URL, config.SECRET_KEY,
                config.EXPIRY_SECONDS,
            )
            asyncio.create_task(
                utils.schedule_cleanup(merged_path, config.EXPIRY_SECONDS)
            )

        # Free disk: temp episodes no longer needed once merged.
        utils.delete_path(session["dir"])
        merge_sessions.pop(uid, None)

        await status.edit_text(
            "✅ Done! Your merged series is ready 🎬\n\n"
            + _link_message(f"{uid}_merged.mkv", size, url),
            disable_web_page_preview=True,
        )
    except merger.MergeError as exc:
        logger.exception("merge failed")
        await status.edit_text(f"❌ Merge failed.\n\n{exc}")
    except Exception as exc:  # noqa: BLE001
        logger.exception("merge error")
        await status.edit_text(f"❌ Something went wrong during merge: {exc}")


# --- media ------------------------------------------------------------------

@app.on_message((filters.document | filters.video | filters.audio) & filters.private)
async def on_media(client: Client, message: Message) -> None:
    uid = message.from_user.id
    if not _authorized(uid):
        await message.reply_text("Sorry, you're not authorized to use this bot.")
        return

    info = _media_info(message)
    if info is None:
        return
    file_name, file_size = info

    if file_size and file_size > config.MAX_FILE_BYTES:
        await message.reply_text(
            f"⚠️ That file is {utils.format_size(file_size)}, which is over the "
            f"{utils.format_size(config.MAX_FILE_BYTES)} limit."
        )
        return

    if uid in merge_sessions:
        await _handle_episode(client, message, uid, file_name)
    else:
        await _handle_single(client, message, uid, file_name, file_size)


async def _handle_episode(client: Client, message: Message, uid: int, file_name: str) -> None:
    session = merge_sessions[uid]
    if len(session["files"]) >= config.MAX_BATCH_SIZE:
        await message.reply_text(
            f"Session is full (max {config.MAX_BATCH_SIZE}). Send /merge."
        )
        return

    n = len(session["files"]) + 1
    ext = Path(file_name).suffix or ".mkv"
    dest = session["dir"] / f"ep_{n:03d}{ext}"

    status = await message.reply_text(f"⏬ Downloading episode {n}...")
    try:
        await client.download_media(
            message, file_name=str(dest),
            progress=_make_progress(status, f"⏬ Downloading episode {n}"),
        )
        session["files"].append(dest)
        await status.edit_text(
            f"Episode {n} received ✅\n"
            f"Total episodes: {n}\n"
            "Send the next one, or /merge when done."
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("episode download failed")
        await status.edit_text(f"❌ Failed to download episode {n}: {exc}")


async def _handle_single(
    client: Client, message: Message, uid: int, file_name: str, file_size: int
) -> None:
    """Single-file mode: instant streaming link, or R2 storage if enabled."""
    if not file_size:
        await message.reply_text(
            "⚠️ Couldn't read this file's size, so I can't make a link. "
            "Try sending it as a document."
        )
        return

    safe_name = Path(file_name).name

    # R2 path: download once, upload to R2, serve free from R2 forever.
    if r2.is_configured() and config.R2_SINGLE_FILES:
        await _handle_single_r2(client, message, uid, safe_name, file_size)
        return

    # Streaming path: instant link, stream from Telegram on demand.
    src_chat_id = message.chat.id
    src_message_id = message.id
    if config.LOG_CHANNEL_ID:
        try:
            stored = await message.copy(config.LOG_CHANNEL_ID)
            src_chat_id = config.LOG_CHANNEL_ID
            src_message_id = stored.id
        except Exception:  # noqa: BLE001
            logger.exception("copy to log channel failed; using original message")

    url = utils.generate_stream_url(
        safe_name,
        src_chat_id,
        src_message_id,
        file_size,
        config.BASE_URL,
        config.SECRET_KEY,
        config.EXPIRY_SECONDS,
    )
    await message.reply_text(
        "Here's your download link 🎬\n\n" + _link_message(safe_name, file_size, url),
        disable_web_page_preview=True,
    )


async def _handle_single_r2(
    client: Client, message: Message, uid: int, safe_name: str, file_size: int
) -> None:
    status = await message.reply_text(
        f"⏬ Preparing your link ({utils.format_size(file_size)})... "
        "this takes a moment the first time."
    )
    disk_name = f"{int(time.time())}_{uid}_{safe_name}"
    dest = config.FILES_DIR / disk_name
    key = f"single/{disk_name}"
    try:
        await client.download_media(
            message, file_name=str(dest),
            progress=_make_progress(status, "⏬ Downloading"),
        )
        await status.edit_text("☁️ Uploading to storage...")
        await r2.upload(dest, key)
        url = await r2.presigned_url(key, config.EXPIRY_SECONDS)
        utils.delete_path(dest)
        asyncio.create_task(r2.schedule_delete(key, config.EXPIRY_SECONDS))
        await status.edit_text(
            "Here's your download link 🎬\n\n" + _link_message(safe_name, file_size, url),
            disable_web_page_preview=True,
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("single-file R2 failed")
        utils.delete_path(dest)
        await status.edit_text(f"❌ Couldn't process that file: {exc}")


# --- entrypoint -------------------------------------------------------------

async def main() -> None:
    if not config.API_ID or not config.API_HASH:
        raise SystemExit(
            "API_ID / API_HASH not set. Get them from https://my.telegram.org "
            "and put them in your .env."
        )
    if config.BOT_TOKEN.startswith("PUT-YOUR"):
        raise SystemExit("BOT_TOKEN not set. Put it in your .env.")
    if config.SECRET_KEY == "change-me-to-a-long-random-secret":
        logger.warning("SECRET_KEY is still the default — change it in .env!")

    # Start the HTTP server first so the port is open for platform health
    # checks, then log in to Telegram and wire the client into the server.
    srv = build_server()
    server_task = asyncio.create_task(srv.serve())
    await app.start()
    server.tg_client = app
    logger.info("Bot started. Download server on %s:%d", config.HTTP_HOST, config.HTTP_PORT)
    logger.info("Public base URL: %s", config.BASE_URL)

    try:
        await idle()
    finally:
        srv.should_exit = True
        await server_task
        await app.stop()
        logger.info("Bot stopped.")


if __name__ == "__main__":
    app.run(main())
