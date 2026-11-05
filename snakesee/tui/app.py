"""Main Textual application.

Threading rule: the UI thread never touches the filesystem or runs subprocesses.
All I/O happens in worker threads, which hand plain data back via
``call_from_thread``. That keeps every keypress (including ctrl+z) responsive no
matter how slow the cluster filesystem is.
"""

from __future__ import annotations

import os
import signal
import threading
import time
from pathlib import Path
from typing import Any
from typing import ClassVar

from rich.text import Text
from textual import work
from textual.app import App
from textual.app import ComposeResult
from textual.binding import Binding
from textual.binding import BindingType
from textual.widgets import DataTable
from textual.widgets import Footer
from textual.widgets import ProgressBar
from textual.widgets import Static
from textual.widgets import TabbedContent
from textual.widgets import TabPane
from textual.worker import get_current_worker

from snakesee.joblogs import SourceKind
from snakesee.model import Job
from snakesee.model import JobStatus
from snakesee.model import RunStatus
from snakesee.model import RunSummary
from snakesee.monitor import Update
from snakesee.monitor import WorkflowMonitor
from snakesee.tui.format import fmt_duration
from snakesee.tui.format import fmt_time
from snakesee.tui.format import truncate
from snakesee.tui.log_screen import LogScreen
from snakesee.tui.widgets import JobTable

WILDCARD_WIDTH = 60
MESSAGE_WIDTH = 60
ROW_LIMIT = 1000
REBUILD_THRESHOLD = 50
"""Max rows shown in the Done and Failed tables (newest first)."""

# (column key, label) per table
COLUMNS: dict[JobStatus, list[tuple[str, str]]] = {
    JobStatus.ACTIVE: [
        ("jobid", "Job"),
        ("rule", "Rule"),
        ("wildcards", "Wildcards"),
        ("external", "Cluster ID"),
        ("submitted", "Submitted"),
        ("elapsed", "Since submit"),
    ],
    JobStatus.FAILED: [
        ("jobid", "Job"),
        ("rule", "Rule"),
        ("wildcards", "Wildcards"),
        ("external", "Cluster ID"),
        ("ended", "Failed at"),
        ("message", "Message"),
    ],
    JobStatus.DONE: [
        ("jobid", "Job"),
        ("rule", "Rule"),
        ("wildcards", "Wildcards"),
        ("ended", "Finished"),
        ("duration", "Submit→done"),
    ],
}
TABLE_IDS = {
    JobStatus.ACTIVE: "active",
    JobStatus.FAILED: "failed",
    JobStatus.DONE: "done",
}
TAB_TITLES = {
    JobStatus.ACTIVE: "Active",
    JobStatus.FAILED: "Failed",
    JobStatus.DONE: "Done",
}
STATUS_STYLE = {
    RunStatus.STARTING: "yellow",
    RunStatus.RUNNING: "bold green",
    RunStatus.DRAINING: "bold yellow",
    RunStatus.SUCCEEDED: "bold blue",
    RunStatus.FAILED: "bold red",
    RunStatus.NOTHING_TO_DO: "blue",
}


def _end_key(job: Job) -> float:
    return job.end_time or 0.0


def job_cells(job: Job, now: float) -> dict[str, Any]:
    """Cell values for a job's row in the table matching its status."""
    cells: dict[str, Any] = {
        "jobid": job.jobid,
        "rule": job.rule,
        "wildcards": truncate(job.wildcards, WILDCARD_WIDTH),
        "external": job.external_id or "-",
    }
    if job.status is JobStatus.ACTIVE:
        cells["submitted"] = fmt_time(job.submit_time, now)
        cells["elapsed"] = fmt_duration(now - job.submit_time if job.submit_time else None)
    elif job.status is JobStatus.FAILED:
        cells["ended"] = fmt_time(job.end_time, now)
        cells["message"] = truncate(job.error_message, MESSAGE_WIDTH)
    else:
        cells["ended"] = fmt_time(job.end_time, now)
        dur = job.end_time - job.submit_time if job.end_time and job.submit_time else None
        cells["duration"] = fmt_duration(dur)
    return cells


