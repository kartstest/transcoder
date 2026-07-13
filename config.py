#!/usr/bin/env python3
"""
Configuration module for the batch video transcoder.
Central place for all paths, encoding settings, and tunable parameters.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Tuple


@dataclass(frozen=True)
class Config:
    """Immutable configuration for the transcoder."""

    # === Paths ===
    SRC_DIR: Path = Path("/root/SRC_Videos")
    ENC_DIR: Path = Path("/root/ENC_Videos")
    LOG_DIR: Path = Path("/root/transcoder")
    LOG_FILE: Path = field(init=False)

    # === Supported input extensions (case-insensitive) ===
    SUPPORTED_EXTS: Tuple[str, ...] = (
        ".mp4", ".mkv", ".avi", ".mov", ".flv", ".webm",
        ".ts", ".m2ts", ".mts", ".mpeg", ".mpg", ".m4v"
    )

    # === Video encoding settings ===
    VIDEO_CODEC: str = "libx264"
    PRESET: str = "veryfast"
    CRF: int = 22
    # Lanczos scaling to 720p, width auto with -2 to preserve aspect
    SCALE_FILTER: str = "scale=-2:720:flags=lanczos"
    PIX_FMT: str = "yuv420p"
    OUTPUT_EXT: str = ".mp4"

    # === Audio settings ===
    # If source audio codec(s) are all AAC -> copy, else re-encode to AAC 192k
    AUDIO_CODEC: str = "aac"
    AUDIO_BITRATE: str = "192k"

    # === Subtitle & metadata ===
    SUBTITLE_COPY: bool = True
    PRESERVE_METADATA: bool = True

    # === Worker / threading (auto-tuned at runtime) ===
    # These are computed dynamically based on os.cpu_count()
    # Do NOT hardcode here for production use on different machines
    MAX_WORKERS: int = field(init=False, repr=False)
    THREADS_PER_FFMPEG: int = field(init=False, repr=False)

    # === Probing / safety ===
    FFPROBE_TIMEOUT: int = 30  # seconds
    FFMPEG_TIMEOUT: int = 0    # 0 = no timeout (rely on graceful shutdown)

    # === Dashboard refresh ===
    DASHBOARD_REFRESH_HZ: float = 2.0

    def __post_init__(self) -> None:
        object.__setattr__(self, "LOG_FILE", self.LOG_DIR / "transcoder.log")
        self._compute_threading()

    def _compute_threading(self) -> None:
        """Auto-tune workers and threads based on available CPU cores.
        Goal: good balance between parallelism and not overloading the machine.
        Example: 16 cores -> 4 workers * 4 threads each.
        """
        cpu_count: int = os.cpu_count() or 4
        # Conservative but efficient for veryfast preset + CPU encoding
        max_workers = max(1, min(cpu_count // 4, 8))
        threads_per = max(1, cpu_count // max_workers)
        object.__setattr__(self, "MAX_WORKERS", max_workers)
        object.__setattr__(self, "THREADS_PER_FFMPEG", threads_per)

    @property
    def ffmpeg_global_args(self) -> list[str]:
        """Common ffmpeg arguments for all encodes."""
        return [
            "-hide_banner",
            "-y",                    # overwrite temp file if exists
            "-stats",                # human readable stats (we also parse progress)
            "-loglevel", "info",
        ]

    @property
    def video_args(self) -> list[str]:
        """Video filter + codec settings."""
        return [
            "-vf", self.SCALE_FILTER,
            "-c:v", self.VIDEO_CODEC,
            "-preset", self.PRESET,
            "-crf", str(self.CRF),
            "-pix_fmt", self.PIX_FMT,
            "-threads", str(self.THREADS_PER_FFMPEG),
        ]

    def get_audio_args(self, audio_codecs: list[str]) -> list[str]:
        """Return audio arguments.
        If ALL audio streams are already AAC -> copy (fast, lossless).
        Otherwise re-encode everything to AAC 192k.
        """
        if not audio_codecs:
            return ["-an"]  # no audio
        all_aac = all(c.lower() == "aac" for c in audio_codecs)
        if all_aac:
            return ["-c:a", "copy"]
        return ["-c:a", self.AUDIO_CODEC, "-b:a", self.AUDIO_BITRATE]

    def get_subtitle_args(self) -> list[str]:
        if self.SUBTITLE_COPY:
            return ["-c:s", "copy"]
        return ["-sn"]

    def get_metadata_args(self) -> list[str]:
        if self.PRESERVE_METADATA:
            return ["-map_metadata", "0", "-map_chapters", "0"]
        return []

    def build_ffmpeg_cmd(
        self,
        input_path: Path,
        output_partial: Path,
        video_stream_idx: int,
        audio_codecs: list[str],
        has_subtitles: bool,
    ) -> list[str]:
        """Build the complete ffmpeg command as argument list (no shell=True ever)."""
        cmd = ["ffmpeg"]
        cmd.extend(self.ffmpeg_global_args)

        # Input
        cmd.extend(["-i", str(input_path)])

        # Select only main video stream (we already chose correct index from probe)
        # Map video
        cmd.extend(["-map", f"0:{video_stream_idx}"])

        # Map all audio streams
        cmd.extend(["-map", "0:a?"])

        # Map all subtitles if present
        if has_subtitles:
            cmd.extend(["-map", "0:s?"])

        # Video settings
        cmd.extend(self.video_args)

        # Audio
        cmd.extend(self.get_audio_args(audio_codecs))

        # Subtitles
        cmd.extend(self.get_subtitle_args())

        # Metadata
        cmd.extend(self.get_metadata_args())

        # Output
        cmd.extend(["-f", "mp4", str(output_partial)])

        return cmd


# Singleton config instance for easy import
config = Config()