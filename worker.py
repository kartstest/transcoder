#!/usr/bin/env python3
"""
Worker module.
Thread-based workers that consume jobs from queue.Queue.
Each worker owns one Encoder instance.
Supports graceful stop via threading.Event.
Updates shared dashboard state under lock.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from queue import Empty, Queue
from typing import Any, Callable, Optional

from encoder import EncodeResult, Encoder


@dataclass
class WorkerStats:
    """Per-worker live stats (updated by worker, read by dashboard)."""
    worker_id: int
    status: str = "idle"          # idle | probing | encoding | done
    current_file: str = ""
    percent: float = 0.0
    fps: float = 0.0
    eta_seconds: float = 0.0
    files_completed: int = 0
    files_failed: int = 0


class Worker(threading.Thread):
    """Single worker thread."""

    def __init__(
        self,
        worker_id: int,
        job_queue: Queue[Optional[Path]],
        result_queue: Queue[EncodeResult],
        stop_event: threading.Event,
        stats_dict: dict[int, WorkerStats],
        stats_lock: threading.Lock,
        logger: Any,
    ):
        super().__init__(daemon=True, name=f"Worker-{worker_id}")
        self.worker_id = worker_id
        self.job_queue = job_queue
        self.result_queue = result_queue
        self.stop_event = stop_event
        self.stats_dict = stats_dict
        self.stats_lock = stats_lock
        self.logger = logger
        self.encoder = Encoder(progress_callback=self._progress_callback)
        self._current_input: Optional[Path] = None

    def _update_stats(self, **kwargs: Any) -> None:
        with self.stats_lock:
            stats = self.stats_dict.get(self.worker_id)
            if stats:
                for k, v in kwargs.items():
                    setattr(stats, k, v)

    def _progress_callback(self, current_file: str, percent: float, fps: float, eta: float) -> None:
        self._update_stats(
            status="encoding",
            current_file=current_file,
            percent=round(percent, 1),
            fps=round(fps, 1),
            eta_seconds=round(eta, 1),
        )

    def run(self) -> None:
        while not self.stop_event.is_set():
            try:
                input_path: Optional[Path] = self.job_queue.get(timeout=0.5)
            except Empty:
                continue

            if input_path is None:  # poison pill
                self.job_queue.task_done()
                break

            self._current_input = input_path
            self._update_stats(status="probing", current_file=str(input_path), percent=0.0, fps=0.0, eta_seconds=0.0)

            try:
                result = self.encoder.encode(input_path)
                self.result_queue.put(result)

                with self.stats_lock:
                    stats = self.stats_dict[self.worker_id]
                    if result.success and not result.skipped_reason:
                        stats.files_completed += 1
                    elif result.error:
                        stats.files_failed += 1

                self._update_stats(status="idle", current_file="", percent=0.0, fps=0.0, eta_seconds=0.0)
            except Exception as e:
                # Never let worker die
                error_result = EncodeResult(False, input_path, Path(""), error=str(e))
                self.result_queue.put(error_result)
                self.logger.error(f"Worker {self.worker_id} unhandled exception on {input_path}: {e}")
            finally:
                self.job_queue.task_done()
                self._current_input = None

        # Worker exiting
        self._update_stats(status="stopped", current_file="", percent=0.0)