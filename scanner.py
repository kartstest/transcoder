#!/usr/bin/env python3
"""
Scanner module.
Recursively walks SRC_DIR using pathlib, collects only supported video files.
Case-insensitive extension matching.
Yields absolute paths ready for queueing.
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterator

from config import config


def is_supported_video(path: Path) -> bool:
    """Case-insensitive extension check."""
    return path.suffix.lower() in config.SUPPORTED_EXTS


def scan_videos(src_dir: Path) -> Iterator[Path]:
    """Generator that yields every supported video file under src_dir recursively.
    Skips directories that cannot be read (best effort, no crash).
    """
    if not src_dir.exists():
        return
    if not src_dir.is_dir():
        return

    for path in src_dir.rglob("*"):
        if path.is_file() and is_supported_video(path):
            yield path