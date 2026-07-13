#!/usr/bin/env python3
"""
Encoder module - single file responsibility.
Builds ffmpeg command via config, runs it with live progress parsing,
writes to .partial, renames on success, cleans up on failure.
Never writes directly to final .mp4 until 100% success.
"""

from __future__ import annotations

import re
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from queue import Queue
from typing import Callable, Optional

from config import config
from utils import (
    VideoInfo,
    get_file_size,
    get_relative_output_path,
    probe_video,
    safe_rename,
    should_skip_file,
)


@dataclass
class EncodeResult:
    success: bool
    input_path: Path
    output_path: Path
    original_size: int = 0
    encoded_size: int = 0
    duration: float = 0.0
    error: Optional[str] = None
    skipped_reason: Optional[str] = None


# Regex for ffmpeg progress lines (key=value)
PROGRESS_RE = re.compile(r"^(frame|fps|bitrate|time|speed|out_time_ms|progress)=(.+)$")


class Encoder:
    """Encodes ONE video file. Thread-safe for use from worker threads."""

    def __init__(
        self,
        progress_callback: Optional[Callable[[str, float, float, float], None]] = None,
    ):
        self.progress_callback = progress_callback  # (current_file, percent, fps, eta_seconds)

    def encode(self, input_path: Path) -> EncodeResult:
        """Main entry. Probes, decides skip/encode, runs ffmpeg safely."""
        output_path = get_relative_output_path(input_path, config.SRC_DIR, config.ENC_DIR)
        output_partial = output_path.with_suffix(output_path.suffix + ".partial")

        # Ensure output directory exists
        output_path.parent.mkdir(parents=True, exist_ok=True)

        # Probe first (cheap)
        info = probe_video(input_path)
        skip, reason = should_skip_file(info, output_path)
        if skip:
            return EncodeResult(
                success=True,
                input_path=input_path,
                output_path=output_path,
                skipped_reason=reason,
            )

        original_size = get_file_size(input_path)

        # Build command
        cmd = config.build_ffmpeg_cmd(
            input_path=input_path,
            output_partial=output_partial,
            video_stream_idx=info.video_stream_index,
            audio_codecs=info.audio_codecs,
            has_subtitles=info.subtitle_count > 0,
        )

        # Run with progress
        try:
            success, error = self._run_ffmpeg_with_progress(cmd, input_path, info.duration)
            if not success:
                # Cleanup partial on failure
                if output_partial.exists():
                    try:
                        output_partial.unlink()
                    except OSError:
                        pass
                return EncodeResult(False, input_path, output_path, original_size, 0, info.duration, error)

            # Success -> atomic rename
            if output_partial.exists():
                safe_rename(output_partial, output_path)
                encoded_size = get_file_size(output_path)
                return EncodeResult(True, input_path, output_path, original_size, encoded_size, info.duration)
            else:
                return EncodeResult(False, input_path, output_path, original_size, 0, info.duration, "Partial file missing after ffmpeg exit 0")

        except Exception as e:
            if output_partial.exists():
                try:
                    output_partial.unlink()
                except OSError:
                    pass
            return EncodeResult(False, input_path, output_path, original_size, 0, info.duration, str(e))

    def _run_ffmpeg_with_progress(
        self, cmd: list[str], input_path: Path, total_duration: float
    ) -> tuple[bool, Optional[str]]:
        """Run ffmpeg, parse progress from stdout (via -progress pipe:1), call callback."""
        # We use -progress pipe:1 so progress goes to stdout, regular logs to stderr
        full_cmd = cmd + ["-progress", "pipe:1", "-nostats"]

        process = subprocess.Popen(
            full_cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,  # line buffered
        )

        progress_data: dict[str, str] = {}
        last_update = time.time()

        def reader_thread() -> None:
            nonlocal progress_data, last_update
            assert process.stdout is not None
            for line in process.stdout:
                line = line.strip()
                if not line:
                    continue
                match = PROGRESS_RE.match(line)
                if match:
                    key, value = match.groups()
                    progress_data[key] = value
                    # Throttle callback to ~2-4 Hz
                    now = time.time()
                    if now - last_update > 0.25 and self.progress_callback:
                        self._emit_progress(progress_data, total_duration, input_path)
                        last_update = now
            process.stdout.close()

        reader = threading.Thread(target=reader_thread, daemon=True)
        reader.start()

        # Also consume stderr to prevent pipe full deadlock (we don't parse it for now)
        def stderr_drain() -> None:
            assert process.stderr is not None
            for _ in process.stderr:
                pass
            process.stderr.close()

        stderr_thread = threading.Thread(target=stderr_drain, daemon=True)
        stderr_thread.start()

        # Wait for process
        return_code = process.wait()
        reader.join(timeout=5)
        stderr_thread.join(timeout=5)

        if return_code == 0:
            # Final progress emit
            if self.progress_callback and progress_data:
                self._emit_progress(progress_data, total_duration, input_path, final=True)
            return True, None
        else:
            # Try to get last stderr lines for error
            stderr_lines: list[str] = []
            if process.stderr:
                try:
                    stderr_lines = process.stderr.readlines()[-10:] if not process.stderr.closed else []
                except Exception:
                    pass
            error_msg = f"ffmpeg exited with code {return_code}"
            if stderr_lines:
                error_msg += f": {' '.join(stderr_lines[-3:]).strip()}"
            return False, error_msg

    def _emit_progress(
        self, data: dict[str, str], total_duration: float, input_path: Path, final: bool = False
    ) -> None:
        """Compute percent, fps, eta and call user callback."""
        if not self.progress_callback or total_duration <= 0:
            return

        try:
            out_time_ms = int(data.get("out_time_ms", 0))
            current_time = out_time_ms / 1000.0
            percent = min(100.0, (current_time / total_duration) * 100) if total_duration > 0 else 0.0

            fps = float(data.get("fps", 0.0))
            speed = data.get("speed", "0x").replace("x", "")
            try:
                speed_val = float(speed)
            except ValueError:
                speed_val = 1.0

            remaining_time = max(0.0, total_duration - current_time)
            eta = remaining_time / max(speed_val, 0.1) if speed_val > 0 else 0.0

            self.progress_callback(str(input_path), percent, fps, eta)
        except (ValueError, KeyError, ZeroDivisionError):
            pass  # ignore bad progress line