"""
FFmpeg merge logic with chapter markers.

- get_video_duration(file)      -> duration in seconds via ffprobe
- create_chapter_metadata(files)-> writes an FFMETADATA file with one chapter
                                   per episode at the correct timestamps
- merge_videos(user_id, files)  -> concat demuxer (-c copy) + chapters,
                                   outputs output/{user_id}_merged.mkv
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
    """Return media duration in seconds (0.0 if it can't be determined)."""
    cmd = [
        config.FFPROBE_BINARY,
        "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        str(file),
    ]
    code, out = await _run(cmd)
    if code != 0:
        logger.warning("ffprobe failed for %s: %s", file, out[-300:])
        return 0.0
    try:
        return float(out.strip())
    except ValueError:
        return 0.0


async def create_chapter_metadata(files: List[Path], metadata_path: Path) -> None:
    """Write an FFMETADATA file with a chapter per input file."""
    lines = [";FFMETADATA1"]
    start_ms = 0
    for idx, f in enumerate(files, start=1):
        duration = await get_video_duration(f)
        end_ms = start_ms + int(duration * 1000)
        # Guard against zero-length: ensure END > START.
        if end_ms <= start_ms:
            end_ms = start_ms + 1
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
    demuxer with stream copy (no re-encode) plus chapter markers.
    Returns the output path. Raises MergeError on failure.
    """
    if len(files) < 2:
        raise MergeError("Need at least 2 files to merge.")

    output = config.OUTPUT_DIR / f"{user_id}_merged.mkv"
    list_path = config.SESSIONS_DIR / f"{user_id}_concat.txt"
    meta_path = config.SESSIONS_DIR / f"{user_id}_chapters.txt"

    output.parent.mkdir(parents=True, exist_ok=True)
    list_path.parent.mkdir(parents=True, exist_ok=True)

    _write_concat_list(files, list_path)
    await create_chapter_metadata(files, meta_path)

    cmd = [
        config.FFMPEG_BINARY, "-y",
        "-f", "concat", "-safe", "0", "-i", str(list_path),
        "-i", str(meta_path),
        "-map", "0",
        "-map_metadata", "1",
        "-map_chapters", "1",
        "-c", "copy",
        str(output),
    ]
    code, log = await _run(cmd)

    # Clean up the intermediate text files regardless of outcome.
    for p in (list_path, meta_path):
        try:
            p.unlink()
        except OSError:
            pass

    if code != 0 or not output.exists() or output.stat().st_size == 0:
        raise MergeError(
            "ffmpeg concat failed. This usually means the episodes use "
            "different codecs/resolutions (stream copy needs them identical).\n\n"
            f"{log[-1200:]}"
        )

    return output
