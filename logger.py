#!/usr/bin/env python3
"""
Production logging module.
UTF-8 file logging + optional console.
All logs go to /root/transcoder/transcoder.log
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Optional

from config import config


def setup_logger(console: bool = True) -> logging.Logger:
    """Configure and return the root logger.
    Creates log dir if needed.
    """
    config.LOG_DIR.mkdir(parents=True, exist_ok=True)

    logger = logging.getLogger("transcoder")
    logger.setLevel(logging.INFO)
    logger.propagate = False

    # Remove any existing handlers (idempotent)
    for h in logger.handlers[:]:
        logger.removeHandler(h)

    # File handler - UTF-8, append, with rotation not needed for this use case
    fh = logging.FileHandler(config.LOG_FILE, encoding="utf-8", mode="a")
    fh.setLevel(logging.INFO)
    file_formatter = logging.Formatter(
        "%(asctime)s | %(levelname)-8s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    fh.setFormatter(file_formatter)
    logger.addHandler(fh)

    if console:
        ch = logging.StreamHandler(sys.stdout)
        ch.setLevel(logging.INFO)
        console_formatter = logging.Formatter("%(message)s")
        ch.setFormatter(console_formatter)
        logger.addHandler(ch)

    logger.info("=" * 80)
    logger.info("TRANSCODER STARTED")
    logger.info("=" * 80)
    return logger


def log_event(
    logger: logging.Logger,
    event: str,
    input_path: Optional[Path] = None,
    output_path: Optional[Path] = None,
    duration: Optional[float] = None,
    original_size: Optional[int] = None,
    encoded_size: Optional[int] = None,
    error: Optional[str] = None,
) -> None:
    """Structured one-line log events for later analysis."""
    parts = [event]
    if input_path:
        parts.append(f"input={input_path}")
    if output_path:
        parts.append(f"output={output_path}")
    if duration is not None:
        parts.append(f"runtime={duration:.1f}s")
    if original_size is not None and encoded_size is not None:
        saved = original_size - encoded_size
        pct = (saved / original_size * 100) if original_size > 0 else 0
        parts.append(f"size={original_size}->{encoded_size} saved={saved} ({pct:.1f}%)")
    if error:
        parts.append(f"ERROR: {error}")

    logger.info(" | ".join(parts))