"""Textual App for snakesee TUI."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from enum import StrEnum
from pathlib import Path
from typing import ClassVar

from textual.app import App
from textual.app import ComposeResult
from textual.binding import Binding
from textual.binding import BindingType
from textual.containers import Container
from textual.containers import Horizontal
from textual.reactive import reactive
from textual.timer import Timer
from textual.widgets import DataTable
from textual.widgets import Footer
from textual.widgets import Input
from textual.widgets import Static

from snakesee.constants import DEFAULT_REFRESH_RATE
from snakesee.constants import MAX_REFRESH_RATE
from snakesee.constants import MIN_REFRESH_RATE
from snakesee.formatting import format_duration
from snakesee.models import TimeEstimate
from snakesee.models import WeightingStrategy
from snakesee.models import WorkflowProgress
from snakesee.tui.accessibility import ACCESSIBLE_CONFIG
from snakesee.tui.accessibility import DEFAULT_CONFIG
from snakesee.tui.accessibility import AccessibilityConfig
from snakesee.tui.data_source import SortTableName
from snakesee.tui.data_source import WorkflowDataSource
from snakesee.tui.renderables import make_header
from snakesee.tui.renderables import make_progress_panel
from snakesee.tui.renderables import make_summary_footer
from snakesee.tui.screens import EasterEggScreen
from snakesee.tui.screens import HelpScreen
from snakesee.tui.screens import JobLogScreen
from snakesee.tui.tables import completion_rows
from snakesee.tui.tables import failed_rows
from snakesee.tui.tables import incomplete_rows
from snakesee.tui.tables import pending_rows
from snakesee.tui.tables import running_rows
from snakesee.tui.tables import sort_rows
from snakesee.tui.tables import sort_stats_rows
from snakesee.tui.tables import stats_rows


class LayoutMode(Enum):
    """Available TUI layout modes."""

    FULL = "full"
    COMPACT = "compact"
    MINIMAL = "minimal"


class SortTable(StrEnum):
    """Sortable DataTable identifiers; values match the widget IDs in compose()."""

    RUNNING = "running"
    COMPLETIONS = "completions"
    PENDING = "pending"
    STATS = "stats"


# Maximum sortable column index per table (exclusive upper bound).
_SORT_MAX_COLS: dict[SortTable, int] = {
    SortTable.RUNNING: 4,
    SortTable.COMPLETIONS: 3,
    SortTable.PENDING: 2,
    SortTable.STATS: 4,
}

# Forward cycle order for the `s` / `S` sort-target bindings.
_SORT_CYCLE: tuple[SortTable | None, ...] = (
    None,
    SortTable.RUNNING,
    SortTable.COMPLETIONS,
    SortTable.PENDING,
    SortTable.STATS,
)

# Column index of the "Rule" cell per DataTable id. The index varies by table:
# running/completions/failed carry a leading "#" column so Rule is at index 1,
# while pending/stats lead with Rule at index 0. Tables absent from this map
# (e.g. "incomplete", which lists output files) have no Rule column, so the n/N
# filter-jump bindings are a no-op there rather than crashing or matching the
# wrong column.
_RULE_COLUMN_BY_TABLE: dict[str, int] = {
    "running": 1,
    "completions": 1,
    "failed": 1,
    "pending": 0,
    "stats": 0,
}


class SnakeseeApp(App[None]):
    """Textual application for monitoring Snakemake workflows."""

    CSS_PATH = "app.tcss"
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("q,ctrl+c", "quit", "Quit"),
        Binding("tab", "cycle_layout", "Layout", priority=True),
        Binding("s", "cycle_sort_forward", "Sort →", show=False),
        Binding("S", "cycle_sort_back", "Sort ←", show=False),
        Binding("1", "sort_column(0)", show=False),
        Binding("2", "sort_column(1)", show=False),
        Binding("3", "sort_column(2)", show=False),
        Binding("4", "sort_column(3)", show=False),
        Binding("question_mark", "show_help", "Help"),
        Binding("f", "easter_pending", show=False),
        Binding("g", "easter_complete", show=False),
        Binding("slash", "open_filter", "Filter"),
        Binding("n", "next_match", show=False),
        Binding("N", "prev_match", show=False),
        Binding("escape", "clear_filter", "Clear filter"),
        Binding("p", "toggle_pause", "Pause"),
        Binding("e", "toggle_estimation", "Estimation"),
        Binding("w", "toggle_wildcard", "Wildcard"),
        Binding("a", "toggle_accessibility", "Accessibility"),
        Binding("r", "force_refresh", "Refresh"),
        Binding("ctrl+r", "hard_refresh", show=False),
        Binding("plus,equal", "rate_inc(0.5)", show=False),
        Binding("minus", "rate_dec(0.5)", show=False),
        Binding("greater_than_sign,full_stop", "rate_inc(5.0)", show=False),
        Binding("less_than_sign,comma", "rate_dec(5.0)", show=False),
        Binding("0", "rate_reset", show=False),
        Binding("G", "rate_min", show=False),
        Binding("left_square_bracket", "log_older(1)", show=False),
        Binding("right_square_bracket", "log_newer(1)", show=False),
        Binding("left_curly_bracket", "log_older(5)", show=False),
        Binding("right_curly_bracket", "log_newer(5)", show=False),
    ]

    paused: reactive[bool] = reactive(False)
    layout_mode: reactive[LayoutMode] = reactive(LayoutMode.FULL)
    sort_table: reactive[SortTable | None] = reactive(None, init=False)
    sort_column: reactive[int] = reactive(0, init=False)
    sort_ascending: reactive[bool] = reactive(True, init=False)
    filter_text: reactive[str | None] = reactive(None, init=False)
    accessibility_mode: reactive[bool] = reactive(False, init=False)
    refresh_rate: reactive[float] = reactive(DEFAULT_REFRESH_RATE, init=False)
    current_log_index: reactive[int] = reactive(0, init=False)

    _easter_timer: Timer | None = None
    _easter_pending: bool = False
    _refresh_timer: Timer | None = None
    _last_poll: tuple[WorkflowProgress, TimeEstimate | None] | None = None

    @property
    def last_poll(self) -> tuple[WorkflowProgress, TimeEstimate | None] | None:
        """The most recent (progress, estimate) snapshot taken by the refresh cycle.

        Read-only accessor for external tooling (e.g. the docs screenshot
        generator) so it does not have to reach into the private attribute.
        Returns None until the first refresh has polled the data source.
        """
        return self._last_poll

    def action_cycle_layout(self) -> None:
        """Cycle to the next layout mode."""
        modes = list(LayoutMode)
        idx = modes.index(self.layout_mode)
        self.layout_mode = modes[(idx + 1) % len(modes)]

    def watch_layout_mode(self, old: LayoutMode, new: LayoutMode) -> None:
        """Swap the CSS class on the root when the layout mode changes."""
        for mode in LayoutMode:
            self.remove_class(f"-{mode.value}")
        self.add_class(f"-{new.value}")

    def action_cycle_sort_forward(self) -> None:
        """Cycle the sort target one step forward (None → running → … → stats → None)."""
        self._cycle_sort(direction=1)

    def action_cycle_sort_back(self) -> None:
        """Cycle the sort target one step backward (None → stats → … → running → None)."""
        self._cycle_sort(direction=-1)

    def _cycle_sort(self, direction: int) -> None:
        """Advance the sort target by ``direction`` steps and refresh once.

        With no per-attribute watchers on the sort reactives, plain assignment is
        just storage; we explicitly call ``_refresh_panels`` once after all three
        settle so a single keystroke triggers exactly one redraw.
        """
        i = _SORT_CYCLE.index(self.sort_table)
        self.sort_table = _SORT_CYCLE[(i + direction) % len(_SORT_CYCLE)]
        self.sort_column = 0
        self.sort_ascending = True
        self._refresh_panels(ignore_pause=True)

    def action_sort_column(self, col: int) -> None:
        """Set the sort column for the active sort target, or toggle direction if same column.

        Columns are 0-indexed.  Each table enforces its own maximum:
        running and stats support columns 0-3; completions 0-2; pending 0-1.

        Args:
            col: Zero-based column index to sort by.
        """
        if self.sort_table is None:
            return
        if col >= _SORT_MAX_COLS[self.sort_table]:
            return
        if col == self.sort_column:
            self.sort_ascending = not self.sort_ascending
        else:
            self.sort_column = col
            self.sort_ascending = True
        self._refresh_panels(ignore_pause=True)

    def action_show_help(self) -> None:
        """Push the modal HelpScreen overlay."""
        self.push_screen(HelpScreen())

    def action_easter_pending(self) -> None:
        """Start (or restart) the 2-second window for completing the f-then-g easter egg."""
        self._easter_pending = True
        if self._easter_timer is not None:
            self._easter_timer.stop()
        self._easter_timer = self.set_timer(2.0, self._clear_easter)

    def _clear_easter(self) -> None:
        """Reset the easter-egg pending state when the 2-second window elapses."""
        self._easter_pending = False
        self._easter_timer = None

    def action_easter_complete(self) -> None:
        """Push the EasterEggScreen if the f-then-g chord finished within the window."""
        if self._easter_pending:
            self._easter_pending = False
            if self._easter_timer is not None:
                self._easter_timer.stop()
                self._easter_timer = None
            self.push_screen(EasterEggScreen())

    def action_open_filter(self) -> None:
        """Reveal the filter Input widget and focus it for keyboard entry."""
        f = self.query_one("#filter", Input)
        f.can_focus = True
        f.add_class("-active")
        f.focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        """Apply the submitted filter text and hide the Input again."""
        if event.input.id != "filter":
            return
        self.filter_text = event.value or None
        event.input.remove_class("-active")
        event.input.value = ""
        self.set_focus(None)
        event.input.can_focus = False

    def action_clear_filter(self) -> None:
        """Hide the filter Input if focused, otherwise clear the filter and return to latest log."""
        focused = self.focused
        if isinstance(focused, Input) and focused.id == "filter":
            focused.remove_class("-active")
            focused.value = ""
            self.set_focus(None)
            focused.can_focus = False
            return
        self.filter_text = None
        self.current_log_index = 0

    def watch_filter_text(self, old: str | None, new: str | None) -> None:
        """Re-populate all panels when the filter text changes.

        Filter changes only come from user keystrokes, so the redraw bypasses
        the pause guard.
        """
        self._refresh_panels(ignore_pause=True)

    def action_next_match(self) -> None:
        """Move the cursor to the next row whose Rule column matches the filter."""
        self._jump_match(direction=1)

    def action_prev_match(self) -> None:
        """Move the cursor to the previous row whose Rule column matches the filter."""
        self._jump_match(direction=-1)

    def action_toggle_pause(self) -> None:
        """Toggle the paused state of the monitor and repaint immediately.

        The repaint bypasses the pause guard so the header's PAUSED indicator
        updates right away instead of waiting for the next interval tick.
        """
        self.paused = not self.paused
        self._refresh_panels(ignore_pause=True)

    def action_toggle_estimation(self) -> None:
        """Toggle time estimation and re-initialize the estimator in a worker thread."""
        self._data.use_estimation = not self._data.use_estimation
        self.run_worker(self._reinit_estimator, thread=True, exclusive=True)

    def action_toggle_wildcard(self) -> None:
        """Toggle wildcard conditioning and re-initialize the estimator in a worker thread."""
        self._data.use_wildcard_conditioning = not self._data.use_wildcard_conditioning
        self.run_worker(self._reinit_estimator, thread=True, exclusive=True)

    def action_toggle_accessibility(self) -> None:
        """Toggle accessibility mode, updating the accessibility config and refreshing panels.

        Toggling off restores the constructor-supplied config (which may be a
        custom override), not necessarily ``DEFAULT_CONFIG``.
        """
        self.accessibility_mode = not self.accessibility_mode
        self._accessibility_config = (
            ACCESSIBLE_CONFIG if self.accessibility_mode else self._base_accessibility_config
        )
        self._refresh_panels(ignore_pause=True)

    def action_force_refresh(self) -> None:
        """Force an immediate panel refresh, even when paused."""
        self._refresh_panels(ignore_pause=True)

    def action_hard_refresh(self) -> None:
        """Re-initialize the estimator in a worker thread and refresh panels."""
        self.run_worker(self._reinit_estimator, thread=True, exclusive=True)

    def watch_refresh_rate(self, old: float, new: float) -> None:
        """Restart the polling timer when the refresh rate changes."""
        if self._refresh_timer is not None:
            self._refresh_timer.stop()
        self._refresh_timer = self.set_interval(new, self._refresh_panels)
        self._data.refresh_rate = new
        self._data.update_cache_ttl()

    def action_rate_inc(self, delta: float) -> None:
        """Increase the refresh rate by ``delta`` seconds, clamped to MAX_REFRESH_RATE."""
        self.refresh_rate = min(MAX_REFRESH_RATE, self.refresh_rate + delta)

    def action_rate_dec(self, delta: float) -> None:
        """Decrease the refresh rate by ``delta`` seconds, clamped to MIN_REFRESH_RATE."""
        self.refresh_rate = max(MIN_REFRESH_RATE, self.refresh_rate - delta)

    def action_rate_reset(self) -> None:
        """Reset the refresh rate to the default value."""
        self.refresh_rate = DEFAULT_REFRESH_RATE

    def action_rate_min(self) -> None:
        """Set the refresh rate to the minimum value."""
        self.refresh_rate = MIN_REFRESH_RATE

    def watch_current_log_index(self, old: int, new: int) -> None:
        """Sync the log index to the data source and refresh panels when it changes.

        Log navigation only comes from user keystrokes, so the redraw bypasses
        the pause guard.
        """
        self._data.current_log_index = new
        self._refresh_panels(ignore_pause=True)

    def action_log_older(self, step: int) -> None:
        """Navigate to an older log file by ``step`` entries.

        Args:
            step: Number of log entries to step backward (toward older logs).
        """
        self._data.refresh_log_list()
        max_idx = self._data.available_log_count - 1
        if max_idx < 0:
            self.current_log_index = 0
            return
        self.current_log_index = min(max_idx, self.current_log_index + step)

    def action_log_newer(self, step: int) -> None:
        """Navigate to a newer log file by ``step`` entries.

        Args:
            step: Number of log entries to step forward (toward newer logs).
        """
        self.current_log_index = max(0, self.current_log_index - step)

    def _reinit_estimator(self) -> None:
        """Re-initialize the estimator (runs on a worker thread) then refresh panels.

        Loads silently: the Textual UI is already on screen, so the startup Rich
        progress spinner would corrupt the display.

        The refresh bypasses the pause guard: every path here is an explicit user
        action (Ctrl+R, estimation/wildcard toggles), so the result should render
        even while auto-refresh is paused.
        """
        self._data.init_estimator(show_progress=False)
        self.call_from_thread(lambda: self._refresh_panels(ignore_pause=True))

    def _sort_table_name(self) -> SortTableName | None:
        """Return the current sort target as the Literal alias the data source expects."""
        return self.sort_table.value if self.sort_table is not None else None

    def _jump_match(self, direction: int) -> None:
        """Jump the focused (or running) DataTable's cursor to the next/prev match.

        The filter matches against the table's Rule column, whose index varies by
        table (see ``_RULE_COLUMN_BY_TABLE``). Tables with no Rule column (e.g.
        ``#incomplete``) are skipped so a global n/N press can neither raise
        ``IndexError`` nor match against the wrong column.

        Args:
            direction: 1 to step forward, -1 to step backward.
        """
        if not self.filter_text:
            return
        focused = self.focused
        table = focused if isinstance(focused, DataTable) else self.query_one("#running", DataTable)
        rule_col = _RULE_COLUMN_BY_TABLE.get(table.id or "")
        if rule_col is None:
            return
        n = table.row_count
        if n == 0:
            return
        needle = self.filter_text.lower()
        start = (table.cursor_row + direction) % n
        i = start
        for _ in range(n):
            row = table.get_row_at(i)
            if rule_col < len(row) and needle in str(row[rule_col]).lower():
                table.move_cursor(row=i)
                return
            i = (i + direction) % n

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        """Open the JobLogScreen for the selected job in the running/completions tables."""
        table_id = event.data_table.id
        if table_id not in {SortTable.RUNNING.value, SortTable.COMPLETIONS.value}:
            return
        # Reuse the latest poll snapshot — populated by _refresh_panels at most
        # `refresh_rate` seconds ago, which is also the data the table was rendered from.
        if self._last_poll is None:
            self._last_poll = self._data.poll_state()
        progress, _ = self._last_poll
        if table_id == SortTable.RUNNING.value:
            jobs = self._data.get_running_jobs_list(
                progress,
                filter_text=self.filter_text,
                sort_table=self._sort_table_name(),
                sort_column=self.sort_column,
                sort_ascending=self.sort_ascending,
            )
        else:  # completions
            jobs, _ = self._data.get_completions_list(
                progress,
                filter_text=self.filter_text,
                sort_table=self._sort_table_name(),
                sort_column=self.sort_column,
                sort_ascending=self.sort_ascending,
            )
        if event.cursor_row >= len(jobs):
            return
        job = jobs[event.cursor_row]
        log_path = job.log_file
        if log_path is None:
            return
        lines = self._data.read_log_tail(log_path, max_lines=500)
        self.push_screen(JobLogScreen(log_path, lines))

    def __init__(
        self,
        workflow_dir: Path,
        refresh_rate: float = DEFAULT_REFRESH_RATE,
        use_estimation: bool = True,
        profile_path: Path | None = None,
        use_wildcard_conditioning: bool = True,
        weighting_strategy: WeightingStrategy = "index",
        half_life_logs: int = 10,
        half_life_days: float = 7.0,
        accessibility_config: AccessibilityConfig | None = None,
    ) -> None:
        """Initialize the SnakeseeApp.

        Args:
            workflow_dir: Path to workflow directory containing ``.snakemake/``.
            refresh_rate: Refresh interval in seconds.
            use_estimation: Whether to enable time estimation.
            profile_path: Optional path to a timing profile for bootstrapping estimates.
            use_wildcard_conditioning: Whether to enable wildcard-conditioned estimates.
            weighting_strategy: Strategy for weighting historical data ("index" or "time").
            half_life_logs: Half-life in run count for index-based weighting.
            half_life_days: Half-life in days for time-based weighting.
            accessibility_config: Optional accessibility configuration override.
        """
        super().__init__()
        self._data = WorkflowDataSource(
            workflow_dir=workflow_dir,
            refresh_rate=refresh_rate,
            use_estimation=use_estimation,
            profile_path=profile_path,
            use_wildcard_conditioning=use_wildcard_conditioning,
            weighting_strategy=weighting_strategy,
            half_life_logs=half_life_logs,
            half_life_days=half_life_days,
        )
        # Keep the constructor-supplied config around so toggling accessibility
        # off restores it rather than falling back to DEFAULT_CONFIG.
        self._base_accessibility_config = accessibility_config or DEFAULT_CONFIG
        self._accessibility_config = self._base_accessibility_config
        # Resolve the workflow path once; the header truncates it per frame, so the
        # per-render cost stays a string slice rather than a filesystem resolve().
        self._resolved_workflow_dir = str(workflow_dir.resolve())
        self.refresh_rate = refresh_rate

    def compose(self) -> ComposeResult:
        """Compose the widget tree (header / progress / six tables / summary / footer)."""
        yield Static(id="header")
        yield Static(id="progress")
        with Container(id="body"):
            with Horizontal(id="left"):
                yield DataTable(id="running")
                yield DataTable(id="completions")
            with Horizontal(id="right"):
                yield DataTable(id="pending")
                yield DataTable(id="failed")
                yield DataTable(id="incomplete")
                yield DataTable(id="stats")
        yield Static(id="summary")
        filter_input = Input(placeholder="filter rules…", id="filter")
        filter_input.can_focus = False
        yield filter_input
        yield Footer()

    def on_mount(self) -> None:
        """Configure tables, populate panels, and start the refresh timer."""
        running = self.query_one("#running", DataTable)
        running.add_columns("#", "Rule", "Thr", "Started", "Elapsed", "Progress", "ETA")
        running.cursor_type = "row"
        completions = self.query_one("#completions", DataTable)
        completions.add_columns("#", "Rule", "Thr", "Duration", "Completed")
        completions.cursor_type = "row"
        self.query_one("#pending", DataTable).add_columns("Rule", "Est. Count")
        self.query_one("#failed", DataTable).add_columns("#", "Rule", "Job ID")
        self.query_one("#incomplete", DataTable).add_columns("Output File")
        self.query_one("#stats", DataTable).add_columns("Rule", "Thr", "Count", "Avg", "Std Dev")
        self._refresh_panels()
        # __init__ already triggered watch_refresh_rate which started a timer; stop it
        # before mounting our own so we don't end up with two interval callbacks racing.
        if self._refresh_timer is not None:
            self._refresh_timer.stop()
        self._refresh_timer = self.set_interval(self.refresh_rate, self._refresh_panels)
        self.add_class(f"-{self.layout_mode.value}")

    def _refresh_panels(self, ignore_pause: bool = False) -> None:
        """Poll the data source and update header/progress/summary/tables widgets.

        Args:
            ignore_pause: When True, refresh even if ``paused`` is set. Used by every
                explicit user-triggered redraw (force refresh, pause toggle, sort,
                filter, log nav, accessibility, estimator re-init) — pausing gates
                only the interval timer's automatic polling.
        """
        if self.paused and not ignore_pause:
            return
        progress, estimate = self._data.poll_state()
        self._last_poll = (progress, estimate)
        self.query_one("#header", Static).update(
            make_header(
                progress,
                self._resolved_workflow_dir,
                self.paused,
                self._data.event_reader,
                max_path_len=max(20, self.size.width - 80),
            )
        )
        self.query_one("#progress", Static).update(
            make_progress_panel(
                progress,
                estimate,
                self._data.use_estimation,
                self._accessibility_config,
            )
        )
        self.query_one("#summary", Static).update(make_summary_footer(progress))

        self._populate_running(progress)
        self._populate_completions(progress)
        self._populate_pending(progress)
        self._populate_failed(progress)
        self._populate_incomplete(progress)
        self._populate_stats()

    def _populate_running(self, progress: WorkflowProgress) -> None:
        """Populate the running-jobs table from the current workflow progress."""
        table = self.query_one("#running", DataTable)
        table.clear()
        jobs = self._data.get_running_jobs_list(
            progress,
            filter_text=self.filter_text,
            sort_table=self._sort_table_name(),
            sort_column=self.sort_column,
            sort_ascending=self.sort_ascending,
        )
        rows = running_rows(self._data.build_running_job_data(jobs))
        for idx, row in enumerate(rows):
            job = row.job
            elapsed_str = (
                format_duration(row.elapsed_seconds) if row.elapsed_seconds is not None else "?"
            )
            remaining_str = (
                f"~{format_duration(row.remaining_seconds)}"
                if row.remaining_seconds is not None
                else "?"
            )
            started_str = "?"
            if job.start_time is not None:
                started_str = datetime.fromtimestamp(job.start_time).strftime("%H:%M:%S")

            progress_str = "-"
            if row.tool_progress is not None:
                if row.tool_progress.percent_complete is not None:
                    progress_str = row.tool_progress.percent_str
                else:
                    progress_str = f"{row.tool_progress.items_processed:,} {row.tool_progress.unit}"

            threads_str = str(job.threads) if job.threads is not None else "-"
            job_id_str = str(job.job_id) if job.job_id else str(idx + 1)
            table.add_row(
                job_id_str,
                job.rule,
                threads_str,
                started_str,
                elapsed_str,
                progress_str,
                remaining_str,
            )

    def _populate_completions(self, progress: WorkflowProgress) -> None:
        """Populate the recent-completions table from the current workflow progress."""
        table = self.query_one("#completions", DataTable)
        table.clear()
        jobs, failed_job_ids = self._data.get_completions_list(
            progress,
            filter_text=self.filter_text,
            sort_table=self._sort_table_name(),
            sort_column=self.sort_column,
            sort_ascending=self.sort_ascending,
        )
        rows = completion_rows(jobs, failed_job_ids)
        for idx, row in enumerate(rows):
            job = row.job
            duration_str = format_duration(job.duration) if job.duration is not None else "?"
            threads_str = str(job.threads) if job.threads is not None else "-"
            completed_str = "?"
            if job.end_time is not None:
                completed_str = datetime.fromtimestamp(job.end_time).strftime("%H:%M:%S")
            job_id_str = str(job.job_id) if job.job_id else str(idx + 1)
            table.add_row(job_id_str, job.rule, threads_str, duration_str, completed_str)

    def _populate_pending(self, progress: WorkflowProgress) -> None:
        """Populate the pending-jobs table using inferred per-rule pending counts."""
        table = self.query_one("#pending", DataTable)
        table.clear()
        pending_rules = self._data.get_inferred_pending_rules(progress)
        if not pending_rules:
            return
        rows = pending_rows(pending_rules)
        if self.sort_table == SortTable.PENDING:
            rows = sort_rows(rows, self.sort_column, self.sort_ascending)
        for row in rows:
            table.add_row(row.rule, str(row.job_count))

    def _populate_failed(self, progress: WorkflowProgress) -> None:
        """Populate the failed-jobs table from ``progress.failed_jobs_list``."""
        table = self.query_one("#failed", DataTable)
        table.clear()
        rows = failed_rows(progress)
        for idx, row in enumerate(rows):
            job = row.job
            job_id_str = job.job_id if job.job_id else "-"
            table.add_row(str(idx + 1), job.rule, job_id_str)

    def _populate_incomplete(self, progress: WorkflowProgress) -> None:
        """Populate the incomplete-jobs table from ``progress.incomplete_jobs_list``."""
        table = self.query_one("#incomplete", DataTable)
        table.clear()
        for row in incomplete_rows(progress):
            table.add_row(row.display_path)

    def _populate_stats(self) -> None:
        """Populate the rule-statistics table from the data source's filtered stats."""
        table = self.query_one("#stats", DataTable)
        table.clear()
        if not self._data.use_estimation:
            return
        stats_list = self._data.get_filtered_stats()
        if not stats_list:
            return
        # Default ordering: most-frequently-run rules first.
        stats_list = sorted(stats_list, key=lambda s: s.count, reverse=True)
        rows = stats_rows(stats_list, self._data.thread_stats_dict())
        if self.sort_table == SortTable.STATS:
            rows = sort_stats_rows(rows, self.sort_column, self.sort_ascending)
        for row in rows:
            table.add_row(
                row.rule_display,
                row.threads,
                str(row.stats.count),
                format_duration(row.stats.mean_duration),
                format_duration(row.stats.std_dev) if row.stats.std_dev > 0 else "-",
            )
