#!/usr/bin/env python3
"""
Utility functions: robust ffprobe parsing, file size helpers, path handling,
partial file cleanup, human-readable formatting.
Everything is pure and side-effect free except filesystem helpers.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from config import config


@dataclass
class VideoInfo:
    """Structured information returned by robust probe."""
    width: int
    height: int
    duration: float
    video_codec: str
    audio_codecs: list[str]
    fps: float
    bitrate: int
    subtitle_count: int
    audio_stream_count: int
    is_interlaced: bool
    video_stream_index: int
    error: Optional[str] = None


def run_ffprobe(input_path: Path) -> dict[str, Any]:
    """Run ffprobe and return parsed JSON. Raises on failure."""
    cmd = [
        "ffprobe",
        "-v", "error",
        "-show_format",
        "-show_streams",
        "-of", "json",
        str(input_path),
    ]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=config.FFPROBE_TIMEOUT,
            check=True,
        )
        return json.loads(result.stdout)
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"ffprobe timeout on {input_path}")
    except subprocess.CalledProcessError as e:
        raise RuntimeError(f"ffprobe failed on {input_path}: {e.stderr}") from e
    except json.JSONDecodeError as e:
        raise RuntimeError(f"ffprobe JSON parse error on {input_path}") from e


def find_main_video_stream(streams: list[dict[str, Any]]) -> Optional[dict[str, Any]]:
    """Find the REAL movie video stream.
    Ignores PNG/MJPEG cover art (attached_pic disposition).
    Prefers the stream with largest resolution if multiple valid video streams.
    """
    candidates: list[dict[str, Any]] = []
    for s in streams:
        if s.get("codec_type") != "video":
            continue
        disp = s.get("disposition", {})
        if disp.get("attached_pic", 0) == 1:
            continue  # cover art, skip
        if s.get("width", 0) <= 0 or s.get("height", 0) <= 0:
            continue
        candidates.append(s)

    if not candidates:
        return None

    # Prefer highest resolution, then first
    candidates.sort(key=lambda s: (s.get("width", 0) * s.get("height", 0), s.get("index", 0)), reverse=True)
    return candidates[0]


def parse_r_frame_rate(rate: str) -> float:
    """Parse '30000/1001' or '25/1' into float FPS."""
    if not rate or rate == "0/0":
        return 0.0
    try:
        if "/" in rate:
            num, den = rate.split("/", 1)
            return float(num) / float(den) if float(den) != 0 else 0.0
        return float(rate)
    except (ValueError, ZeroDivisionError):
        return 0.0


def probe_video(input_path: Path) -> VideoInfo:
    """High-reliability probe. Never mistakes cover art for movie.
    Returns VideoInfo or raises with clear error.
    """
    try:
        data = run_ffprobe(input_path)
    except Exception as e:
        return VideoInfo(0, 0, 0.0, "", [], 0.0, 0, 0, 0, False, -1, str(e))

    streams: list[dict[str, Any]] = data.get("streams", [])
    fmt: dict[str, Any] = data.get("format", {})

    main_video = find_main_video_stream(streams)
    if main_video is None:
        return VideoInfo(0, 0, 0.0, "", [], 0.0, 0, 0, 0, False, -1, "No valid video stream found (cover art ignored)")

    # Basic fields
    width = int(main_video.get("width", 0))
    height = int(main_video.get("height", 0))
    video_codec = main_video.get("codec_name", "unknown")
    video_stream_index = int(main_video.get("index", 0))

    # Duration - prefer stream, fallback to format
    duration_str = main_video.get("duration") or fmt.get("duration", "0")
    try:
        duration = float(duration_str)
    except (ValueError, TypeError):
        duration = 0.0

    # FPS
    fps = parse_r_frame_rate(main_video.get("r_frame_rate", "0/0"))
    if fps == 0:
        fps = parse_r_frame_rate(main_video.get("avg_frame_rate", "0/0"))

    # Bitrate
    try:
        bitrate = int(fmt.get("bit_rate", 0)) or int(main_video.get("bit_rate", 0))
    except (ValueError, TypeError):
        bitrate = 0

    # Audio
    audio_streams = [s for s in streams if s.get("codec_type") == "audio"]
    audio_codecs = [s.get("codec_name", "unknown") for s in audio_streams]
    audio_stream_count = len(audio_streams)

    # Subtitles
    subtitle_streams = [s for s in streams if s.get("codec_type") == "subtitle"]
    subtitle_count = len(subtitle_streams)

    # Interlaced detection (best effort)
    is_interlaced = False
    field_order = main_video.get("field_order", "progressive")
    if field_order and field_order.lower() not in ("progressive", "unknown"):
        is_interlaced = True
    # Also check side_data for interlace info if present
    for side in main_video.get("side_data_list", []):
        if side.get("side_data_type", "").lower().startswith("interlaced"):
            is_interlaced = True
            break

    return VideoInfo(
        width=width,
        height=height,
        duration=duration,
        video_codec=video_codec,
        audio_codecs=audio_codecs,
        fps=fps,
        bitrate=bitrate,
        subtitle_count=subtitle_count,
        audio_stream_count=audio_stream_count,
        is_interlaced=is_interlaced,
        video_stream_index=video_stream_index,
    )


def should_skip_file(info: VideoInfo, output_path: Path) -> tuple[bool, str]:
    """Decide if we should skip this file and why."""
    if info.error:
        return True, f"probe error: {info.error}"
    if info.height <= 720:
        return True, f"height {info.height}p <= 720p"
    if output_path.exists():
        return True, "output already exists"
    if info.width <= 0 or info.height <= 0:
        return True, "invalid dimensions"
    if info.duration <= 0:
        return True, "invalid duration"
    return False, ""


def human_size(num_bytes: int) -> str:
    """Convert bytes to human readable (e.g. 1.23 GB)."""
    if num_bytes == 0:
        return "0 B"
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if abs(num_bytes) < 1024.0:
            return f"{num_bytes:.2f} {unit}"
        num_bytes /= 1024.0
    return f"{num_bytes:.2f} PB"


def get_file_size(path: Path) -> int:
    """Safe file size."""
    try:
        return path.stat().st_size if path.exists() else 0
    except OSError:
        return 0


def format_duration(seconds: float) -> str:
    """HH:MM:SS or MM:SS"""
    if seconds < 0:
        seconds = 0
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = int(seconds % 60)
    if hours > 0:
        return f"{hours:02d}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


def cleanup_abandoned_partials(enc_dir: Path) -> int:
    """Delete any *.partial files left from previous interrupted runs.
    Returns number of files deleted. Called at startup for reliability.
    """
    deleted = 0
    if not enc_dir.exists():
        return 0
    for partial in enc_dir.rglob("*.partial"):
        try:
            partial.unlink()
            deleted += 1
        except OSError:
            pass  # best effort
    return deleted


def safe_rename(src: Path, dst: Path) -> None:
    """Atomic rename on same filesystem. Best effort."""
    try:
        src.rename(dst)
    except OSError as e:
        # If cross-device or permission, try copy + delete
        try:
            shutil.copy2(src, dst)
            src.unlink()
        except OSError:
            raise RuntimeError(f"Failed to finalize {src} -> {dst}: {e}") from e


def get_relative_output_path(input_path: Path, src_dir: Path, enc_dir: Path) -> Path:
    """Preserve exact folder structure under ENC_DIR, change ext to .mp4"""
    try:
        rel = input_path.relative_to(src_dir)
    except ValueError:
        # Fallback: flat under ENC_DIR with original name (should never happen)
        rel = input_path.name
    output = enc_dir / rel
    return output.with_suffix(config.OUTPUT_EXT)