class SnakeseeApp(App[None]):
    """Monitor one Snakemake workflow."""

    TITLE = "snakesee"
    CSS = """
    #header { height: auto; padding: 0 1; background: $panel; }
    #progress { padding: 0 1; height: 1; }
    #progress Bar { width: 1fr; }
    TabbedContent { height: 1fr; }
    TabPane { padding: 0; }
    DataTable { height: 1fr; }
    """

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("q", "quit", "Quit"),
        # Raw terminal mode turns ctrl+z into an ordinary key; hand it back to the shell.
        Binding("ctrl+z", "suspend_process", "Background", priority=True),
        Binding("1", "show_tab('active')", "Active"),
        Binding("2", "show_tab('failed')", "Failed"),
        Binding("3", "show_tab('done')", "Done"),
        Binding("o", "open_log('stdout')", "stdout"),
        Binding("e", "open_log('stderr')", "stderr"),
        Binding("b", "open_log('bpeek')", "bpeek"),
        Binding("r", "refresh_now", "Refresh"),
    ]

    def __init__(
        self,
        workflow_dir: Path,
        log_file: Path | None = None,
        interval: float = 2.0,
    ) -> None:
        super().__init__()
        # Instant feedback beats eye candy: no sliding tab underlines or smooth scrolling.
        self.animation_level = "none"
        self.workflow_dir = workflow_dir
        self.interval = interval
        self.monitor = WorkflowMonitor(workflow_dir, log_file)
        self.jobs: dict[int, Job] = {}
        self._row_table: dict[int, JobStatus] = {}
        self.summary = RunSummary()
        self.log_path: Path | None = None
        self.log_mtime: float | None = None
        self.last_poll: float | None = None
        self.message = ""
        self._wake = threading.Event()
        self._stopping = False

    # ------------------------------------------------------------------ layout
    def compose(self) -> ComposeResult:
        yield Static(id="header")
        yield ProgressBar(id="progress", show_eta=False)
        with TabbedContent(id="tabs"):
            for status, table_id in TABLE_IDS.items():
                with TabPane(TAB_TITLES[status], id=f"tab-{table_id}"):
                    yield JobTable(id=table_id, cursor_type="row", zebra_stripes=True)
        yield Footer()

    def on_mount(self) -> None:
        for status, table_id in TABLE_IDS.items():
            table = self.query_one(f"#{table_id}", DataTable)
            for key, label in COLUMNS[status]:
                table.add_column(label, key=key)
        self.query_one("#active", DataTable).focus()
        self._render_header()
        self.set_interval(1.0, self._tick)
        self._poll_loop()

    def on_unmount(self) -> None:
        self._stopping = True
        self._wake.set()

    # ----------------------------------------------------------- worker thread
    @work(thread=True, exclusive=True, group="poll", exit_on_error=False)
    def _poll_loop(self) -> None:
        worker = get_current_worker()
        while not (worker.is_cancelled or self._stopping):
            try:
                update = self.monitor.poll()
            except Exception as e:  # keep monitoring through transient FS errors
                update = Update(message=f"poll error: {e}")
            try:
                self.call_from_thread(self._apply_update, update)
            except RuntimeError:  # app is shutting down
                return
            if update.more:
                continue
            self._wake.wait(self.interval)
            self._wake.clear()

    # ------------------------------------------------------------- UI updates
    def _apply_update(self, update: Update) -> None:
        self.last_poll = time.time()
        self.message = update.message
        if update.switched:
            for table_id in TABLE_IDS.values():
                self.query_one(f"#{table_id}", DataTable).clear()
            self.jobs.clear()
            self._row_table.clear()
        if update.log_path is not None:
            self.log_path = update.log_path
        if update.log_mtime is not None:
            self.log_mtime = update.log_mtime
        if update.summary is not None:
            self.summary = update.summary
        if update.jobs:
            self._apply_jobs(update.jobs)
        elif update.summary is not None or update.switched:
            self._update_tab_titles()
        self._render_header()

    def _apply_jobs(self, jobs: list[Job]) -> None:
        tables = {s: self.query_one(f"#{t}", DataTable) for s, t in TABLE_IDS.items()}
        cursor_keys = {s: _cursor_key(t) for s, t in tables.items()}
        now = time.time()
        new_rows: dict[JobStatus, list[Job]] = {s: [] for s in JobStatus}
        log_screen = self.screen if isinstance(self.screen, LogScreen) else None
        for job in jobs:
            key = str(job.jobid)
            old = self._row_table.get(job.jobid)
            if old is not None and old is not job.status:
                tables[old].remove_row(key)
                del self._row_table[job.jobid]
                old = None
            if old is None:
                new_rows[job.status].append(job)
            else:
                previous = job_cells(self.jobs[job.jobid], now)
                for col, value in job_cells(job, now).items():
                    if previous.get(col) != value:
                        tables[old].update_cell(key, col, value)
            self.jobs[job.jobid] = job
            if log_screen is not None and log_screen.job.jobid == job.jobid:
                log_screen.update_job(job)

        for status, pending in new_rows.items():
            if pending:
                self._add_rows(tables[status], status, pending, now)
        for status, table in tables.items():
            _restore_cursor(table, cursor_keys[status])
        self._update_tab_titles()

    def _add_rows(
        self, table: DataTable[Any], status: JobStatus, jobs: list[Job], now: float
    ) -> None:
        """Add rows; finished/failed tables keep only the newest ``ROW_LIMIT``, newest first.

        DataTable cost grows with row count (every cell is measured), so showing all
        of a 50k-job history would stall the UI for no real benefit.
        """
        columns = COLUMNS[status]

        def add(job: Job) -> None:
            cells = job_cells(job, now)
            table.add_row(*(cells[c] for c, _ in columns), key=str(job.jobid))
            self._row_table[job.jobid] = status

        if status is JobStatus.ACTIVE:
            for job in jobs:
                add(job)
            return

        shown = [self.jobs[int(k.value or -1)] for k in table.rows]
        keep = sorted(shown + jobs, key=_end_key, reverse=True)[:ROW_LIMIT]
        keep_ids = {j.jobid for j in keep}
        evicted = [j for j in shown if j.jobid not in keep_ids]
        if len(evicted) > REBUILD_THRESHOLD:
            # remove_row is O(rows) per call; one clear + bulk add is far cheaper.
            table.clear()
            for job in shown:
                self._row_table.pop(job.jobid, None)
            for job in keep:
                add(job)
        else:
            for job in evicted:
                table.remove_row(str(job.jobid))
                del self._row_table[job.jobid]
            for job in jobs:
                if job.jobid in keep_ids:
                    add(job)
        table.sort("jobid", key=lambda jid: _end_key(self.jobs[jid]), reverse=True)

    def _update_tab_titles(self) -> None:
        tabs = self.query_one(TabbedContent)
        s = self.summary
        counts = {
            JobStatus.ACTIVE: s.n_active,
            JobStatus.FAILED: s.n_failed,
            JobStatus.DONE: s.n_done,
        }
        for status, table_id in TABLE_IDS.items():
            shown = self.query_one(f"#{table_id}", DataTable).row_count
            label = f"{TAB_TITLES[status]} ({counts[status]})"
            if shown < counts[status]:
                label += f" newest {shown}"
            tabs.get_tab(f"tab-{table_id}").label = label

    def _tick(self) -> None:
        """Once a second: advance 'elapsed' for active jobs and the header clock."""
        now = time.time()
        table = self.query_one("#active", DataTable)
        for row_key in list(table.rows):
            job = self.jobs.get(int(row_key.value or -1))
            if job is not None and job.submit_time:
                table.update_cell(row_key, "elapsed", fmt_duration(now - job.submit_time))
        self._render_header()

    def _render_header(self) -> None:
        s = self.summary
        now = time.time()
        text = Text()
        text.append("snakesee ", style="bold")
        text.append(str(self.workflow_dir))
        if self.log_path is not None:
            text.append(f"  log: {self.log_path.name}", style="dim")
        if s.snakemake_version:
            text.append(f"  snakemake {s.snakemake_version}", style="dim")
        text.append("\n")
        text.append(f" {s.status.value.upper()} ", style=f"reverse {STATUS_STYLE[s.status]}")
        pct = f" ({100 * s.steps_done / s.steps_total:.0f}%)" if s.steps_total else ""
        text.append(f"  {s.steps_done}/{s.steps_total or '?'} steps{pct}")
        text.append(f"   active {s.n_active}", style="green")
        text.append(f"  failed {s.n_failed}", style="red" if s.n_failed else "")
        text.append(f"  done {s.n_done}")
        if self.log_mtime is not None:
            age = now - self.log_mtime
            stale = s.status in (RunStatus.RUNNING, RunStatus.DRAINING) and age > 600
            text.append(
                f"   log written {fmt_duration(age)} ago",
                style="bold yellow" if stale else "dim",
            )
        if self.last_poll is not None:
            text.append(f"   polled {fmt_duration(now - self.last_poll)} ago", style="dim")
        else:
            text.append("   reading log…", style="yellow")
        if self.message:
            text.append(f"\n{self.message}", style="yellow")
        self.query_one("#header", Static).update(text)
        bar = self.query_one("#progress", ProgressBar)
        if s.steps_total:
            bar.update(total=s.steps_total, progress=s.steps_done)

    # ----------------------------------------------------------------- actions
    MAIN_SCREEN_ACTIONS: ClassVar[frozenset[str]] = frozenset(
        {"show_tab", "open_log", "refresh_now"}
    )

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        """Disable (and hide) main-screen keys while the log viewer is open."""
        if action in self.MAIN_SCREEN_ACTIONS and isinstance(self.screen, LogScreen):
            return False
        return True

    def action_suspend_process(self) -> None:
        """Suspend like a terminal's ctrl+z: stop the whole foreground process group.

        Textual's built-in version only stops this process, so under ``uv run``/``uvx``
        the parent ``uv`` keeps the terminal and the shell prompt doesn't come back.
        The terminal is restored *before* signalling: once ``uv`` stops, the shell takes
        the terminal, and touching it from the background would stop us mid-cleanup.
        ``App.suspend`` restores the terminal, then redraws when we are continued.
        """
        if self._driver is None or not self._driver.can_suspend:
            return
        with self.suspend():
            os.killpg(os.getpgrp(), signal.SIGTSTP)

    def action_show_tab(self, table_id: str) -> None:
        self.query_one(TabbedContent).active = f"tab-{table_id}"
        self.query_one(f"#{table_id}", DataTable).focus()

    def action_refresh_now(self) -> None:
        self._wake.set()

    def action_open_log(self, kind: str | None = None) -> None:
        job = self._selected_job()
        if job is not None:
            self.push_screen(LogScreen(job, self.workflow_dir, SourceKind(kind) if kind else None))

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        self.action_open_log()

    def on_tabbed_content_tab_activated(self, event: TabbedContent.TabActivated) -> None:
        pane_id = event.pane.id or ""
        table_id = pane_id.removeprefix("tab-")
        if table_id in TABLE_IDS.values():
            self.query_one(f"#{table_id}", DataTable).focus()

    def _selected_job(self) -> Job | None:
        pane = self.query_one(TabbedContent).active
        table = self.query_one(f"#{pane.removeprefix('tab-')}", DataTable)
        key = _cursor_key(table)
        return self.jobs.get(int(key)) if key is not None else None


def _cursor_key(table: DataTable[Any]) -> str | None:
    if table.row_count == 0:
        return None
    try:
        row_key, _ = table.coordinate_to_cell_key(table.cursor_coordinate)
    except Exception:
        return None
    return row_key.value


def _restore_cursor(table: DataTable[Any], key: str | None) -> None:
    """Keep the cursor on the same job after rows were added/removed/sorted."""
    if key is None or table.row_count == 0:
        return
    try:
        index = table.get_row_index(key)
    except Exception:
        return  # the row moved to another table; leave the cursor where it is
    if index != table.cursor_row:
        table.move_cursor(row=index, animate=False)
