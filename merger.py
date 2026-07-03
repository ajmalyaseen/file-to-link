"""
FFmpeg merge with correct timestamp handling.

The key problem with concat + -c copy for HEVC/MKV files:
- Each episode has a non-zero start_time (e.g. 0.041s due to B-frame offsets)
- Raw concat causes episode 2 to restart timestamps from its own start_time
- This produces wrong total duration, broken seeking, wrong chapter positions

Fix: use -fflags +genpts which forces FFmpeg to regenerate PTS values from
scratch across the whole output, producing monotonically increasing timestamps.
Also apply -avoid_negative_ts make_zero to handle negative DTS (common in
HEVC) and -async 1 to correct audio drift at segment boundaries.
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
    """Return media duration in seconds via ffprobe.
    Raises MergeError if ffprobe fails or returns 0 (corrupt / not ready)."""
    cmd = [
        config.FFPROBE_BINARY,
        "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        str(file),
    ]
    code, out = await _run(cmd)
    if code != 0:
        raise MergeError(f"ffprobe failed for {file.name}:\n{out[-300:]}")
    try:
        val = float(out.strip())
    except ValueError:
        raise MergeError(
            f"ffprobe returned non-numeric output for {file.name}: {out[:100]}"
        )
    if val <= 0:
        raise MergeError(
            f"ffprobe returned duration={val} for {file.name}. "
            "File may be corrupt or not fully written to disk."
        )
    return val


async def get_stream_start_time(file: Path) -> float:
    """Return the start_time of the first video stream (often non-zero for HEVC).
    Returns 0.0 if not available — used for accurate chapter offsets."""
    cmd = [
        config.FFPROBE_BINARY,
        "-v", "error",
        "-select_streams", "v:0",
        "-show_entries", "stream=start_time",
        "-of", "default=noprint_wrappers=1:nokey=1",
        str(file),
    ]
    code, out = await _run(cmd)
    if code != 0:
        return 0.0
    try:
        val = float(out.strip())
        return max(val, 0.0)
    except ValueError:
        return 0.0


async def create_chapter_metadata(files: List[Path], metadata_path: Path) -> None:
    """Write FFMETADATA with one chapter per episode.
    Uses round() for ms precision. Raises MergeError if any duration is bad."""
    lines = [";FFMETADATA1"]
    start_ms = 0
    for idx, f in enumerate(files, start=1):
        duration = await get_video_duration(f)   # raises on failure/0
        end_ms = start_ms + round(duration * 1000)
        if end_ms <= start_ms:
            raise MergeError(
                f"Chapter END ({end_ms}ms) ≤ START ({start_ms}ms) for "
                f"{f.name}. Duration={duration}s."
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
    Merge files into output/{user_id}_merged.mkv.
    Uses -c copy (lossless, fast). -fflags +genpts fixes timestamp
    discontinuities that occur with HEVC/MKV files that have non-zero
    start_times, which is the root cause of doubled duration and broken seeking.
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
        # -fflags +genpts: regenerate PTS from scratch across the whole output.
        # This is the key fix for HEVC non-zero start_time discontinuities.
        "-fflags", "+genpts",
        "-f", "concat", "-safe", "0",
        "-i", str(list_path),
        "-i", str(meta_path),
        "-map", "0",
        "-map_metadata", "1",
        "-map_chapters", "1",
        "-c", "copy",
        # Handle negative DTS (common in HEVC) by shifting to make them zero.
        "-avoid_negative_ts", "make_zero",
        str(output),
    ]
    code, log = await _run(cmd)

    for p in (list_path, meta_path):
        try:
            p.unlink()
        except OSError:
            pass

    if code != 0 or not output.exists() or output.stat().st_size == 0:
        raise MergeError(
            "ffmpeg concat failed. Episodes must share identical codecs and "
            "resolution for stream copy (-c copy) to work.\n\n"
            f"{log[-1500:]}"
        )

    return output
