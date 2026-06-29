"""
Cloudflare R2 helper (S3-compatible, zero egress).

Used for merged files: upload the finished file, then hand out a presigned URL
so users download straight from R2 — no egress cost on your VM.

Blocking boto3 calls run in a thread so they don't block the asyncio loop.
"""

from __future__ import annotations

import asyncio
import logging

import config

logger = logging.getLogger("file2link.r2")


def _client():
    import boto3  # imported lazily so the bot runs even without boto3 installed

    return boto3.client(
        "s3",
        endpoint_url=f"https://{config.R2_ACCOUNT_ID}.r2.cloudflarestorage.com",
        aws_access_key_id=config.R2_ACCESS_KEY_ID,
        aws_secret_access_key=config.R2_SECRET_ACCESS_KEY,
        region_name="auto",
    )


def is_configured() -> bool:
    return bool(
        config.R2_ENABLED
        and config.R2_ACCOUNT_ID
        and config.R2_ACCESS_KEY_ID
        and config.R2_SECRET_ACCESS_KEY
        and config.R2_BUCKET
    )


async def upload(path, key: str) -> None:
    def _do() -> None:
        _client().upload_file(str(path), config.R2_BUCKET, key)

    await asyncio.to_thread(_do)
    logger.info("Uploaded to R2: %s", key)


async def presigned_url(key: str, expires: int) -> str:
    def _do() -> str:
        return _client().generate_presigned_url(
            "get_object",
            Params={"Bucket": config.R2_BUCKET, "Key": key},
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
