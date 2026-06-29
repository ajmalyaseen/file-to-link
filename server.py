"""
FastAPI download/stream server. Runs in the same process/loop as the bot.

Routes
------
GET /stream/{filename}?token=...
    Single-file instant links. Verifies the HMAC stream token, then streams
    the file directly from Telegram on demand (no server-side copy). Supports
    HTTP range requests for seeking/resume.

GET /download/{filename}?token=...
    Fallback for merged files when R2 is disabled: serves the file from the
    OUTPUT_DIR on disk.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional, Tuple

import uvicorn
from fastapi import FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import FileResponse, PlainTextResponse, StreamingResponse

import config
from utils import verify_signed_url, verify_stream_token

logger = logging.getLogger("file2link.server")

app = FastAPI(title="File-to-Link Server")

# Telegram client, injected by bot.py at startup (needed for streaming).
tg_client = None

# Telegram downloads in 1 MB chunks.
CHUNK_SIZE = 1024 * 1024


@app.get("/", response_class=PlainTextResponse)
async def root() -> str:
    return "File-to-Link bot server is running."


# --- streaming (single files) ----------------------------------------------

def _parse_range(range_header: str, size: int) -> Tuple[int, int]:
    """Parse 'bytes=start-end' into (start, end) inclusive."""
    try:
        units, _, rng = range_header.partition("=")
        if units.strip() != "bytes":
            return 0, size - 1
        start_s, _, end_s = rng.partition("-")
        start = int(start_s) if start_s else 0
        end = int(end_s) if end_s else size - 1
    except ValueError:
        return 0, size - 1
    start = max(0, start)
    end = min(end, size - 1)
    if start > end:
        start, end = 0, size - 1
    return start, end


@app.get("/stream/{filename}")
async def stream(
    filename: str,
    token: str = Query(...),
    range_header: Optional[str] = Header(None, alias="Range"),
):
    info = verify_stream_token(filename, token, config.SECRET_KEY)
    if info is None:
        raise HTTPException(status_code=403, detail="Invalid or expired link.")
    if tg_client is None:
        raise HTTPException(status_code=503, detail="Server not ready.")

    size = info["size"]
    try:
        message = await tg_client.get_messages(info["chat_id"], info["message_id"])
    except Exception:  # noqa: BLE001
        raise HTTPException(status_code=404, detail="File no longer available.")
    if message is None or not message.media:
        raise HTTPException(status_code=404, detail="File no longer available.")

    if range_header:
        start, end = _parse_range(range_header, size)
        status_code = 206
    else:
        start, end = 0, size - 1
        status_code = 200

    length = end - start + 1
    offset_chunks = start // CHUNK_SIZE
    first_skip = start - offset_chunks * CHUNK_SIZE

    async def body():
        sent = 0
        to_skip = first_skip
        async for chunk in tg_client.stream_media(message, offset=offset_chunks):
            if to_skip:
                chunk = chunk[to_skip:]
                to_skip = 0
            if sent + len(chunk) > length:
                chunk = chunk[: length - sent]
            if chunk:
                yield chunk
                sent += len(chunk)
            if sent >= length:
                break

    headers = {
        "Content-Length": str(length),
        "Accept-Ranges": "bytes",
        "Content-Disposition": f'attachment; filename="{Path(filename).name}"',
    }
    if status_code == 206:
        headers["Content-Range"] = f"bytes {start}-{end}/{size}"

    return StreamingResponse(
        body(),
        status_code=status_code,
        headers=headers,
        media_type="application/octet-stream",
    )


# --- disk download (merged files when R2 is off) ----------------------------

@app.get("/download/{filename}")
async def download(filename: str, token: str = Query(...)) -> FileResponse:
    if not verify_signed_url(filename, token, config.SECRET_KEY):
        raise HTTPException(status_code=403, detail="Invalid or expired link.")
    name = Path(filename).name
    path = (config.OUTPUT_DIR / name).resolve()
    if config.OUTPUT_DIR.resolve() not in path.parents or not path.is_file():
        raise HTTPException(status_code=404, detail="File not found or cleaned up.")
    return FileResponse(
        path, media_type="application/octet-stream", filename=name
    )


def build_server() -> uvicorn.Server:
    cfg = uvicorn.Config(
        app, host=config.HTTP_HOST, port=config.HTTP_PORT, log_level="warning"
    )
    return uvicorn.Server(cfg)
