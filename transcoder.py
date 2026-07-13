#!/usr/bin/env python3
"""
Main orchestrator for the production batch video transcoder.
- Scans /root/SRC_Videos recursively
- Producer/consumer with queue.Queue + worker threads (NOT multiprocessing)
- Auto CPU tuning
- Rich live dashboard
- Graceful Ctrl+C shutdown (current ffmpeg jobs finish)
- UTF-8 logging + final summary
- .partial safety + abandoned partial cleanup on start
- Full unicode / long filename / emoji support via pathlib + list args

This is the ONLY file you need to run:
    python -m transcoder.transcoder
or
    python transcoder.py
"""

from __future__ import annotations

import signal
import sys
import threading
import time
from pathlib import Path
from queue import Queue
from typing import Optional

from config import config
from dashboard import Dashboard, GlobalStats
from encoder import EncodeResult
from logger import log_event, setup_logger
from scanner import scan_videos
from utils import cleanup_abandoned_partials, get_file_size
from worker import Worker, WorkerStats


def main() -> None:
    logger = setup_logger(console=True)

    # === Startup cleanup (reliability for 20TB unattended runs) ===
    logger.info(f"Cleaning abandoned *.partial files in {config.ENC_DIR} ...")
    deleted = cleanup_abandoned_partials(config.ENC_DIR)
    if deleted:
        logger.info(f"Deleted {deleted} abandoned partial file(s) from previous runs.")

    # === Prepare directories ===
    config.SRC_DIR.mkdir(parents=True, exist_ok=True)
    config.ENC_DIR.mkdir(parents=True, exist_ok=True)

    # === Shared state ===
    job_queue: Queue[Optional[Path]] = Queue(maxsize=1000)  # bounded to avoid memory bloat on huge scans
    result_queue: Queue[EncodeResult] = Queue()
    stop_event = threading.Event()

    worker_stats: dict[int, WorkerStats] = {}
    stats_lock = threading.Lock()
    global_stats = GlobalStats(start_time=time.time())

    for wid in range(config.MAX_WORKERS):
        worker_stats[wid] = WorkerStats(worker_id=wid)

    # === Dashboard ===
    dashboard = Dashboard(worker_stats, stats_lock, global_stats)
    dashboard.start()

    # === Signal handling for graceful shutdown ===
    def signal_handler(signum: int, frame: Optional[object]) -> None:
        logger.warning("Ctrl+C received. Finishing current encodes then shutting down...")
        stop_event.set()
        # Do NOT empty queue here - let workers finish current jobs

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    # === Producer: scan and enqueue ===
    logger.info(f"Scanning {config.SRC_DIR} ...")
    video_files = list(scan_videos(config.SRC_DIR))
    global_stats.total_found = len(video_files)
    logger.info(f"Found {global_stats.total_found} supported video files.")

    # Enqueue all (producer is main thread)
    for path in video_files:
        job_queue.put(path)
    global_stats.queued = len(video_files)

    # Poison pills so workers know when to stop
    for _ in range(config.MAX_WORKERS):
        job_queue.put(None)

    # === Start worker threads ===
    workers: list[Worker] = []
    for wid in range(config.MAX_WORKERS):
        w = Worker(
            worker_id=wid,
            job_queue=job_queue,
            result_queue=result_queue,
            stop_event=stop_event,
            stats_dict=worker_stats,
            stats_lock=stats_lock,
            logger=logger,
        )
        w.start()
        workers.append(w)

    logger.info(f"Started {config.MAX_WORKERS} worker threads (each using {config.THREADS_PER_FFMPEG} ffmpeg threads).")

    # === Result consumer (main thread) ===
    processed = 0
    try:
        while processed < len(video_files) and not stop_event.is_set():
            try:
                result: EncodeResult = result_queue.get(timeout=1.0)
            except Exception:
                # Timeout - refresh dashboard
                dashboard.refresh()
                continue

            processed += 1
            result_queue.task_done()

            if result.skipped_reason:
                global_stats.skipped += 1
                log_event(logger, "SKIPPED", result.input_path, skipped_reason=result.skipped_reason)
            elif result.success:
                global_stats.completed += 1
                global_stats.total_original_size += result.original_size
                global_stats.total_encoded_size += result.encoded_size
                log_event(
                    logger,
                    "COMPLETED",
                    result.input_path,
                    result.output_path,
                    duration=result.duration,
                    original_size=result.original_size,
                    encoded_size=result.encoded_size,
                )
            else:
                global_stats.failed += 1
                log_event(
                    logger,
                    "FAILED",
                    result.input_path,
                    error=result.error or "unknown error",
                )

            # Update dashboard after each result
            dashboard.refresh()

    except KeyboardInterrupt:
        stop_event.set()

    # === Wait for all workers to finish current jobs ===
    logger.info("Waiting for workers to finish current jobs (graceful shutdown)...")
    for w in workers:
        w.join(timeout=300)  # generous timeout per worker

    # === Final dashboard refresh + stop ===
    dashboard.refresh()
    time.sleep(0.5)
    dashboard.stop()

    # === Final summary ===
    elapsed = time.time() - global_stats.start_time
    saved = global_stats.total_original_size - global_stats.total_encoded_size
    saved_pct = (saved / global_stats.total_original_size * 100) if global_stats.total_original_size > 0 else 0.0

    summary = f"""
{'='*80}
FINAL SUMMARY
{'='*80}
Total files found          : {global_stats.total_found}
Queued for processing      : {global_stats.queued}
Skipped                    : {global_stats.skipped}
Successfully completed     : {global_stats.completed}
Failed                     : {global_stats.failed}
Total runtime              : {elapsed:.1f} seconds ({elapsed/3600:.2f} hours)
Original total size        : {global_stats.total_original_size / (1024**3):.2f} GB
Encoded total size         : {global_stats.total_encoded_size / (1024**3):.2f} GB
Space saved                : {saved / (1024**3):.2f} GB  ({saved_pct:.1f}%)
{'='*80}
"""
    print(summary)
    logger.info(summary.replace("\n", " | "))

    if global_stats.failed > 0:
        logger.warning(f"{global_stats.failed} file(s) failed. Check {config.LOG_FILE} for details.")
        sys.exit(1)
    else:
        logger.info("All done. Exiting cleanly.")
        sys.exit(0)


if __name__ == "__main__":
    main()