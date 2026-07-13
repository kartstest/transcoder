#!/usr/bin/env python3
"""
Dashboard module using rich.
Live updating table + summary panels.
Thread-safe reads from shared worker stats.
No scrolling spam - single live view.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Optional

from rich.console import Console
from rich.live import Live
from rich.panel import Panel
from rich.progress import BarColumn, Progress, TextColumn
from rich.table import Table
from rich.text import Text

from config import config
from utils import format_duration, human_size


@dataclass
class GlobalStats:
    """Overall counters (protected by lock in main)."""
    total_found: int = 0
    queued: int = 0
    skipped: int = 0
    completed: int = 0
    failed: int = 0
    start_time: float = 0.0
    total_original_size: int = 0
    total_encoded_size: int = 0


class Dashboard:
    """Rich live dashboard."""

    def __init__(
        self,
        worker_stats: dict[int, Any],
        stats_lock: threading.Lock,
        global_stats: GlobalStats,
        console: Optional[Console] = None,
    ):
        self.worker_stats = worker_stats
        self.stats_lock = stats_lock
        self.global_stats = global_stats
        self.console = console or Console()
        self.live: Optional[Live] = None
        self._last_refresh = 0.0

    def _build_worker_table(self) -> Table:
        table = Table(title="Active Workers", expand=True)
        table.add_column("ID", justify="center", style="cyan", no_wrap=True)
        table.add_column("Status", justify="left")
        table.add_column("Current File", justify="left", overflow="fold", max_width=50)
        table.add_column("Progress", justify="right")
        table.add_column("FPS", justify="right")
        table.add_column("ETA", justify="right")

        with self.stats_lock:
            for wid in sorted(self.worker_stats.keys()):
                st = self.worker_stats[wid]
                status_style = {
                    "idle": "green",
                    "probing": "yellow",
                    "encoding": "bold blue",
                    "done": "green",
                    "stopped": "red",
                }.get(st.status, "white")

                eta_str = format_duration(st.eta_seconds) if st.eta_seconds > 0 else "-"
                progress_str = f"{st.percent:5.1f}%" if st.percent > 0 else "-"

                table.add_row(
                    str(wid),
                    Text(st.status, style=status_style),
                    st.current_file or "-",
                    progress_str,
                    f"{st.fps:.1f}" if st.fps > 0 else "-",
                    eta_str,
                )
        return table

    def _build_summary_panel(self) -> Panel:
        gs = self.global_stats
        elapsed = time.time() - gs.start_time if gs.start_time > 0 else 0
        elapsed_str = format_duration(elapsed)

        # Very rough overall ETA based on completed files (better than nothing)
        remaining_files = max(0, gs.queued - gs.completed - gs.skipped)
        avg_time_per_file = elapsed / max(1, gs.completed + gs.skipped) if (gs.completed + gs.skipped) > 0 else 60.0
        eta_overall = remaining_files * avg_time_per_file
        eta_str = format_duration(eta_overall) if remaining_files > 0 else "done"

        saved = gs.total_original_size - gs.total_encoded_size
        saved_pct = (saved / gs.total_original_size * 100) if gs.total_original_size > 0 else 0

        summary_text = (
            f"[bold]Total Found:[/bold] {gs.total_found}   "
            f"[bold]Queued:[/bold] {gs.queued}   "
            f"[bold]Skipped:[/bold] {gs.skipped}   "
            f"[bold green]Completed:[/bold green] {gs.completed}   "
            f"[bold red]Failed:[/bold red] {gs.failed}\n"
            f"[bold]Elapsed:[/bold] {elapsed_str}   "
            f"[bold]Overall ETA:[/bold] {eta_str}\n"
            f"[bold]Original Size:[/bold] {human_size(gs.total_original_size)}   "
            f"[bold]Encoded Size:[/bold] {human_size(gs.total_encoded_size)}   "
            f"[bold green]Saved:[/bold green] {human_size(saved)} ({saved_pct:.1f}%)"
        )
        return Panel(summary_text, title="Overall Progress", border_style="blue")

    def _build_header(self) -> Text:
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        header = Text.assemble(
            ("BATCH VIDEO TRANSCODER  ", "bold white on blue"),
            (f"  |  {now}  |  ", "dim"),
            (f"CPU: {config.MAX_WORKERS} workers × {config.THREADS_PER_FFMPEG} threads", "cyan"),
        )
        return header

    def render(self) -> Table:
        """Build the complete live renderable."""
        layout = Table.grid(expand=True)
        layout.add_row(self._build_header())
        layout.add_row(self._build_worker_table())
        layout.add_row(self._build_summary_panel())
        return layout

    def start(self) -> None:
        """Start the live display."""
        self.live = Live(
            self.render(),
            console=self.console,
            refresh_per_second=config.DASHBOARD_REFRESH_HZ,
            transient=False,
        )
        self.live.start()

    def stop(self) -> None:
        if self.live:
            self.live.stop()
            self.live = None

    def refresh(self) -> None:
        """Call this periodically from main loop or from worker callbacks."""
        if self.live:
            now = time.time()
            if now - self._last_refresh > (1.0 / config.DASHBOARD_REFRESH_HZ):
                self.live.update(self.render())
                self._last_refresh = now