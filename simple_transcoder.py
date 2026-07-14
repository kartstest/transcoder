#!/usr/bin/env python3
"""
SIMPLE Batch Video Transcoder - Single File Version (Updated)
==============================================================
Subtitles are DISABLED by default because MP4 has poor support
for many subtitle formats (including SRT in some cases).

If you ever want subtitles back, change INCLUDE_SUBTITLES = True
at the top.

All other behavior is the same: simple, sequential, skips on error,
.partial safety, good logging.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

# ============================================================
# CONFIGURATION
# ============================================================
SRC_DIR = Path("/root/SRC_Videos")
ENC_DIR = Path("/root/ENC_Videos")
LOG_DIR = Path("/root/transcoder")
LOG_FILE = LOG_DIR / "transcoder.log"

SUPPORTED_EXTS = {".mp4", ".mkv", ".avi", ".mov", ".flv", ".webm",
                  ".ts", ".m2ts", ".mts", ".mpeg", ".mpg", ".m4v"}

# Encoding settings
VIDEO_CODEC = "libx264"
PRESET = "veryfast"
CRF = 22
SCALE_FILTER = "scale=-2:720:flags=lanczos"
PIX_FMT = "yuv420p"
OUTPUT_EXT = ".mp4"

AUDIO_CODEC = "aac"
AUDIO_BITRATE = "192k"

# Set to True only if you really need subtitles (may cause failures on some files)
INCLUDE_SUBTITLES = False

FFPROBE_TIMEOUT = 30
PROGRESS_UPDATE_INTERVAL = 2.0
CPU_COUNT = os.cpu_count() or 8
FFMPEG_THREADS = max(2, min(CPU_COUNT // 2, 6))


# ============================================================
# DATA CLASSES
# ============================================================
@dataclass
class VideoInfo:
    width: int
    height: int
    duration: float
    video_codec: str
    audio_codecs: list[str]
    fps: float
    video_stream_index: int
    subtitle_count: int
    error: Optional[str] = None


@dataclass
class EncodeStats:
    total_found: int = 0
    processed: int = 0
    skipped: int = 0
    completed: int = 0
    failed: int = 0
    total_original_size: int = 0
    total_encoded_size: int = 0
    start_time: float = 0.0


# ============================================================
# LOGGING
# ============================================================
def setup_logger() -> logging.Logger:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("simple_transcoder")
    logger.setLevel(logging.INFO)
    logger.propagate = False

    for h in logger.handlers[:]:
        logger.removeHandler(h)

    fh = logging.FileHandler(LOG_FILE, encoding="utf-8", mode="a")
    fh.setFormatter(logging.Formatter(
        "%(asctime)s | %(levelname)-7s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    ))
    logger.addHandler(fh)

    ch = logging.StreamHandler(sys.stdout)
    ch.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(ch)

    return logger


logger = setup_logger()


def log(msg: str, level: str = "info") -> None:
    if level == "info":
        logger.info(msg)
    elif level == "warning":
        logger.warning(msg)
    elif level == "error":
        logger.error(msg)


# ============================================================
# UTILITIES
# ============================================================
def human_size(n: int) -> str:
    if n == 0:
        return "0 B"
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if abs(n) < 1024.0:
            return f"{n:.2f} {unit}"
        n /= 1024.0
    return f"{n:.2f} PB"


def format_duration(sec: float) -> str:
    if sec < 0:
        sec = 0
    h = int(sec // 3600)
    m = int((sec % 3600) // 60)
    s = int(sec % 60)
    return f"{h:02d}:{m:02d}:{s:02d}" if h > 0 else f"{m:02d}:{s:02d}"


def cleanup_abandoned_partials() -> int:
    if not ENC_DIR.exists():
        return 0
    deleted = 0
    for p in ENC_DIR.rglob("*.partial"):
        try:
            p.unlink()
            deleted += 1
        except Exception:
            pass
    return deleted


def get_relative_output(input_path: Path) -> Path:
    try:
        rel = input_path.relative_to(SRC_DIR)
    except ValueError:
        rel = input_path.name
    return (ENC_DIR / rel).with_suffix(OUTPUT_EXT)


def get_file_size(p: Path) -> int:
    try:
        return p.stat().st_size if p.exists() else 0
    except Exception:
        return 0


# ============================================================
# FFPROBE
# ============================================================
def run_ffprobe(path: Path) -> dict[str, Any]:
    cmd = ["ffprobe", "-v", "error", "-show_format", "-show_streams", "-of", "json", str(path)]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=FFPROBE_TIMEOUT, check=True)
    return json.loads(result.stdout)


def find_main_video_stream(streams: list[dict]) -> Optional[dict]:
    candidates = []
    for s in streams:
        if s.get("codec_type") != "video":
            continue
        if s.get("disposition", {}).get("attached_pic", 0) == 1:
            continue
        if s.get("width", 0) > 0 and s.get("height", 0) > 0:
            candidates.append(s)
    if not candidates:
        return None
    candidates.sort(key=lambda s: s.get("width", 0) * s.get("height", 0), reverse=True)
    return candidates[0]


def parse_fps(rate: str) -> float:
    if not rate or rate == "0/0":
        return 0.0
    try:
        if "/" in rate:
            n, d = rate.split("/", 1)
            return float(n) / float(d) if float(d) else 0.0
        return float(rate)
    except Exception:
        return 0.0


def probe_video(path: Path) -> VideoInfo:
    try:
        data = run_ffprobe(path)
    except Exception as e:
        return VideoInfo(0, 0, 0.0, "", [], 0.0, -1, 0, str(e))

    streams = data.get("streams", [])
    fmt = data.get("format", {})

    main = find_main_video_stream(streams)
    if main is None:
        return VideoInfo(0, 0, 0.0, "", [], 0.0, -1, 0, "No valid video stream")

    w = int(main.get("width", 0))
    h = int(main.get("height", 0))
    vcodec = main.get("codec_name", "unknown")
    vidx = int(main.get("index", 0))

    dur_str = main.get("duration") or fmt.get("duration", "0")
    try:
        dur = float(dur_str)
    except Exception:
        dur = 0.0

    fps = parse_fps(main.get("r_frame_rate", "0/0"))
    if fps == 0:
        fps = parse_fps(main.get("avg_frame_rate", "0/0"))

    audio_codecs = [s.get("codec_name", "unknown") for s in streams if s.get("codec_type") == "audio"]
    sub_count = len([s for s in streams if s.get("codec_type") == "subtitle"])

    return VideoInfo(w, h, dur, vcodec, audio_codecs, fps, vidx, sub_count)


def should_skip(info: VideoInfo, output_path: Path) -> tuple[bool, str]:
    if info.error:
        return True, f"probe failed: {info.error}"
    if info.height <= 720:
        return True, f"height={info.height}p <= 720p"
    if output_path.exists():
        return True, "output already exists"
    if info.duration <= 0 or info.width <= 0:
        return True, "invalid video"
    return False, ""


# ============================================================
# ENCODING
# ============================================================
PROGRESS_RE = re.compile(r"^(frame|fps|out_time_ms|progress|speed)=(.+)$")


def build_ffmpeg_cmd(input_path: Path, partial_path: Path,
                     vidx: int, audio_codecs: list[str]) -> list[str]:
    cmd = ["ffmpeg", "-hide_banner", "-y", "-stats", "-loglevel", "info",
           "-i", str(input_path)]

    cmd += ["-map", f"0:{vidx}"]          # main video
    cmd += ["-map", "0:a?"]               # all audio

    # Subtitles - disabled by default for MP4 compatibility
    if INCLUDE_SUBTITLES:
        cmd += ["-map", "0:s?"]

    # Video
    cmd += ["-vf", SCALE_FILTER,
            "-c:v", VIDEO_CODEC,
            "-preset", PRESET,
            "-crf", str(CRF),
            "-pix_fmt", PIX_FMT,
            "-threads", str(FFMPEG_THREADS)]

    # Audio
    all_aac = bool(audio_codecs) and all(c.lower() == "aac" for c in audio_codecs)
    if all_aac:
        cmd += ["-c:a", "copy"]
    else:
        cmd += ["-c:a", AUDIO_CODEC, "-b:a", AUDIO_BITRATE]

    # Subtitles handling
    if INCLUDE_SUBTITLES:
        cmd += ["-c:s", "copy"]
    else:
        cmd += ["-sn"]                    # drop subtitles

    cmd += ["-map_metadata", "0", "-f", "mp4", str(partial_path)]
    return cmd


def encode_file(input_path: Path, stats: EncodeStats) -> None:
    output_path = get_relative_output(input_path)
    partial_path = output_path.with_suffix(output_path.suffix + ".partial")
    output_path.parent.mkdir(parents=True, exist_ok=True)

    info = probe_video(input_path)
    skip, reason = should_skip(info, output_path)

    if skip:
        stats.skipped += 1
        log(f"[{stats.processed}/{stats.total_found}] SKIP  {input_path.name}  ({reason})")
        return

    original_size = get_file_size(input_path)
    cmd = build_ffmpeg_cmd(input_path, partial_path, info.video_stream_index, info.audio_codecs)

    log(f"[{stats.processed}/{stats.total_found}] START {input_path.name}  "
        f"({info.height}p → 720p | {human_size(original_size)})")

    process = subprocess.Popen(
        cmd + ["-progress", "pipe:1", "-nostats"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1
    )

    progress_data: dict[str, str] = {}
    last_print = time.time()
    total_dur = info.duration
    stderr_lines: list[str] = []

    def read_progress() -> None:
        nonlocal last_print
        if process.stdout is None:
            return
        for line in process.stdout:
            line = line.strip()
            if not line:
                continue
            m = PROGRESS_RE.match(line)
            if m:
                progress_data[m.group(1)] = m.group(2)
                now = time.time()
                if now - last_print >= PROGRESS_UPDATE_INTERVAL and total_dur > 0:
                    try:
                        ot = int(progress_data.get("out_time_ms", 0)) / 1000.0
                        pct = min(100.0, (ot / total_dur) * 100)
                        fps = float(progress_data.get("fps", 0))
                        spd = progress_data.get("speed", "1x").replace("x", "")
                        try:
                            speed = max(0.1, float(spd))
                        except Exception:
                            speed = 1.0
                        eta = max(0, (total_dur - ot) / speed)
                        print(f"    → {pct:5.1f}% | fps={fps:5.1f} | ETA {format_duration(eta)}",
                              end="\r", flush=True)
                        last_print = now
                    except Exception:
                        pass
        if process.stdout:
            process.stdout.close()

    import threading
    reader = threading.Thread(target=read_progress, daemon=True)
    reader.start()

    if process.stderr:
        for line in process.stderr:
            stderr_lines.append(line.strip())
        process.stderr.close()

    return_code = process.wait()
    reader.join(timeout=5)
    print()

    if return_code != 0:
        if partial_path.exists():
            try:
                partial_path.unlink()
            except Exception:
                pass
        stats.failed += 1
        error_msg = "\n".join(stderr_lines[-10:]) if stderr_lines else "No error details"
        log(f"    FAILED {input_path.name}  (ffmpeg exit code {return_code})", "error")
        log(f"    ffmpeg error:\n{error_msg}", "error")
        return

    try:
        if partial_path.exists():
            partial_path.rename(output_path)
        encoded_size = get_file_size(output_path)
        stats.completed += 1
        stats.total_original_size += original_size
        stats.total_encoded_size += encoded_size
        saved = original_size - encoded_size
        pct_saved = (saved / original_size * 100) if original_size > 0 else 0
        log(f"    DONE  {input_path.name}  "
            f"({human_size(original_size)} → {human_size(encoded_size)} | saved {pct_saved:.1f}%)")
    except Exception as e:
        stats.failed += 1
        log(f"    FAILED to finalize {input_path.name}: {e}", "error")


# ============================================================
# MAIN
# ============================================================
def main() -> None:
    print("=" * 70)
    print("SIMPLE VIDEO TRANSCODER (single file, sequential) - Subtitles DISABLED")
    print("=" * 70)

    deleted = cleanup_abandoned_partials()
    if deleted:
        log(f"Cleaned up {deleted} abandoned .partial file(s)")

    SRC_DIR.mkdir(parents=True, exist_ok=True)
    ENC_DIR.mkdir(parents=True, exist_ok=True)

    log(f"Scanning {SRC_DIR} ...")
    files = sorted([p for p in SRC_DIR.rglob("*")
                    if p.is_file() and p.suffix.lower() in SUPPORTED_EXTS])

    stats = EncodeStats(total_found=len(files), start_time=time.time())

    if stats.total_found == 0:
        log("No supported video files found.")
        return

    log(f"Found {stats.total_found} video files. Starting...")

    for idx, path in enumerate(files, 1):
        stats.processed = idx
        try:
            encode_file(path, stats)
        except KeyboardInterrupt:
            log("Interrupted by user.", "warning")
            break
        except Exception as e:
            stats.failed += 1
            log(f"UNEXPECTED ERROR on {path.name}: {e}", "error")

    elapsed = time.time() - stats.start_time
    saved = stats.total_original_size - stats.total_encoded_size
    saved_pct = (saved / stats.total_original_size * 100) if stats.total_original_size > 0 else 0

    print("\n" + "=" * 70)
    print("FINAL SUMMARY")
    print("=" * 70)
    print(f"Total files found      : {stats.total_found}")
    print(f"Processed              : {stats.processed}")
    print(f"Skipped                : {stats.skipped}")
    print(f"Successfully completed : {stats.completed}")
    print(f"Failed                 : {stats.failed}")
    print(f"Total runtime          : {format_duration(elapsed)}")
    if stats.total_original_size > 0:
        print(f"Original size          : {human_size(stats.total_original_size)}")
        print(f"Encoded size           : {human_size(stats.total_encoded_size)}")
        print(f"Space saved            : {human_size(saved)} ({saved_pct:.1f}%)")
    print("=" * 70)
    print(f"Log file: {LOG_FILE}")
    print("Done.")


if __name__ == "__main__":
    main()
