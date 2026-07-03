"""
Cloudflare R2 helper (S3-compatible, zero egress).

Fixes applied:
- Singleton boto3 client (created once, reused)
- Multipart parallel upload via TransferConfig (6 threads, 50 MB chunks)
- Upload progress via asyncio.run_coroutine_threadsafe → edits Telegram message every 5%
- boto3 lazily imported so bot starts even without it installed
- presigned_url() accepts optional filename for Content-Disposition
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Optional

import config

logger = logging.getLogger("file2link.r2")

# Singleton: created on first use, reused on every subsequent call.
_r2_client = None


def _client():
    global _r2_client
    if _r2_client is None:
        import boto3  # lazy import — bot runs even without boto3
        _r2_client = boto3.client(
            "s3",
            endpoint_url=f"https://{config.R2_ACCOUNT_ID}.r2.cloudflarestorage.com",
            aws_access_key_id=config.R2_ACCESS_KEY_ID,
            aws_secret_access_key=config.R2_SECRET_ACCESS_KEY,
            region_name="auto",
        )
    return _r2_client


def is_configured() -> bool:
    return bool(
        config.R2_ENABLED
        and config.R2_ACCOUNT_ID
        and config.R2_ACCESS_KEY_ID
        and config.R2_SECRET_ACCESS_KEY
        and config.R2_BUCKET
    )


async def _safe_edit(msg, text: str) -> None:
    try:
        await msg.edit_text(text)
    except Exception:  # noqa: BLE001
        pass


async def upload(path, key: str, status_msg=None) -> None:
    """Upload a file to R2 with multipart parallel transfer.
    Optionally edits `status_msg` every 5% of progress."""
    from boto3.s3.transfer import TransferConfig  # lazy import

    file_path = Path(path)
    total = file_path.stat().st_size

    # Callback state (plain list so closure mutation works in Python 3.12).
    uploaded = [0]
    last_pct = [0]
    loop = asyncio.get_event_loop()

    def _progress(bytes_transferred: int) -> None:
        if total == 0 or status_msg is None:
            return
        uploaded[0] += bytes_transferred
        pct = uploaded[0] * 100 // total
        if pct - last_pct[0] < 5:
            return
        last_pct[0] = pct
        done_str = _fmt(uploaded[0])
        tot_str = _fmt(total)
        asyncio.run_coroutine_threadsafe(
            _safe_edit(status_msg, f"☁️ Uploading to storage... {pct}%  ({done_str} / {tot_str})"),
            loop,
        )

    transfer_cfg = TransferConfig(
        multipart_threshold=50 * 1024 * 1024,   # files >50 MB → multipart
        multipart_chunksize=50 * 1024 * 1024,    # 50 MB per chunk
        max_concurrency=6,                        # 6 parallel threads
        use_threads=True,
    )

    def _do() -> None:
        _client().upload_file(
            str(file_path),
            config.R2_BUCKET,
            key,
            Config=transfer_cfg,
            Callback=_progress,
        )

    await asyncio.to_thread(_do)
    logger.info("Uploaded to R2: %s (%s)", key, _fmt(total))


def _fmt(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


async def presigned_url(key: str, expires: int, filename: Optional[str] = None) -> str:
    def _do() -> str:
        params: dict = {"Bucket": config.R2_BUCKET, "Key": key}
        if filename:
            params["ResponseContentDisposition"] = f'attachment; filename="{filename}"'
        return _client().generate_presigned_url(
            "get_object",
            Params=params,
            ExpiresIn=int(expires),
        )

    return await asyncio.to_thread(_do)


async def delete(key: str) -> None:
    def _do() -> None:
        _client().delete_object(Bucket=config.R2_BUCKET, Key=key)

    try:
        await asyncio.to_thread(_do)
        logger.info("Deleted from R2: %s", key)
    except Exception:  # noqa: BLE001
        logger.exception("R2 delete failed for %s", key)


async def schedule_delete(key: str, delay: int) -> None:
    try:
        await asyncio.sleep(delay)
        await delete(key)
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001
        logger.exception("R2 scheduled delete failed for %s", key)
