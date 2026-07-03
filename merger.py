"""
FFmpeg merge logic with chapter markers.

Fixes applied:
- round() instead of int() for millisecond timestamps (no truncation drift)
- ffprobe returns 0 or fails → raises MergeError immediately (never silently uses 0)
- Output and temp dirs created before use
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import List

import config

logger = logging.getLogger("file2link.merger")


class MergeError(Exception):
    pass


async def _run(cmd: List[str]) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    out, _ = await proc.communicate()
    return proc.returncode, out.decode(errors="replace")


async def get_video_duration(file: Path) -> float:
    """Return media duration in seconds. Raises MergeError if ffprobe fails or
    returns 0 (which means the file isn't ready / is corrupt)."""
    cmd = [
        config.FFPROBE_BINARY,
        "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        str(file),
    ]
    code, out = await _run(cmd)
    if code != 0:
        raise MergeError(
            f"ffprobe failed for {file.name}:\n{out[-300:]}"
        )
    try:
        val = float(out.strip())
    except ValueError:
        raise MergeError(f"ffprobe returned non-numeric output for {file.name}: {out[:100]}")

    if val <= 0:
        raise MergeError(
            f"ffprobe returned duration={val} for {file.name}. "
            "File may be corrupt or not fully written to disk."
        )
    return val


async def create_chapter_metadata(files: List[Path], metadata_path: Path) -> None:
    """Write an FFMETADATA file with one chapter per input file.
    Uses round() for millisecond precision — no truncation drift."""
    lines = [";FFMETADATA1"]
    start_ms = 0
    for idx, f in enumerate(files, start=1):
        duration = await get_video_duration(f)   # raises on failure
        end_ms = start_ms + round(duration * 1000)
        # Guard: END must be strictly greater than START.
        if end_ms <= start_ms:
            raise MergeError(
                f"Computed chapter END ({end_ms}ms) ≤ START ({start_ms}ms) for "
                f"{f.name}. Duration was {duration}s."
            )
        lines.append("[CHAPTER]")
        lines.append("TIMEBASE=1/1000")
        lines.append(f"START={start_ms}")
        lines.append(f"END={end_ms}")
        lines.append(f"title=Chapter {idx}")
        start_ms = end_ms
    metadata_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _write_concat_list(files: List[Path], list_path: Path) -> None:
    lines = []
    for f in files:
        safe = str(f.resolve()).replace("'", "'\\''")
        lines.append(f"file '{safe}'")
    list_path.write_text("\n".join(lines), encoding="utf-8")


async def merge_videos(user_id: int, files: List[Path]) -> Path:
    """
    Merge `files` (in order) into output/{user_id}_merged.mkv using the concat
    demuxer with stream copy (no re-encode) and chapter markers.
    Raises MergeError on any failure (including ffprobe issues).
    """
    if len(files) < 2:
        raise MergeError("Need at least 2 files to merge.")

    output = config.OUTPUT_DIR / f"{user_id}_merged.mkv"
    list_path = config.SESSIONS_DIR / f"{user_id}_concat.txt"
    meta_path = config.SESSIONS_DIR / f"{user_id}_chapters.txt"

    # Ensure directories exist before writing.
    output.parent.mkdir(parents=True, exist_ok=True)
    list_path.parent.mkdir(parents=True, exist_ok=True)

    _write_concat_list(files, list_path)
    await create_chapter_metadata(files, meta_path)   # raises on ffprobe failure

    cmd = [
        config.FFMPEG_BINARY, "-y",
        "-f", "concat", "-safe", "0", "-i", str(list_path),
        "-i", str(meta_path),
        "-map", "0",
        "-map_metadata", "1",
        "-map_chapters", "1",
        "-c", "copy",   # stream copy — no re-encode, lossless, fast
        str(output),
    ]
    code, log = await _run(cmd)

    # Clean up intermediate text files.
    for p in (list_path, meta_path):
        try:
            p.unlink()
        except OSError:
            pass

    if code != 0 or not output.exists() or output.stat().st_size == 0:
        raise MergeError(
            "ffmpeg concat failed. Episodes must share identical codecs and "
            "resolution for stream copy to work.\n\n"
            f"{log[-1200:]}"
        )

    return output
