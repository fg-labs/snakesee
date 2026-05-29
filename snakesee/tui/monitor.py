"""Rich TUI for Snakemake workflow monitoring."""

import heapq
import itertools
import logging
import math
import sys
import time
from collections.abc import Iterable
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING
from typing import Any

if TYPE_CHECKING:
    from snakesee.types import ProgressCallback

from rich.console import Console
from rich.console import Group
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.progress import BarColumn
from rich.progress import MofNCompleteColumn
from rich.progress import Progress
from rich.progress import SpinnerColumn
from rich.progress import TextColumn
from rich.table import Table
from rich.text import Text

from snakesee.constants import ADAPTIVE_CACHE_TTL_MULTIPLIER
from snakesee.constants import DEFAULT_REFRESH_RATE
from snakesee.constants import MAX_CACHE_TTL
from snakesee.constants import MAX_REFRESH_RATE
from snakesee.constants import MIN_REFRESH_RATE
from snakesee.estimator import TimeEstimator
from snakesee.events import EventReader
from snakesee.events import EventType
from snakesee.events import SnakeseeEvent
from snakesee.events import get_event_file_path
from snakesee.models import JobInfo
from snakesee.models import RuleTimingStats
from snakesee.models import TimeEstimate
from snakesee.models import WeightingStrategy
from snakesee.models import WorkflowProgress
from snakesee.models import WorkflowStatus
from snakesee.models import format_duration
from snakesee.parser import IncrementalLogReader
from snakesee.parser import parse_workflow_state
from snakesee.plugins import parse_tool_progress
from snakesee.plugins.base import ToolProgress
from snakesee.state.clock import get_clock
from snakesee.state.paths import WorkflowPaths
from snakesee.state.workflow_state import WorkflowState
from snakesee.tui.accessibility import ACCESSIBLE_CONFIG
from snakesee.tui.accessibility import DEFAULT_CONFIG
from snakesee.tui.accessibility import AccessibilityConfig
from snakesee.tui.accessibility import BarStyle
from snakesee.validation import EventAccumulator
from snakesee.validation import ValidationLogger
from snakesee.validation import compare_states

logger = logging.getLogger(__name__)

# Fulcrum Genomics brand colors
FG_BLUE = "#26a8e0"
FG_GREEN = "#38b44a"

# Fulcrum Genomics logo path (easter egg)
FG_LOGO_PATH = Path(__file__).parent.parent / "assets" / "logo.png"


def _is_event_file_current(event_file: Path, log_start_time: float | None) -> bool:
    """Check if the event file belongs to the current workflow run.

    The events file should contain a workflow_started event with a timestamp
    that matches the current log file's timeframe. If the events file is
    stale (from a previous run), it should be ignored.

    Args:
        event_file: Path to the events file.
        log_start_time: Start time of the current log file (first timestamp or ctime).

    Returns:
        True if the events file appears to be from the current workflow.
    """
    import orjson

    if log_start_time is None:
        # No log start time - can't validate, assume events are current
        return True

    try:
        with open(event_file, "r", encoding="utf-8") as f:
            first_line = f.readline().strip()
            if not first_line:
                return False

            data = orjson.loads(first_line)
            if data.get("event_type") != "workflow_started":
                # First event isn't workflow_started - stale file
                logger.debug("Events file missing workflow_started as first event")
                return False

            event_timestamp = data.get("timestamp", 0)

            # Events file is considered current if its workflow_started timestamp
            # is within 60 seconds of the log start time (allowing for clock drift)
            # or if it's newer than the log
            time_diff = event_timestamp - log_start_time
            if time_diff >= -60:  # Event is at or after log start (with 60s tolerance)
                return True

            logger.debug(
                "Events file is stale: workflow_started at %.1f, log starts at %.1f (diff: %.1fs)",
                event_timestamp,
                log_start_time,
                time_diff,
            )
            return False

    except (OSError, orjson.JSONDecodeError, KeyError) as e:
        logger.debug("Could not validate events file %s: %s", event_file, e)
        return False


class LayoutMode(Enum):
    """Available TUI layout modes."""

    FULL = "full"
    COMPACT = "compact"
    MINIMAL = "minimal"


class WorkflowMonitorTUI:
    """
    Rich TUI for monitoring Snakemake workflows.

    Provides a full-screen terminal interface with:
    - Progress bar showing overall completion
    - Table of currently running jobs
    - Recent completions
    - Time estimation with confidence bounds

    Keyboard Controls (vim-like):
        q: Quit
        ?: Show help
        p: Pause/resume auto-refresh
        e: Toggle time estimation
        w: Toggle wildcard conditioning (estimate per sample/batch)
        a: Toggle colorblind-accessible mode
        r: Force refresh
        Ctrl+r: Hard refresh (reload historical data)

        Refresh rate:
        -: Decrease by 0.5s (faster)
        +: Increase by 0.5s (slower)
        <: Decrease by 5s (faster)
        >: Increase by 5s (slower)
        0: Reset to default (1s)
        G: Set to minimum (0.5s, fastest)

        Layout:
        Tab: Cycle layout mode (full/compact/minimal)

        Filter:
        /: Enter filter mode (filter rules by name)
        n: Next filter match
        N: Previous filter match
        Esc: Clear filter, return to latest log

        Log Navigation:
        [: View older log/execution (1 step)
        ]: View newer log/execution (1 step)
        {: View older log/execution (5 steps)
        }: View newer log/execution (5 steps)

        Table Sorting:
        s: Cycle sort table forward (Running -> Completions -> Pending -> Stats -> none)
        S: Cycle sort table backward
        1-4: Sort by column (press again to reverse)

    Attributes:
        workflow_dir: Path to the workflow directory.
        refresh_rate: How often to refresh the display (seconds).
        use_estimation: Whether to use historical time estimation.
    """

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
        """
        Initialize the TUI.

        Args:
            workflow_dir: Path to workflow directory containing .snakemake/.
            refresh_rate: Refresh interval in seconds.
            use_estimation: Whether to enable time estimation.
            profile_path: Optional path to a timing profile for bootstrapping estimates.
            use_wildcard_conditioning: Whether to enable wildcard-conditioned estimates.
            weighting_strategy: Strategy for weighting historical data ("index" or "time").
            half_life_logs: Half-life in run count for index-based weighting.
            half_life_days: Half-life in days for time-based weighting.
            accessibility_config: Visual encoding config for colorblind accessibility.
                Defaults to DEFAULT_CONFIG (standard block characters).
        """
        self.workflow_dir = workflow_dir
        self.refresh_rate = refresh_rate
        self.use_estimation = use_estimation
        self.profile_path = profile_path
        self.weighting_strategy = weighting_strategy
        self.half_life_logs = half_life_logs
        self.half_life_days = half_life_days
        self._accessibility_config = accessibility_config or DEFAULT_CONFIG
        self.console = Console()
        self._running = True
        self._estimator: TimeEstimator | None = None
        self._force_refresh = False

        # Wildcard conditioning toggle
        self._use_wildcard_conditioning: bool = use_wildcard_conditioning

        # New state for vim-like features
        self._paused: bool = False
        self._show_help: bool = False
        self._layout_mode: LayoutMode = LayoutMode.FULL
        self._filter_text: str | None = None
        self._filter_mode: bool = False
        self._filter_input = ""
        self._filter_matches: list[str] = []
        self._filter_index = 0

        # Log file navigation
        self._available_logs: list[Path] = []
        self._current_log_index: int = 0  # 0 = most recent
        self._latest_log_path: Path | None = None  # Track latest log to detect new workflows
        self._refresh_log_list()

        # Table sorting state
        self._sort_table: str | None = None  # "running", "completions", "pending", or "stats"
        self._sort_column: int = 0  # 0-indexed column
        self._sort_ascending: bool = True

        # Easter egg state
        self._easter_egg_pending_f = False  # True if 'f' was just pressed
        self._show_easter_egg = False

        # Cutoff time for historical view (updated in _poll_state)
        self._cutoff_time: float | None = None

        # Job log viewer state (two nested modes)
        # Normal → [Enter] → Table Mode → [Enter] → Log Mode
        #                        ↑                      |
        #                        └──────── [Esc] ───────┘
        #                    [Esc] → Normal
        self._job_selection_mode: bool = False  # True when in table navigation mode
        self._log_viewing_mode: bool = False  # True when viewing a job's log (nested)
        self._log_source: str | None = None  # "running" or "completions"
        self._selected_job_index: int = 0  # Index into running_jobs list
        self._selected_completion_index: int = 0  # Index into completions list
        self._running_scroll_offset: int = 0  # Scroll offset for running jobs table
        self._completions_scroll_offset: int = 0  # Scroll offset for completions table
        self._selected_pending_index: int = 0  # Index into pending rules list
        self._selected_stats_index: int = 0  # Index into stats rows list
        self._pending_scroll_offset: int = 0  # Scroll offset for pending table
        self._stats_scroll_offset: int = 0  # Scroll offset for stats table
        self._left_panel_count: int = 2  # Number of panels in left column
        self._right_panel_count: int = 2  # Number of panels in right column
        self._log_scroll_offset: int = 0  # Lines to skip from end (0 = show latest)
        self._log_scroll_page_size: int = 10  # Lines to scroll with Ctrl+u/d
        self._cached_log_path: Path | None = None
        self._cached_log_lines: list[str] = []
        self._cached_log_mtime: float = 0

        # Tool progress cache (to avoid parsing job logs on every refresh)
        # Cache stores: (cached_time, file_mtime, progress) - invalidates if file changes
        self._tool_progress_cache: dict[str, tuple[float, float, ToolProgress | None]] = {}
        # Adaptive TTL: scales with refresh rate to avoid cache outliving refresh cycles
        self._tool_progress_cache_ttl: float = min(
            ADAPTIVE_CACHE_TTL_MULTIPLIER * refresh_rate, MAX_CACHE_TTL
        )

        # Event reader for real-time events from logger plugin
        self._event_reader: EventReader | None = None
        self._events_enabled: bool = True
        self._init_event_reader()

        # All scheduled jobs from log (for pending job estimation without logger plugin)
        self._all_scheduled_jobs: dict[str, JobInfo] = {}

        # Incremental log reader for efficient polling
        self._log_reader: IncrementalLogReader | None = None
        self._init_log_reader()

        # Validation: compare event-based state with parsed state
        self._event_accumulator: EventAccumulator | None = None
        self._validation_logger: ValidationLogger | None = None
        self._init_validation()

        # Centralized workflow state
        self._workflow_state: WorkflowState = WorkflowState.create(
            workflow_dir=workflow_dir,
        )

        self._init_estimator()

    def _refresh_log_list(self) -> None:
        """Refresh the list of available log files."""
        log_dir = self.workflow_dir / ".snakemake" / "log"
        if log_dir.exists():
            # Sort by modification time, newest first
            logs = sorted(
                log_dir.glob("*.snakemake.log"),
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )
            self._available_logs = logs
        else:
            self._available_logs = []

        # Reset to most recent if current index is out of bounds
        if self._current_log_index >= len(self._available_logs):
            self._current_log_index = 0

        # Detect when a new workflow starts (new latest log)
        # and re-parse current_rules to filter pending jobs correctly
        new_latest = self._available_logs[0] if self._available_logs else None
        if new_latest != self._latest_log_path:
            self._latest_log_path = new_latest
            self._init_current_rules_from_log()

    def _get_current_log(self) -> Path | None:
        """Get the currently selected log file."""
        if not self._available_logs:
            return None
        if self._current_log_index < len(self._available_logs):
            return self._available_logs[self._current_log_index]
        return self._available_logs[0] if self._available_logs else None

    def _init_estimator(self) -> None:
        """Initialize or reinitialize the time estimator with progress display."""
        self._workflow_state.rules.clear()
        self._workflow_state.jobs.clear()

        if not self.use_estimation:
            self._estimator = None
            return

        self._estimator = TimeEstimator(
            use_wildcard_conditioning=self._use_wildcard_conditioning,
            weighting_strategy=self.weighting_strategy,
            half_life_logs=self.half_life_logs,
            half_life_days=self.half_life_days,
            rule_registry=self._workflow_state.rules,
        )

        metadata_dir = self.workflow_dir / ".snakemake" / "metadata"
        has_metadata_fs = metadata_dir.exists()
        has_profile = self.profile_path is not None and self.profile_path.exists()

        # Check if there's anything to load (worth showing progress)
        paths = WorkflowPaths(self.workflow_dir)
        has_metadata_db = paths.has_metadata_db
        has_metadata = has_metadata_fs or has_metadata_db
        log_paths = paths.find_all_logs()

        # Skip progress display if nothing to load
        if not has_metadata and not has_profile and not log_paths:
            return

        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            MofNCompleteColumn(),
            console=self.console,
            transient=True,
        ) as progress:
            # Load from profile first if available
            if has_profile:
                task = progress.add_task("Loading profile...", total=1)
                try:
                    from snakesee.profile import load_profile

                    profile = load_profile(self.profile_path)
                    self._estimator.rule_stats = profile.to_rule_stats()
                except (OSError, ValueError) as e:
                    # Log failure and fall back to metadata only
                    logger.debug("Failed to load profile %s: %s", self.profile_path, e)
                progress.update(task, completed=1)

            # Load metadata via persistence backend (supports both FS and DB)
            from snakesee.persistence import detect_backend

            backend = detect_backend(self.workflow_dir)

            if has_metadata:
                # Determine progress bar total from FS file count (DB has no cheap count)
                if has_metadata_fs and not has_metadata_db:
                    metadata_files = list(metadata_dir.rglob("*"))
                    metadata_files = [f for f in metadata_files if f.is_file()]
                    file_count = len(metadata_files)
                else:
                    file_count = 0

                task = progress.add_task(
                    "Loading metadata...", total=file_count if file_count > 0 else None
                )

                def metadata_cb(current: int, _total: int) -> None:
                    progress.update(task, completed=current)

                self._estimator.load_from_backend(backend, progress_callback=metadata_cb)

            # Load historical timing from events file (complements metadata)
            events_file = get_event_file_path(self.workflow_dir)
            if events_file.exists():
                task = progress.add_task("Loading events...", total=1)
                self._estimator.load_from_events(events_file)
                progress.update(task, completed=1)

            # Initialize thread stats from log parsing
            if log_paths:
                task = progress.add_task("Analyzing thread usage...", total=len(log_paths))
                self._init_thread_stats_from_log(
                    log_paths=log_paths,
                    progress_callback=lambda current, _total: progress.update(
                        task, completed=current
                    ),
                )

            # Parse current rules (fast, no progress needed)
            self._init_current_rules_from_log()

    def _init_thread_stats_from_log(
        self,
        log_paths: list[Path] | None = None,
        progress_callback: "ProgressCallback | None" = None,
    ) -> None:
        """Initialize thread stats from all log files (metadata doesn't have threads).

        Populates the centralized RuleRegistry with thread-specific timing data.

        Args:
            log_paths: Optional list of log paths to process (avoids re-discovering).
            progress_callback: Optional callback(current, total) for progress reporting.
        """
        from snakesee.parser import parse_completed_jobs_from_log
        from snakesee.state.paths import WorkflowPaths

        if log_paths is None:
            paths = WorkflowPaths(self.workflow_dir)
            log_paths = paths.find_all_logs()

        if not log_paths:
            return

        total = len(log_paths)
        for i, log_path in enumerate(log_paths):
            if progress_callback is not None:
                progress_callback(i + 1, total)

            for job in parse_completed_jobs_from_log(log_path):
                if job.threads is None or job.duration is None:
                    continue
                # Record to centralized RuleRegistry (includes thread info)
                self._workflow_state.rules.record_completion(
                    rule=job.rule,
                    duration=job.duration,
                    timestamp=job.end_time or 0.0,
                    threads=job.threads,
                    wildcards=dict(job.wildcards) if job.wildcards else None,
                    input_size=job.input_size,
                )

    def _init_current_rules_from_log(self) -> None:
        """Parse current rules, job counts, cores, and all scheduled jobs from the latest log."""
        from snakesee.parser import parse_all_jobs_from_log
        from snakesee.parser import parse_cores_from_log
        from snakesee.parser import parse_job_stats_counts_from_log
        from snakesee.parser import parse_job_stats_from_log

        if self._estimator is None:
            return

        paths = WorkflowPaths(self.workflow_dir)
        log_path = paths.find_latest_log()
        if log_path is None:
            return

        # Parse rule names for filtering
        current_rules = parse_job_stats_from_log(log_path)
        if current_rules:
            self._estimator.current_rules = current_rules

        # Parse job counts for accurate pending job inference. Always replace
        # (clearing to None when the latest log has no Job stats yet) so counts
        # from a previous run can't linger and be inferred onto a new run.
        self._estimator.expected_job_counts = parse_job_stats_counts_from_log(log_path) or None

        # Parse "Provided cores: N" for definitive parallelism info
        cores = parse_cores_from_log(log_path)
        if cores is not None:
            self._estimator.set_provided_cores(cores)

        # Parse all scheduled jobs with wildcards for pending job estimation
        all_jobs = parse_all_jobs_from_log(log_path)
        if all_jobs:
            self._all_scheduled_jobs = {job.job_id: job for job in all_jobs if job.job_id}

    def _init_event_reader(self) -> None:
        """Initialize the event reader if event file exists and is current.

        The events file is validated against the current log file's start time
        to ensure we don't use stale events from a previous workflow run.
        """
        if not self._events_enabled:
            return

        event_file = get_event_file_path(self.workflow_dir)
        if not event_file.exists():
            self._event_reader = None
            return

        # Get the current log file's start time for validation
        paths = WorkflowPaths(self.workflow_dir)
        log_path = paths.find_latest_log()
        log_start_time: float | None = None
        if log_path is not None:
            try:
                log_start_time = log_path.stat().st_ctime
            except OSError:
                pass

        # Validate the events file is for the current workflow run
        if _is_event_file_current(event_file, log_start_time):
            self._event_reader = EventReader(event_file)
            logger.debug("Events file is current, using for monitoring")
        else:
            self._event_reader = None
            logger.info(
                "Ignoring stale events file %s (from a previous workflow run)",
                event_file,
            )

    def _init_log_reader(self) -> None:
        """Initialize the incremental log reader.

        Creates a reader for the current log file, enabling efficient
        incremental parsing instead of re-reading the entire file on each poll.
        """
        paths = WorkflowPaths(self.workflow_dir)
        log_path = paths.find_latest_log()
        if log_path is not None:
            self._log_reader = IncrementalLogReader(log_path)
        else:
            # Create with a placeholder path; will be updated when log appears
            self._log_reader = IncrementalLogReader(paths.log_dir / "placeholder.snakemake.log")

    def _init_validation(self) -> None:
        """Initialize validation if event file exists.

        Validation is automatically enabled when the logger plugin's event
        file is detected, allowing comparison between event-based and
        parsed state to find bugs in either approach.
        """
        # Close existing validation logger to prevent file handle leaks
        if self._validation_logger is not None:
            self._validation_logger.close()
            self._validation_logger = None

        event_file = get_event_file_path(self.workflow_dir)
        if event_file.exists():
            self._event_accumulator = EventAccumulator()
            self._validation_logger = ValidationLogger(self.workflow_dir)
            self._validation_logger.log_session_start()

    def _validate_state(self, events: list[SnakeseeEvent], parsed: WorkflowProgress) -> None:
        """Compare event-based state with parsed state and log discrepancies.

        Args:
            events: New events to process.
            parsed: Current parsed workflow progress.
        """
        # Initialize validation if not yet done (event file may have appeared)
        if self._event_accumulator is None:
            self._init_validation()

        if self._event_accumulator is None or self._validation_logger is None:
            return

        # Accumulate new events
        self._event_accumulator.process_events(events)

        # Only compare if we have meaningful state from events
        if not self._event_accumulator.workflow_started:
            return

        # Compare states and log discrepancies
        discrepancies = compare_states(self._event_accumulator, parsed)

        if discrepancies:
            self._validation_logger.log_discrepancies(discrepancies)

        # Log summary periodically (every comparison for now)
        self._validation_logger.log_summary(self._event_accumulator, parsed)

    def _read_new_events(self) -> list[SnakeseeEvent]:
        """Read new events from the event file if available.

        Returns:
            List of new events, or empty list if no events or event reading disabled.
        """
        if not self._events_enabled or self._event_reader is None:
            # Try to initialize if event file now exists (with validation)
            if self._events_enabled and self._event_reader is None:
                self._init_event_reader()

            if self._event_reader is None:
                return []

        return self._event_reader.read_new_events()

    def _handle_job_submitted_event(
        self,
        event: SnakeseeEvent,
        running_jobs: list[JobInfo],
    ) -> None:
        """Handle JOB_SUBMITTED event - track pending job with wildcards."""
        from snakesee.state.job_registry import Job
        from snakesee.state.job_registry import JobStatus

        if event.job_id is None:
            return
        job_id_str = str(event.job_id)

        # Create or update job in registry with SUBMITTED status
        existing_job = self._workflow_state.jobs.get_by_job_id(job_id_str)
        if existing_job is None:
            # Create new job with SUBMITTED status
            new_job = Job(
                key=job_id_str,
                rule=event.rule_name or "unknown",
                status=JobStatus.SUBMITTED,
                job_id=job_id_str,
                wildcards=dict(event.wildcards) if event.wildcards else {},
                threads=event.threads,
            )
            self._workflow_state.jobs.add(new_job)
        else:
            # Update existing job with submitted info
            if event.wildcards:
                existing_job.wildcards = dict(event.wildcards)
            if event.threads is not None:
                existing_job.threads = event.threads

        # Also store threads for backward compatibility
        if event.threads is not None:
            self._workflow_state.jobs.store_threads(job_id_str, event.threads)

        # Update running_jobs list if this job is already running
        registry_job = self._workflow_state.jobs.get_by_job_id(job_id_str)
        threads = event.threads or (registry_job.threads if registry_job else None)
        for i, job in enumerate(running_jobs):
            if job.job_id == job_id_str:
                running_jobs[i] = JobInfo(
                    rule=job.rule,
                    job_id=job.job_id,
                    start_time=job.start_time,
                    end_time=job.end_time,
                    output_file=job.output_file,
                    wildcards=event.wildcards_dict or job.wildcards,
                    input_size=job.input_size,
                    threads=threads,
                )
                break

    def _handle_job_started_event(
        self,
        event: SnakeseeEvent,
        running_jobs: list[JobInfo],
    ) -> None:
        """Handle JOB_STARTED event - transition from SUBMITTED to RUNNING."""
        from snakesee.state.job_registry import JobStatus

        if event.job_id is None:
            return
        job_id_str = str(event.job_id)

        # Transition job from SUBMITTED to RUNNING
        registry_job = self._workflow_state.jobs.get_by_job_id(job_id_str)
        if registry_job is not None:
            registry_job.start_time = event.timestamp
            self._workflow_state.jobs.set_status(registry_job, JobStatus.RUNNING)

        threads = event.threads or (registry_job.threads if registry_job else None)
        for i, job in enumerate(running_jobs):
            if job.job_id == job_id_str:
                running_jobs[i] = JobInfo(
                    rule=job.rule,
                    job_id=job.job_id,
                    start_time=event.timestamp,
                    end_time=job.end_time,
                    output_file=job.output_file,
                    wildcards=event.wildcards_dict or job.wildcards,
                    input_size=job.input_size,
                    threads=threads or job.threads,
                )
                break

    def _handle_job_finished_event(
        self,
        event: SnakeseeEvent,
        completions: list[JobInfo],
    ) -> None:
        """Handle JOB_FINISHED event - transition to COMPLETED."""
        from snakesee.state.job_registry import JobStatus

        if event.job_id is None or event.duration is None:
            return
        job_id_str = str(event.job_id)
        registry_job = self._workflow_state.jobs.get_by_job_id(job_id_str)

        # Transition job to COMPLETED
        if registry_job is not None:
            registry_job.end_time = event.timestamp
            self._workflow_state.jobs.set_status(registry_job, JobStatus.COMPLETED)

        threads = event.threads or (registry_job.threads if registry_job else None)
        for i, job in enumerate(completions):
            if job.job_id == job_id_str:
                completions[i] = JobInfo(
                    rule=job.rule,
                    job_id=job.job_id,
                    start_time=event.timestamp - event.duration
                    if event.duration
                    else job.start_time,
                    end_time=event.timestamp,
                    output_file=job.output_file,
                    wildcards=job.wildcards,
                    input_size=job.input_size,
                    threads=threads or job.threads,
                )
                break

    def _record_job_stats_from_event(self, event: SnakeseeEvent) -> None:
        """Record job stats to RuleRegistry from a JOB_FINISHED event.

        Uses JobRegistry to track which jobs have had stats recorded
        to avoid duplicates across poll cycles.
        """
        if event.job_id is None or event.duration is None or event.rule_name is None:
            return

        # Check if we've already recorded stats for this job
        job_key = str(event.job_id)
        job = self._workflow_state.jobs.get(job_key)
        if job is not None and job.stats_recorded:
            return

        # Get threads from event or JobRegistry
        threads = event.threads or (job.threads if job else None)

        # Record to RuleRegistry
        self._workflow_state.rules.record_completion(
            rule=event.rule_name,
            duration=event.duration,
            timestamp=event.timestamp,
            threads=threads,
            wildcards=event.wildcards_dict,
        )

        # Mark as recorded
        if job is not None:
            job.stats_recorded = True

    def _handle_job_error_event(
        self,
        event: SnakeseeEvent,
        failed_list: list[JobInfo],
    ) -> int:
        """Handle JOB_ERROR event - track failed job. Returns new failed count."""
        if event.job_id is None:
            return len(failed_list)
        job_id_str = str(event.job_id)
        if not any(j.job_id == job_id_str for j in failed_list):
            failed_list.append(
                JobInfo(
                    rule=event.rule_name or "unknown",
                    job_id=job_id_str,
                    start_time=event.timestamp - event.duration if event.duration else None,
                    end_time=event.timestamp,
                    wildcards=event.wildcards_dict,
                    threads=event.threads,
                )
            )
        return len(failed_list)

    def _compute_pending_jobs_from_scheduled(
        self,
        running_jobs: list[JobInfo],
        completions: list[JobInfo],
        failed_jobs: list[JobInfo] | None = None,
    ) -> list[JobInfo]:
        """Compute pending jobs by subtracting running/completed/failed from all scheduled.

        When the snakesee logger plugin isn't available, we fall back to parsing
        all scheduled jobs from the snakemake log. This method computes which of
        those scheduled jobs are still pending (not yet running, completed, or failed).

        Args:
            running_jobs: Currently running jobs.
            completions: Completed jobs.
            failed_jobs: Failed jobs (to exclude from pending).

        Returns:
            List of pending jobs with their wildcards and threads.
        """
        if not self._all_scheduled_jobs:
            return []
        running_ids = {job.job_id for job in running_jobs if job.job_id}
        completed_ids = {job.job_id for job in completions if job.job_id}
        failed_ids = {job.job_id for job in (failed_jobs or []) if job.job_id}
        excluded_ids = running_ids | completed_ids | failed_ids
        return [
            job for job_id, job in self._all_scheduled_jobs.items() if job_id not in excluded_ids
        ]

    def _apply_events_to_progress(
        self, progress: WorkflowProgress, events: list[SnakeseeEvent]
    ) -> WorkflowProgress:
        """Apply event updates to enhance progress accuracy.

        Events from the logger plugin provide more accurate timing and
        status information than log parsing.

        Args:
            progress: The current workflow progress from parsing.
            events: New events from the logger plugin.

        Returns:
            Updated WorkflowProgress with event data applied.
        """
        # Track updates from events
        new_total = progress.total_jobs
        new_completed = progress.completed_jobs
        new_running_jobs = list(progress.running_jobs)
        new_completions = list(progress.recent_completions)

        # Process events FIRST to update registry state
        for event in events:
            # Route event through centralized JobRegistry (Phase 10)
            self._workflow_state.jobs.apply_event(event)

            if event.event_type == EventType.PROGRESS:
                if event.total_jobs is not None:
                    new_total = event.total_jobs
                    self._workflow_state.total_jobs = event.total_jobs
                if event.completed_jobs is not None:
                    new_completed = event.completed_jobs
            elif event.event_type == EventType.JOB_SUBMITTED:
                self._handle_job_submitted_event(event, new_running_jobs)
            elif event.event_type == EventType.JOB_STARTED:
                self._handle_job_started_event(event, new_running_jobs)
            elif event.event_type == EventType.JOB_FINISHED:
                self._handle_job_finished_event(event, new_completions)
                # Record stats to RuleRegistry for newly completed jobs
                self._record_job_stats_from_event(event)
            # JOB_ERROR is handled by apply_event above (updates registry)

        # AFTER events are processed, merge failed jobs from registry with log-parsed
        # Registry is source of truth (events), log parsing may miss some failures
        registry_failed = self._workflow_state.jobs.failed_job_infos()
        registry_failed_ids = {job.job_id for job in registry_failed if job.job_id}
        # Start with registry failed (authoritative), add any log-parsed that registry missed
        new_failed_list = list(registry_failed)
        for job in progress.failed_jobs_list:
            if job.job_id and job.job_id not in registry_failed_ids:
                new_failed_list.append(job)
        new_failed = len(new_failed_list)

        # Filter running jobs to exclude failed jobs (a job can't be both running and failed)
        failed_job_ids = {job.job_id for job in new_failed_list if job.job_id}
        new_running_jobs = [job for job in new_running_jobs if job.job_id not in failed_job_ids]

        # Apply stored threads/wildcards to running jobs that may have lost them
        # (log-parsed jobs may not have threads if the line order varies)
        for i, job in enumerate(new_running_jobs):
            if job.job_id and (job.threads is None or job.wildcards is None):
                registry_job = self._workflow_state.jobs.get_by_job_id(job.job_id)
                stored_threads = registry_job.threads if registry_job else None
                if job.threads is None and stored_threads is not None:
                    new_running_jobs[i] = JobInfo(
                        rule=job.rule,
                        job_id=job.job_id,
                        start_time=job.start_time,
                        end_time=job.end_time,
                        output_file=job.output_file,
                        wildcards=job.wildcards,
                        input_size=job.input_size,
                        threads=stored_threads,
                        log_file=job.log_file,
                    )

        # Get pending jobs from the registry (jobs submitted but not yet started)
        pending_jobs_list = self._workflow_state.jobs.submitted_job_infos()

        # Fallback: if no pending jobs from events, compute from log-based scheduled jobs
        if not pending_jobs_list:
            # Registry is the single source of truth (populated from events or log parsing)
            all_completed = self._workflow_state.jobs.completed_job_infos()
            pending_jobs_list = self._compute_pending_jobs_from_scheduled(
                new_running_jobs, all_completed, new_failed_list
            )

        # Return updated progress
        return WorkflowProgress(
            workflow_dir=progress.workflow_dir,
            status=progress.status,
            total_jobs=new_total,
            completed_jobs=new_completed,
            failed_jobs=new_failed,
            failed_jobs_list=new_failed_list,
            running_jobs=new_running_jobs,
            recent_completions=new_completions,
            pending_jobs_list=pending_jobs_list,
            start_time=progress.start_time,
            log_file=progress.log_file,
        )

    def _update_rule_stats_from_completions(self, progress: WorkflowProgress) -> None:
        """Update rule_stats with newly completed jobs from registry.

        This handles log-parsed completions that don't go through the event path.
        Event-based completions are handled by _record_job_stats_from_event().
        Uses registry (not recent_completions) to ensure all completed jobs get stats recorded.
        """
        if self._estimator is None:
            return

        # Use registry completed jobs (single source of truth) instead of recent_completions
        # to ensure we don't miss any jobs
        for registry_job in self._workflow_state.jobs.completed():
            # Skip if already recorded (deduplication)
            if registry_job.stats_recorded:
                continue

            # Skip if we don't have a valid duration
            duration = registry_job.duration
            if duration is None:
                continue

            # Record stats to RuleRegistry
            self._workflow_state.rules.record_completion(
                rule=registry_job.rule,
                duration=duration,
                timestamp=registry_job.end_time or 0.0,
                threads=registry_job.threads,
                wildcards=dict(registry_job.wildcards) if registry_job.wildcards else None,
                input_size=registry_job.input_size,
            )

            # Mark as recorded for deduplication
            registry_job.stats_recorded = True

    def _handle_easter_egg_key(self, key: str) -> bool | None:
        """Handle easter egg keys. Returns True/False if handled, None to continue."""
        # Handle easter egg display (any key dismisses)
        if self._show_easter_egg:
            self._show_easter_egg = False
            self._force_refresh = True
            return False

        # Easter egg: 'f' followed by 'g'
        if self._easter_egg_pending_f:
            self._easter_egg_pending_f = False
            if key.lower() == "g":
                self._show_easter_egg = True
                self._force_refresh = True
                return False
        if key.lower() == "f":
            self._easter_egg_pending_f = True
            # Don't return - let other handlers process 'f' if needed

        return None  # Not handled, continue processing

    def _handle_key(self, key: str) -> bool:
        """
        Handle a keypress.

        Args:
            key: The key that was pressed.

        Returns:
            True if should quit, False otherwise.
        """
        # Handle help overlay first - any key closes it regardless of mode
        if self._show_help:
            self._show_help = False
            self._force_refresh = True
            return False

        # Handle log viewing mode (nested inside job selection mode)
        if self._log_viewing_mode:
            return self._handle_log_viewing_key(key)

        # Handle table navigation mode
        if self._job_selection_mode:
            # Use large number; actual bounds checked in _make_job_log_panel
            return self._handle_table_navigation_key(key, 1000)

        # Handle filter input mode
        if self._filter_mode:
            return self._handle_filter_key(key)

        # Handle easter egg
        easter_result = self._handle_easter_egg_key(key)
        if easter_result is not None:
            return easter_result

        # Normal mode keybindings
        if key.lower() == "q":
            return True

        # Dispatch to specialized handlers
        if self._handle_toggle_key(key):
            return False
        if self._handle_refresh_rate_key(key):
            return False
        if self._handle_navigation_key(key):
            return False
        if self._handle_sort_key(key):
            return False
        if self._handle_log_navigation_key(key):
            return False

        return False

    def _handle_toggle_key(self, key: str) -> bool:
        """Handle toggle keys (?, p, e, w, a, r, Ctrl+r). Returns True if handled."""
        if key == "?":
            self._show_help = True
            self._force_refresh = True
            return True
        if key.lower() == "p":
            self._paused = not self._paused
            self._force_refresh = True
            return True
        if key.lower() == "e":
            self.use_estimation = not self.use_estimation
            self._init_estimator()
            self._force_refresh = True
            return True
        if key.lower() == "w":
            # Toggle wildcard conditioning
            self._use_wildcard_conditioning = not self._use_wildcard_conditioning
            self._init_estimator()
            self._force_refresh = True
            return True
        if key.lower() == "r":
            self._force_refresh = True
            return True
        if key.lower() == "a":
            # Toggle colorblind-accessible mode
            if self._accessibility_config == DEFAULT_CONFIG:
                self._accessibility_config = ACCESSIBLE_CONFIG
            else:
                self._accessibility_config = DEFAULT_CONFIG
            self._force_refresh = True
            return True
        if key == "\x12":  # Ctrl+r - hard refresh
            self._init_estimator()
            self._force_refresh = True
            return True
        return False

    def _handle_refresh_rate_key(self, key: str) -> bool:
        """Handle refresh rate keys (+/-, </>, 0). Returns True if key was handled."""
        if key == "-":  # Fine decrease (-0.5s)
            self.refresh_rate = max(MIN_REFRESH_RATE, self.refresh_rate - 0.5)
            self._update_cache_ttl()
            self._force_refresh = True
            return True
        if key == "+" or key == "=":  # Fine increase (+0.5s), = for unshifted +
            self.refresh_rate = min(MAX_REFRESH_RATE, self.refresh_rate + 0.5)
            self._update_cache_ttl()
            self._force_refresh = True
            return True
        if key == "<" or key == ",":  # Coarse decrease (-5s), , for unshifted <
            self.refresh_rate = max(MIN_REFRESH_RATE, self.refresh_rate - 5.0)
            self._update_cache_ttl()
            self._force_refresh = True
            return True
        if key == ">" or key == ".":  # Coarse increase (+5s), . for unshifted >
            self.refresh_rate = min(MAX_REFRESH_RATE, self.refresh_rate + 5.0)
            self._update_cache_ttl()
            self._force_refresh = True
            return True
        if key == "0":  # Reset to default
            self.refresh_rate = DEFAULT_REFRESH_RATE
            self._update_cache_ttl()
            self._force_refresh = True
            return True
        if key == "G":  # Set to minimum (fastest)
            self.refresh_rate = MIN_REFRESH_RATE
            self._update_cache_ttl()
            self._force_refresh = True
            return True
        return False

    def _handle_navigation_key(self, key: str) -> bool:
        """Handle navigation keys (Tab, /, n, N, Esc, Enter). Returns True if handled."""
        if key == "\r" or key == "\n":  # Enter - enter table navigation mode
            self._job_selection_mode = True
            self._log_viewing_mode = False
            self._log_source = "running"  # Default to running jobs table
            self._force_refresh = True
            return True
        if key == "\t":  # Tab - cycle layout
            modes = list(LayoutMode)
            current_idx = modes.index(self._layout_mode)
            self._layout_mode = modes[(current_idx + 1) % len(modes)]
            self._force_refresh = True
            return True
        if key == "/":  # Enter filter mode
            self._filter_mode = True
            self._filter_input = ""
            self._force_refresh = True
            return True
        if key == "n" and self._filter_matches:
            self._filter_index = (self._filter_index + 1) % len(self._filter_matches)
            self._force_refresh = True
            return True
        if key == "N" and self._filter_matches:
            self._filter_index = (self._filter_index - 1) % len(self._filter_matches)
            self._force_refresh = True
            return True
        if key == "\x1b":  # Escape - clear filter and return to latest log
            self._filter_text = None
            self._filter_matches = []
            self._filter_index = 0
            self._current_log_index = 0  # Return to latest
            self._force_refresh = True
            return True
        return False

    def _handle_sort_key(self, key: str) -> bool:
        """Handle table sorting keys (s/S, 1-4). Returns True if handled."""
        if key == "s":
            # Cycle forward: None -> running -> completions -> pending -> stats -> None
            cycle = [None, "running", "completions", "pending", "stats"]
            current_idx = cycle.index(self._sort_table) if self._sort_table in cycle else 0
            self._sort_table = cycle[(current_idx + 1) % len(cycle)]
            self._sort_column = 0
            self._sort_ascending = True
            self._force_refresh = True
            return True
        if key == "S":
            # Cycle backward: None -> stats -> pending -> completions -> running -> None
            cycle = [None, "running", "completions", "pending", "stats"]
            current_idx = cycle.index(self._sort_table) if self._sort_table in cycle else 0
            self._sort_table = cycle[(current_idx - 1) % len(cycle)]
            self._sort_column = 0
            self._sort_ascending = True
            self._force_refresh = True
            return True
        if key in "123" and self._sort_table is not None:
            col = int(key) - 1  # Convert to 0-indexed
            # Completions and pending tables only have limited columns
            if self._sort_table == "completions" and col > 2:
                return True
            if self._sort_table == "pending" and col > 1:
                return True
            if col == self._sort_column:
                self._sort_ascending = not self._sort_ascending
            else:
                self._sort_column = col
                self._sort_ascending = True
            self._force_refresh = True
            return True
        if key == "4" and self._sort_table in ("running", "stats"):
            col = 3
            if col == self._sort_column:
                self._sort_ascending = not self._sort_ascending
            else:
                self._sort_column = col
                self._sort_ascending = True
            self._force_refresh = True
            return True
        return False

    def _handle_log_navigation_key(self, key: str) -> bool:
        """Handle log navigation keys ([, ], {, }). Returns True if handled."""
        if key == "[":  # Older log (1 step)
            self._refresh_log_list()
            if self._current_log_index < len(self._available_logs) - 1:
                self._current_log_index += 1
                self._force_refresh = True
            return True
        if key == "]":  # Newer log (1 step)
            if self._current_log_index > 0:
                self._current_log_index -= 1
                self._force_refresh = True
            return True
        if key == "{":  # Older log (5 steps)
            self._refresh_log_list()
            max_idx = len(self._available_logs) - 1
            self._current_log_index = min(max_idx, self._current_log_index + 5)
            self._force_refresh = True
            return True
        if key == "}":  # Newer log (5 steps)
            self._current_log_index = max(0, self._current_log_index - 5)
            self._force_refresh = True
            return True
        return False

    def _handle_filter_key(self, key: str) -> bool:
        """Handle keypress in filter input mode."""
        if key == "\x1b":  # Escape
            self._filter_mode = False
            self._filter_input = ""
            self._force_refresh = True
        elif key == "\r" or key == "\n":  # Enter
            self._filter_mode = False
            self._filter_text = self._filter_input if self._filter_input else None
            self._filter_index = 0
            self._force_refresh = True
        elif key == "\x7f" or key == "\b":  # Backspace
            self._filter_input = self._filter_input[:-1]
            self._force_refresh = True
        elif key.isprintable():
            self._filter_input += key
            self._force_refresh = True
        return False

    def _handle_table_navigation_key(self, key: str, num_jobs: int) -> bool:  # noqa: C901
        """Handle keypress in table navigation mode. Returns True if should quit.

        Table mode: Navigate between jobs in running/completions tables.
        Press Enter to view a job's log, Esc to exit to normal mode.
        """
        # Help key works in table mode
        if key == "?":
            self._show_help = True
            self._force_refresh = True
            return False

        # Sort keys work in table mode
        if self._handle_sort_key(key):
            return False

        # Enter - view log for selected job (enter log viewing mode)
        # Only running and completions tables have logs to view
        if key == "\r" or key == "\n":
            if self._log_source in ("running", "completions"):
                self._log_viewing_mode = True
                self._log_scroll_offset = 0  # Start at end of log
                self._force_refresh = True
            # For pending/stats, do nothing (no logs available)
            return False

        # Escape - exit table navigation mode
        if key == "\x1b":
            self._job_selection_mode = False
            self._log_source = None
            self._selected_job_index = 0
            self._selected_completion_index = 0
            self._selected_pending_index = 0
            self._selected_stats_index = 0
            self._running_scroll_offset = 0
            self._completions_scroll_offset = 0
            self._pending_scroll_offset = 0
            self._stats_scroll_offset = 0
            self._log_scroll_offset = 0
            self._cached_log_path = None
            self._cached_log_lines = []
            self._force_refresh = True
            return False

        # Tab - cycle forward: running -> completions -> pending -> stats -> running
        if key == "\t":
            cycle = ["running", "completions", "pending", "stats"]
            current_idx = cycle.index(self._log_source) if self._log_source in cycle else 0
            self._log_source = cycle[(current_idx + 1) % len(cycle)]
            self._force_refresh = True
            return False

        # Shift-Tab (backtab) - cycle backward through tables
        if key == "\x1b[Z":
            cycle = ["running", "completions", "pending", "stats"]
            current_idx = cycle.index(self._log_source) if self._log_source in cycle else 0
            self._log_source = cycle[(current_idx - 1) % len(cycle)]
            self._force_refresh = True
            return False

        # h/l for table switching (vim-style left/right column)
        if key == "h":  # h - switch to left column table
            if self._log_source == "completions":
                self._log_source = "running"
                self._force_refresh = True
            elif self._log_source == "stats":
                self._log_source = "pending"
                self._force_refresh = True
            return False

        if key == "l":  # l - switch to right column table
            if self._log_source == "running":
                self._log_source = "completions"
                self._force_refresh = True
            elif self._log_source == "pending":
                self._log_source = "stats"
                self._force_refresh = True
            return False

        # Row navigation: k (up) or j (down) - vim-style
        if key == "k":  # k - previous row (up)
            if self._log_source == "completions":
                self._selected_completion_index = max(0, self._selected_completion_index - 1)
            elif self._log_source == "pending":
                self._selected_pending_index = max(0, self._selected_pending_index - 1)
            elif self._log_source == "stats":
                self._selected_stats_index = max(0, self._selected_stats_index - 1)
            else:  # running
                self._selected_job_index = max(0, self._selected_job_index - 1)
            self._force_refresh = True
            return False

        if key == "j":  # j - next row (down)
            if self._log_source == "completions":
                self._selected_completion_index = min(
                    num_jobs - 1, self._selected_completion_index + 1
                )
            elif self._log_source == "pending":
                self._selected_pending_index = min(num_jobs - 1, self._selected_pending_index + 1)
            elif self._log_source == "stats":
                self._selected_stats_index = min(num_jobs - 1, self._selected_stats_index + 1)
            else:  # running
                self._selected_job_index = min(num_jobs - 1, self._selected_job_index + 1)
            self._force_refresh = True
            return False

        # Half-page navigation: Ctrl+d (down) / Ctrl+u (up)
        half_page = 5
        if key == "\x04":  # Ctrl+d - half page down
            if self._log_source == "completions":
                self._selected_completion_index = min(
                    num_jobs - 1, self._selected_completion_index + half_page
                )
            elif self._log_source == "pending":
                self._selected_pending_index = min(
                    num_jobs - 1, self._selected_pending_index + half_page
                )
            elif self._log_source == "stats":
                self._selected_stats_index = min(
                    num_jobs - 1, self._selected_stats_index + half_page
                )
            else:  # running
                self._selected_job_index = min(num_jobs - 1, self._selected_job_index + half_page)
            self._force_refresh = True
            return False

        if key == "\x15":  # Ctrl+u - half page up
            if self._log_source == "completions":
                self._selected_completion_index = max(
                    0, self._selected_completion_index - half_page
                )
            elif self._log_source == "pending":
                self._selected_pending_index = max(0, self._selected_pending_index - half_page)
            elif self._log_source == "stats":
                self._selected_stats_index = max(0, self._selected_stats_index - half_page)
            else:  # running
                self._selected_job_index = max(0, self._selected_job_index - half_page)
            self._force_refresh = True
            return False

        # Full-page navigation: Ctrl+f (forward/down) / Ctrl+b (back/up)
        full_page = 10
        if key == "\x06":  # Ctrl+f - full page down
            if self._log_source == "completions":
                self._selected_completion_index = min(
                    num_jobs - 1, self._selected_completion_index + full_page
                )
            elif self._log_source == "pending":
                self._selected_pending_index = min(
                    num_jobs - 1, self._selected_pending_index + full_page
                )
            elif self._log_source == "stats":
                self._selected_stats_index = min(
                    num_jobs - 1, self._selected_stats_index + full_page
                )
            else:  # running
                self._selected_job_index = min(num_jobs - 1, self._selected_job_index + full_page)
            self._force_refresh = True
            return False

        if key == "\x02":  # Ctrl+b - full page up
            if self._log_source == "completions":
                self._selected_completion_index = max(
                    0, self._selected_completion_index - full_page
                )
            elif self._log_source == "pending":
                self._selected_pending_index = max(0, self._selected_pending_index - full_page)
            elif self._log_source == "stats":
                self._selected_stats_index = max(0, self._selected_stats_index - full_page)
            else:  # running
                self._selected_job_index = max(0, self._selected_job_index - full_page)
            self._force_refresh = True
            return False

        # Jump to first/last row: g / G
        if key == "g":  # g - first row
            if self._log_source == "completions":
                self._selected_completion_index = 0
            elif self._log_source == "pending":
                self._selected_pending_index = 0
            elif self._log_source == "stats":
                self._selected_stats_index = 0
            else:  # running
                self._selected_job_index = 0
            self._force_refresh = True
            return False

        if key == "G":  # G - last row
            if self._log_source == "completions":
                self._selected_completion_index = max(0, num_jobs - 1)
            elif self._log_source == "pending":
                self._selected_pending_index = max(0, num_jobs - 1)
            elif self._log_source == "stats":
                self._selected_stats_index = max(0, num_jobs - 1)
            else:  # running
                self._selected_job_index = max(0, num_jobs - 1)
            self._force_refresh = True
            return False

        return False

    def _handle_log_viewing_key(self, key: str) -> bool:
        """Handle keypress in log viewing mode. Returns True if should quit.

        Log mode: Scroll through the selected job's log file.
        Press Esc to return to table navigation mode.
        """
        # Help key works in log mode
        if key == "?":
            self._show_help = True
            self._force_refresh = True
            return False

        # Escape - exit log viewing mode, return to table navigation
        if key == "\x1b":
            self._log_viewing_mode = False
            self._log_scroll_offset = 0
            self._force_refresh = True
            return False

        # Line-by-line scrolling: j (down/newer) / k (up/older)
        if key == "j":  # j - scroll down (show newer lines)
            self._log_scroll_offset = max(0, self._log_scroll_offset - 1)
            self._force_refresh = True
            return False

        if key == "k":  # k - scroll up (show older lines)
            self._log_scroll_offset += 1
            self._force_refresh = True
            return False

        # Half-page scrolling: Ctrl+d (down) / Ctrl+u (up)
        half_page = max(1, self._log_scroll_page_size // 2)
        if key == "\x04":  # Ctrl+d - half page down (newer)
            self._log_scroll_offset = max(0, self._log_scroll_offset - half_page)
            self._force_refresh = True
            return False

        if key == "\x15":  # Ctrl+u - half page up (older)
            self._log_scroll_offset += half_page
            self._force_refresh = True
            return False

        # Full-page scrolling: Ctrl+f (forward/down) / Ctrl+b (back/up)
        full_page = self._log_scroll_page_size
        if key == "\x06":  # Ctrl+f - full page down (newer)
            self._log_scroll_offset = max(0, self._log_scroll_offset - full_page)
            self._force_refresh = True
            return False

        if key == "\x02":  # Ctrl+b - full page up (older)
            self._log_scroll_offset += full_page
            self._force_refresh = True
            return False

        # Jump to start/end: g (start/oldest) / G (end/newest)
        if key == "g":  # g - go to start of log (oldest, max offset)
            # Set to a large number; will be clamped by display logic
            self._log_scroll_offset = 999999
            self._force_refresh = True
            return False

        if key == "G":  # G - go to end of log (newest, offset 0)
            self._log_scroll_offset = 0
            self._force_refresh = True
            return False

        return False

    def _make_easter_egg_panel(self) -> Panel:
        """Create the Fulcrum Genomics easter egg panel."""
        from rich.align import Align

        # Try to load and display the logo image
        if FG_LOGO_PATH.exists():
            try:
                from PIL import Image  # type: ignore[import-not-found]
                from rich_pixels import Pixels  # type: ignore[import-not-found]

                # Load and resize image to fit terminal
                img = Image.open(FG_LOGO_PATH)

                # Calculate size based on terminal
                # Each character cell is roughly 2x1 aspect ratio (taller than wide)
                # rich-pixels uses half-block characters, so 2 pixels per character height
                term_width = self.console.width or 80
                term_height = self.console.height or 24

                # Target size: use most of the terminal, leave minimal border
                max_char_width = term_width - 4  # Leave room for panel border
                max_char_height = term_height - 5  # Leave room for border + footer

                # Convert to pixel dimensions
                # Width: 1 char = ~1 pixel width in rich-pixels
                # Height: 1 char = 2 pixel rows (half-block characters)
                max_pixel_width = max_char_width
                max_pixel_height = max_char_height * 2

                # Maintain aspect ratio
                img_ratio = img.width / img.height
                target_ratio = max_pixel_width / max_pixel_height

                if target_ratio > img_ratio:
                    # Terminal is wider than image - fit to height
                    new_height = max_pixel_height
                    new_width = int(new_height * img_ratio)
                else:
                    # Terminal is taller than image - fit to width
                    new_width = max_pixel_width
                    new_height = int(new_width / img_ratio)

                img = img.resize((new_width, new_height), Image.Resampling.LANCZOS)
                pixels = Pixels.from_image(img)

                centered = Align.center(pixels, vertical="middle")

                return Panel(
                    centered,
                    border_style=FG_BLUE,
                    subtitle="[dim]Press any key to return[/dim]",
                    subtitle_align="center",
                )
            except ImportError:
                pass  # Fall through to text fallback
            except (OSError, ValueError, TypeError):
                pass  # Fall through to text fallback

        # Fallback: simple text logo
        fallback = Text()
        fallback.append("\n\n")
        fallback.append("  FULCRUM GENOMICS  ", style=f"bold {FG_BLUE}")
        fallback.append("\n\n")
        fallback.append("  Press any key to return...", style="dim")

        return Panel(
            Align.center(fallback, vertical="middle"),
            border_style=FG_BLUE,
            style="on black",
        )

    def _make_help_panel(self) -> Panel:
        """Create the help overlay panel."""
        help_text = Table(show_header=False, box=None, padding=(0, 2))
        help_text.add_column("Key", style="bold cyan")
        help_text.add_column("Action")

        help_text.add_row("", "[bold]General[/bold]")
        help_text.add_row("q", "Quit")
        help_text.add_row("?", "Toggle this help")
        help_text.add_row("p", "Pause/resume auto-refresh")
        help_text.add_row("e", "Toggle time estimation")
        help_text.add_row("w", "Toggle wildcard conditioning")
        help_text.add_row("a", "Toggle colorblind-accessible mode")
        help_text.add_row("r", "Force refresh")
        help_text.add_row("Ctrl+r", "Hard refresh (reload historical data)")
        help_text.add_row("", "")
        help_text.add_row("", "[bold]Refresh Rate[/bold]")
        help_text.add_row("- / +", "Decrease/increase by 0.5s")
        help_text.add_row("< / >", "Decrease/increase by 5s")
        help_text.add_row("0", f"Reset to default ({DEFAULT_REFRESH_RATE}s)")
        help_text.add_row("G", f"Set to minimum ({MIN_REFRESH_RATE}s, fastest)")
        help_text.add_row("", "")
        help_text.add_row("", "[bold]Layout & Filter[/bold]")
        help_text.add_row("Tab", "Cycle layout (full/compact/minimal)")
        help_text.add_row("/", "Filter rules by name")
        help_text.add_row("n / N", "Next/previous filter match")
        help_text.add_row("Esc", "Clear filter, return to latest log")
        help_text.add_row("", "")
        help_text.add_row("", "[bold]Log Navigation[/bold]")
        help_text.add_row("[ / ]", "View older/newer log (1 step)")
        help_text.add_row("{ / }", "View older/newer log (5 steps)")
        help_text.add_row("", "")
        help_text.add_row("", "[bold]Table Sorting[/bold]")
        help_text.add_row("s / S", "Cycle sort table (forward/backward)")
        help_text.add_row("1-4", "Sort by column (press again to reverse)")
        help_text.add_row("", "")
        help_text.add_row("", "[bold]Table Navigation (Enter to start)[/bold]")
        help_text.add_row("j / k", "Move down/up one row")
        help_text.add_row("g / G", "Jump to first/last row")
        help_text.add_row("Ctrl+d/u", "Move down/up half page")
        help_text.add_row("Ctrl+f/b", "Move down/up full page")
        help_text.add_row("Tab / S-Tab", "Cycle all tables")
        help_text.add_row("h / l", "Switch to left/right column table")
        help_text.add_row("Enter", "View job log (running/completions only)")
        help_text.add_row("Esc", "Exit table navigation")
        help_text.add_row("", "")
        help_text.add_row("", "[bold]Log Viewing (Enter on job)[/bold]")
        help_text.add_row("j / k", "Scroll down/up one line")
        help_text.add_row("g / G", "Jump to start/end of log")
        help_text.add_row("Ctrl+d/u", "Scroll down/up half page")
        help_text.add_row("Ctrl+f/b", "Scroll down/up full page")
        help_text.add_row("Esc", "Return to table navigation")

        return Panel(
            help_text,
            title="[bold]Keyboard Shortcuts[/bold]",
            subtitle="Press any key to close",
            border_style="cyan",
        )

    def _max_visible_rows(self, num_panels: int) -> int:
        """Compute max visible table rows based on terminal height and layout.

        Body area (terminal - fixed chrome) is split equally among panels.
        Each panel loses ~3 lines for border + header row.
        Returns at least 3 rows so tables are always usable.
        """
        height = self.console.height or 24
        # FULL: header(3) + progress(6) + summary_footer(3) + footer(3) = 15
        # COMPACT/MINIMAL: header(3) + progress(6) + footer(3) = 12
        fixed_chrome = 15 if self._layout_mode == LayoutMode.FULL else 12
        body_height = height - fixed_chrome
        panel_height = body_height // max(1, num_panels)
        # Panel border (2) + table header row (1) = 3 lines of overhead
        return max(3, panel_height - 3)

    def _update_panel_counts(self, progress: WorkflowProgress) -> None:
        """Update panel counts based on current workflow state."""
        if self._layout_mode == LayoutMode.FULL:
            self._left_panel_count = 3 if progress.incomplete_jobs_list else 2
            self._right_panel_count = 3 if progress.failed_jobs_list else 2

    def _make_header(self, progress: WorkflowProgress) -> Panel:
        """Create the header panel with workflow path and status."""
        status_styles = {
            WorkflowStatus.RUNNING: "bold green",
            WorkflowStatus.COMPLETED: "bold blue",
            WorkflowStatus.FAILED: "bold red",
            WorkflowStatus.INCOMPLETE: "bold yellow",
            WorkflowStatus.UNKNOWN: "bold yellow",
        }
        style = status_styles.get(progress.status, "bold white")

        header_text = Text()
        header_text.append("FULCRUM GENOMICS", style=f"bold {FG_BLUE}")
        header_text.append(" │ ", style="dim")
        header_text.append("Snakemake Monitor", style="bold white")
        header_text.append("  │  ", style="dim")
        header_text.append(str(self.workflow_dir), style="dim")
        header_text.append("  │  Status: ")
        header_text.append(progress.status.value.upper(), style=style)

        if progress.elapsed_seconds is not None:
            header_text.append("  │  Elapsed: ")
            header_text.append(format_duration(progress.elapsed_seconds), style=FG_BLUE)

        if self._paused:
            header_text.append("  │  ")
            header_text.append("PAUSED", style="bold yellow")

        # Monitoring method indicator
        header_text.append("  │  ")
        if self._event_reader is not None:
            header_text.append("⚡ Events", style="bold green")
        else:
            header_text.append("📄 Parsing", style="bold blue")

        return Panel(header_text, style="white on grey23", border_style=FG_BLUE, height=3)

    def _in_flight_segment(self, progress: WorkflowProgress) -> tuple[int, BarStyle]:
        """Return the count and style for the in-flight (yellow) progress segment.

        The yellow segment shows jobs that are mid-flight. Which jobs those are depends on
        whether the workflow is live or stopped:

        - A live workflow reports its currently-executing jobs via ``running_jobs``.
        - A stopped (``INCOMPLETE``) workflow has no live jobs, so we surface the jobs that
          were in progress when it was interrupted (``incomplete_jobs_list``). This preserves
          the post-mortem "which jobs were interrupted" distinction when browsing a dead run.

        Args:
            progress: Current workflow progress.

        Returns:
            A tuple of (number of in-flight jobs, the BarStyle to render them with).
        """
        config = self._accessibility_config
        if progress.status == WorkflowStatus.INCOMPLETE:
            return len(progress.incomplete_jobs_list), config.incomplete
        return len(progress.running_jobs), config.running

    def _make_progress_bar(self, progress: WorkflowProgress, width: int = 40) -> Text:
        """Create a colored progress bar showing succeeded/failed/in-flight/pending portions."""
        total = max(1, progress.total_jobs)
        succeeded = progress.completed_jobs
        failed = progress.failed_jobs
        in_flight, in_flight_style = self._in_flight_segment(progress)
        config = self._accessibility_config

        # Calculate widths for each segment. Clamp each to non-negative bounds within the
        # remaining width so a transient counter skew (counts briefly exceeding total) can
        # never produce a negative segment and under-render the bar.
        succeeded_width = min(width, max(0, int((succeeded / total) * width)))
        failed_width = min(width - succeeded_width, max(0, int((failed / total) * width)))
        in_flight_width = min(
            width - succeeded_width - failed_width, max(0, int((in_flight / total) * width))
        )
        pending_width = max(0, width - succeeded_width - failed_width - in_flight_width)

        # Build the bar with colored segments
        bar = Text()
        bar.append(config.succeeded.char * succeeded_width, style="green")
        bar.append(config.failed.char * failed_width, style="red")
        bar.append(in_flight_style.char * in_flight_width, style="yellow")
        bar.append(config.remaining.char * pending_width, style="dim")

        return bar

    def _make_progress_panel(
        self,
        progress: WorkflowProgress,
        estimate: TimeEstimate | None,
    ) -> Panel:
        """Create the progress bar panel."""
        total = max(1, progress.total_jobs)
        completed = progress.completed_jobs + progress.failed_jobs
        percent = (completed / total) * 100

        # Calculate bar width based on console width
        # Reserve space for: "Progress " (9) + " XX.X% " (7) + "(XXX/XXX jobs)" (~15) + borders (~4)
        console_width = self.console.width or 80
        bar_width = max(20, console_width - 40)

        # Create colored progress bar
        progress_bar = self._make_progress_bar(progress, width=bar_width)

        # Progress text line
        progress_line = Text()
        progress_line.append("Progress ", style=f"bold {FG_BLUE}")
        progress_line.append(progress_bar)
        progress_line.append(f" {percent:5.1f}% ", style="bold")
        progress_line.append(f"({completed}/{total} jobs)", style="dim")

        # ETA text - handle different workflow states
        eta_parts = []
        if progress.status == WorkflowStatus.FAILED:
            eta_parts.append("[bold red]FAILED[/bold red]")
            if progress.failed_jobs > 0:
                eta_parts.append(f"[dim]({progress.failed_jobs} job(s) failed)[/dim]")
        elif progress.status == WorkflowStatus.INCOMPLETE:
            eta_parts.append("[bold yellow]INCOMPLETE[/bold yellow]")
            if progress.incomplete_jobs_list:
                eta_parts.append(
                    f"[dim]({len(progress.incomplete_jobs_list)} job(s) were in progress)[/dim]"
                )
        elif progress.status == WorkflowStatus.COMPLETED:
            eta_parts.append("[bold blue]Complete[/bold blue]")
        elif estimate is not None:
            eta_parts.append(f"ETA: {estimate.format_eta()}")

            if estimate.seconds_remaining < float("inf") and estimate.seconds_remaining > 0:
                completion_time = datetime.now().timestamp() + estimate.seconds_remaining
                completion_str = datetime.fromtimestamp(completion_time).strftime("%H:%M:%S")
                eta_parts.append(f"(completion: {completion_str})")

            # Show estimation method and inferred cores for transparency
            method_info = estimate.method
            if estimate.inferred_cores is not None and estimate.inferred_cores > 1:
                method_info += f" cores≈{estimate.inferred_cores:.0f}"
            eta_parts.append(f"[dim][{method_info}][/dim]")
        elif not self.use_estimation:
            eta_parts.append("[dim]ETA: disabled[/dim]")

        eta_text = Text.from_markup("  ".join(eta_parts)) if eta_parts else Text("")

        # Legend for the progress bar showing non-zero segments
        config = self._accessibility_config
        legend = Text()
        legend_parts: list[tuple[str, str, str]] = []
        if progress.completed_jobs > 0:
            legend_parts.append(
                (
                    config.succeeded.char,
                    "green",
                    f"{progress.completed_jobs} {config.succeeded.label}",
                )
            )
        if progress.failed_jobs > 0:
            legend_parts.append(
                (config.failed.char, "red", f"{progress.failed_jobs} {config.failed.label}")
            )
        in_flight, in_flight_style = self._in_flight_segment(progress)
        if in_flight > 0:
            legend_parts.append(
                (in_flight_style.char, "yellow", f"{in_flight} {in_flight_style.label}")
            )
        pending = progress.pending_jobs
        if pending > 0:
            legend_parts.append(
                (config.remaining.char, "dim", f"{pending} {config.remaining.label}")
            )
        if legend_parts:
            legend.append("  (", style="dim")
            for i, (symbol, style, label) in enumerate(legend_parts):
                if i > 0:
                    legend.append("  ", style="dim")
                legend.append(symbol, style=style)
                legend.append(f"={label}", style="dim")
            legend.append(")", style="dim")

        # Border color based on status (use FG colors for normal states)
        border_colors = {
            WorkflowStatus.RUNNING: FG_BLUE,
            WorkflowStatus.COMPLETED: FG_GREEN,
            WorkflowStatus.FAILED: "red",
            WorkflowStatus.INCOMPLETE: "yellow",
            WorkflowStatus.UNKNOWN: "yellow",
        }
        border_style = border_colors.get(progress.status, FG_BLUE)

        # Combine progress line with legend
        full_progress = Text()
        full_progress.append(progress_line)
        full_progress.append(legend)
        return Panel(
            Group(full_progress, eta_text),
            title="Progress",
            border_style=border_style,
        )

    def _filter_jobs(self, jobs: list[JobInfo]) -> list[JobInfo]:
        """Filter jobs by rule name if filter is active."""
        if not self._filter_text:
            return jobs

        filtered = [j for j in jobs if self._filter_text.lower() in j.rule.lower()]

        # Update filter matches for n/N navigation (preserve insertion order)
        self._filter_matches = list(dict.fromkeys(j.rule for j in filtered))

        return filtered

    def _sort_indicator(self, table_name: str, col: int) -> str:
        """Get sort indicator for a column header."""
        if self._sort_table != table_name or self._sort_column != col:
            return ""
        return " ▲" if self._sort_ascending else " ▼"

    def _get_job_id_column_width(self, jobs: list[JobInfo]) -> int:
        """Calculate column width needed for job IDs.

        Args:
            jobs: List of jobs to check for max job ID.

        Returns:
            Minimum column width needed to display all job IDs (minimum 2).
        """
        if not jobs:
            return 2
        max_id = 0
        for job in jobs:
            if job.job_id:
                try:
                    job_id_int = int(job.job_id)
                    max_id = max(max_id, job_id_int)
                except ValueError:
                    # Non-integer job ID, use string length
                    max_id = max(max_id, 10 ** (len(job.job_id) - 1))
        # Use row index as fallback if no job IDs
        if max_id == 0:
            max_id = len(jobs)
        return max(2, len(str(max_id)))

    def _get_tool_progress(self, job: JobInfo) -> ToolProgress | None:
        """
        Get tool-specific progress for a running job.

        Results are cached for the TTL duration to avoid parsing job logs
        on every refresh cycle.

        Args:
            job: The running job to check.

        Returns:
            ToolProgress if parseable, None otherwise.
        """
        # Use job.log_file (parsed from snakemake log, keyed by job_id)
        if job.log_file is None:
            return None

        # Use job_id as cache key (unique per job run)
        cache_key = job.job_id if job.job_id else str(job.log_file)
        now = time.time()

        log_path = self.workflow_dir / job.log_file
        if not log_path.exists():
            self._tool_progress_cache[cache_key] = (now, 0.0, None)
            return None

        # Get current file mtime for cache invalidation
        try:
            current_mtime = log_path.stat().st_mtime
        except OSError:
            return None

        # Check cache validity - must be within TTL AND file unchanged
        if cache_key in self._tool_progress_cache:
            cached_time, cached_mtime, cached_progress = self._tool_progress_cache[cache_key]
            if now - cached_time < self._tool_progress_cache_ttl and cached_mtime >= current_mtime:
                return cached_progress

        # Parse and cache the result with current mtime
        # Wrap in try/except to protect TUI from plugin errors
        try:
            progress = parse_tool_progress(job.rule, log_path)
        except Exception:
            # Plugin errors shouldn't crash the TUI - log for debugging
            logger.debug(
                "Failed to parse tool progress for %s: %s", job.rule, log_path, exc_info=True
            )
            progress = None
        self._tool_progress_cache[cache_key] = (now, current_mtime, progress)
        return progress

    def _cleanup_tool_progress_cache(self) -> None:
        """Remove expired entries from tool progress cache.

        Should be called periodically to prevent unbounded memory growth
        in long-running workflows.
        """
        now = time.time()
        # Remove entries that have been stale for 10x the TTL
        max_age = self._tool_progress_cache_ttl * 10
        expired = [
            key
            for key, (cached_time, _, _) in self._tool_progress_cache.items()
            if now - cached_time > max_age
        ]
        for key in expired:
            del self._tool_progress_cache[key]

    def _update_cache_ttl(self) -> None:
        """Update tool progress cache TTL based on current refresh rate.

        Called when refresh_rate changes to keep cache behavior in sync.
        """
        self._tool_progress_cache_ttl = min(
            ADAPTIVE_CACHE_TTL_MULTIPLIER * self.refresh_rate, MAX_CACHE_TTL
        )

    def _build_running_job_data(
        self, jobs: list[JobInfo]
    ) -> list[tuple[JobInfo, float | None, float | None, float | None, ToolProgress | None]]:
        """Build sortable data for running jobs."""
        job_data: list[
            tuple[JobInfo, float | None, float | None, float | None, ToolProgress | None]
        ] = []
        for job in jobs:
            elapsed = job.elapsed
            remaining: float | None = None
            tool_progress: ToolProgress | None = None

            if self._estimator is not None:
                # Use wildcard+thread-aware ETA when available
                expected, variance = self._estimator.get_estimate_for_job(
                    rule=job.rule,
                    wildcards=job.wildcards,
                    threads=job.threads,
                )
                if elapsed is None:
                    # No start time yet - use expected duration as remaining estimate
                    remaining = expected
                elif elapsed <= expected:
                    remaining = expected - elapsed
                else:
                    # Job running longer than expected - use variance to estimate
                    std_dev = math.sqrt(variance) if variance > 0 else expected * 0.5
                    if elapsed <= expected + 2 * std_dev:
                        # Within reasonable variance - assume nearly done
                        remaining = 0.0
                    else:
                        # Far outside expected range - estimate based on elapsed time
                        # Assume job is ~60% done (heuristic for long-running jobs)
                        # This gives a rough estimate rather than "unknown"
                        remaining = elapsed * 0.67  # ~40% more time expected

            # Try to get tool-specific progress
            tool_progress = self._get_tool_progress(job)

            # If we have tool progress with percentage, use it to improve ETA
            if tool_progress is not None and tool_progress.percent_complete is not None:
                if elapsed is not None and tool_progress.percent_complete > 0:
                    # Estimate remaining time based on progress
                    pct = tool_progress.percent_complete / 100.0
                    tool_remaining = elapsed * (1 - pct) / pct if pct > 0 else None
                    # Prefer tool-based estimate if available
                    if tool_remaining is not None:
                        remaining = tool_remaining

            job_data.append((job, elapsed, remaining, job.start_time, tool_progress))
        return job_data

    def _sort_running_job_data(
        self,
        job_data: list[
            tuple[JobInfo, float | None, float | None, float | None, ToolProgress | None]
        ],
    ) -> list[tuple[JobInfo, float | None, float | None, float | None, ToolProgress | None]]:
        """Sort running job data based on current sort settings."""
        if not job_data:
            return job_data
        sort_keys = {
            0: lambda x: x[0].rule.lower(),
            1: lambda x: x[3] or 0,
            2: lambda x: x[1] or 0,
            3: lambda x: x[2] if x[2] is not None else float("inf"),
        }
        key_fn = sort_keys.get(self._sort_column, sort_keys[0])
        return sorted(job_data, key=key_fn, reverse=not self._sort_ascending)

    def _make_running_table(self, progress: WorkflowProgress) -> Panel:  # noqa: C901
        """Create the currently running jobs table."""
        is_sorting = self._sort_table == "running"
        header_style = "bold magenta on dark_blue" if is_sorting else "bold magenta"

        jobs = self._filter_jobs(progress.running_jobs)
        id_col_width = self._get_job_id_column_width(jobs)

        table = Table(expand=True, show_header=True, header_style=header_style)
        table.add_column("#", justify="right", style="dim", width=id_col_width)
        table.add_column(f"Rule{self._sort_indicator('running', 0)}", style="cyan", no_wrap=True)
        table.add_column("Thr", justify="right", style="dim")
        table.add_column(f"Started{self._sort_indicator('running', 1)}", justify="right")
        table.add_column(f"Elapsed{self._sort_indicator('running', 2)}", justify="right")
        table.add_column("Progress", justify="right")
        table.add_column(f"Est. Remaining{self._sort_indicator('running', 3)}", justify="right")
        job_data = self._build_running_job_data(jobs)

        if is_sorting:
            job_data = self._sort_running_job_data(job_data)

        # Virtual scrolling for running jobs table
        num_panels = 1 if self._layout_mode == LayoutMode.COMPACT else self._left_panel_count
        max_visible = self._max_visible_rows(num_panels)
        is_selecting_running = self._job_selection_mode and self._log_source == "running"

        if is_selecting_running and job_data:
            # Clamp selected index to valid bounds
            self._selected_job_index = max(0, min(self._selected_job_index, len(job_data) - 1))

            # Adjust scroll offset to keep selection visible
            if self._selected_job_index < self._running_scroll_offset:
                self._running_scroll_offset = self._selected_job_index
            elif self._selected_job_index >= self._running_scroll_offset + max_visible:
                self._running_scroll_offset = self._selected_job_index - max_visible + 1

            # Clamp scroll offset
            max_scroll = max(0, len(job_data) - max_visible)
            self._running_scroll_offset = max(0, min(self._running_scroll_offset, max_scroll))
        else:
            self._running_scroll_offset = 0

        # Get visible slice
        visible_data = job_data[
            self._running_scroll_offset : self._running_scroll_offset + max_visible
        ]

        for visible_idx, (job, elapsed, remaining, _start, tool_progress) in enumerate(
            visible_data
        ):
            actual_idx = self._running_scroll_offset + visible_idx
            elapsed_str = format_duration(elapsed) if elapsed is not None else "?"
            remaining_str = f"~{format_duration(remaining)}" if remaining is not None else "?"
            started_str = "?"
            if job.start_time is not None:
                started_str = datetime.fromtimestamp(job.start_time).strftime("%H:%M:%S")

            # Format tool progress
            progress_str = ""
            if tool_progress is not None:
                if tool_progress.percent_complete is not None:
                    progress_str = f"{tool_progress.percent_str}"
                else:
                    # Show items processed if no percentage
                    progress_str = f"{tool_progress.items_processed:,} {tool_progress.unit}"

            rule_style = "cyan"
            # Highlight selected job in selection mode (only when viewing running jobs)
            if is_selecting_running and actual_idx == self._selected_job_index:
                rule_style = "bold cyan on dark_blue"
            elif self._filter_matches and self._filter_index < len(self._filter_matches):
                if job.rule == self._filter_matches[self._filter_index]:
                    rule_style = "bold cyan on dark_blue"

            threads_str = str(job.threads) if job.threads is not None else "-"
            job_id_str = str(job.job_id) if job.job_id else str(actual_idx + 1)
            table.add_row(
                job_id_str,
                Text(job.rule, style=rule_style),
                threads_str,
                started_str,
                elapsed_str,
                Text(progress_str, style="green") if progress_str else Text("-", style="dim"),
                remaining_str,
            )

        if not jobs:
            msg = f"[dim]No jobs matching '{self._filter_text}'[/dim]" if self._filter_text else ""
            msg = msg or "[dim]No jobs currently running[/dim]"
            table.add_row("", msg, "", "", "", "", "")

        # Build title with scroll indicator
        total_jobs = len(job_data)
        if is_selecting_running and total_jobs > max_visible:
            start = self._running_scroll_offset + 1
            end = min(self._running_scroll_offset + max_visible, total_jobs)
            title = f"Currently Running ({start}-{end} of {total_jobs})"
        else:
            title = f"Currently Running ({len(progress.running_jobs)} jobs)"
        if is_selecting_running:
            title += " [bold cyan]◀ select job[/bold cyan]"
        elif is_sorting:
            title += " [bold cyan]◀ sorting[/bold cyan]"
        if self._filter_text:
            title += f" [dim]filter: {self._filter_text}[/dim]"
        border = "cyan" if is_selecting_running else (f"bold {FG_BLUE}" if is_sorting else FG_BLUE)
        return Panel(table, title=title, border_style=border, padding=0)

    def _get_completions_sorted(
        self,
        progress: WorkflowProgress,
        *,
        limit: int | None = None,
    ) -> tuple[list[JobInfo], set[int]]:
        """Get merged, filtered, and sorted completions + failed jobs.

        When limit is provided and no selection mode is active, uses heapq for
        O(n * log(limit)) top-N selection instead of a full O(n * log(n)) sort.

        Args:
            progress: Current workflow progress.
            limit: If set, return at most this many items using heap selection.
                When None, returns the full sorted list.

        Returns:
            Tuple of (sorted jobs list, set of failed job ids).
        """
        failed_job_ids = {id(job) for job in progress.failed_jobs_list}

        # Determine effective sort key and direction
        is_sorting = self._sort_table == "completions"
        if is_sorting:
            sort_keys: dict[int, Any] = {
                0: lambda j: j.rule.lower(),
                1: lambda j: j.threads or 0,
                2: lambda j: j.duration or 0,
                3: lambda j: j.end_time or 0,
            }
            key_fn = sort_keys.get(self._sort_column, sort_keys[3])
            descending = not self._sort_ascending
        else:
            key_fn = lambda j: j.end_time or 0  # noqa: E731
            descending = True

        # Stream completions + failures through filter into heap selection to
        # avoid materializing the full merged list on the hot path.
        merged: Iterable[JobInfo] = itertools.chain(
            progress.recent_completions, progress.failed_jobs_list
        )
        filter_text = self._filter_text.lower() if self._filter_text else ""
        if filter_text:
            merged = (j for j in merged if filter_text in j.rule.lower())

        # Use heap selection when we only need the top N items
        if limit is not None:
            if descending:
                jobs = heapq.nlargest(limit, merged, key=key_fn)
            else:
                jobs = heapq.nsmallest(limit, merged, key=key_fn)
        else:
            jobs = sorted(merged, key=key_fn, reverse=descending)

        # Update _filter_matches for n/N navigation (preserve insertion order)
        if filter_text:
            self._filter_matches = list(dict.fromkeys(j.rule for j in jobs))

        return jobs, failed_job_ids

    def _make_completions_table(self, progress: WorkflowProgress) -> Panel:
        """Create the recent completions table."""
        is_sorting = self._sort_table == "completions"
        header_style = "bold green on dark_blue" if is_sorting else "bold green"

        # Virtual scrolling for completions table
        max_visible = self._max_visible_rows(self._right_panel_count)
        is_selecting = self._job_selection_mode and self._log_source == "completions"

        # Use heap selection when only displaying top N (not scrolling)
        limit = None if is_selecting else max_visible
        jobs, failed_job_ids = self._get_completions_sorted(progress, limit=limit)

        id_col_width = self._get_job_id_column_width(jobs)

        table = Table(expand=True, show_header=True, header_style=header_style)
        ind = self._sort_indicator
        table.add_column("#", justify="right", style="dim", width=id_col_width)
        table.add_column(f"Rule{ind('completions', 0)}", no_wrap=True)
        table.add_column(f"Thr{ind('completions', 1)}", justify="right")
        table.add_column(f"Duration{ind('completions', 2)}", justify="right")
        table.add_column(f"Completed{ind('completions', 3)}", justify="right")

        if is_selecting and jobs:
            # Clamp selected index to valid bounds
            self._selected_completion_index = max(
                0, min(self._selected_completion_index, len(jobs) - 1)
            )

            # Adjust scroll offset to keep selection visible
            if self._selected_completion_index < self._completions_scroll_offset:
                self._completions_scroll_offset = self._selected_completion_index
            elif self._selected_completion_index >= self._completions_scroll_offset + max_visible:
                self._completions_scroll_offset = self._selected_completion_index - max_visible + 1

            # Clamp scroll offset
            max_scroll = max(0, len(jobs) - max_visible)
            self._completions_scroll_offset = max(
                0, min(self._completions_scroll_offset, max_scroll)
            )
        else:
            self._completions_scroll_offset = 0

        # Get visible slice
        visible_jobs = jobs[
            self._completions_scroll_offset : self._completions_scroll_offset + max_visible
        ]

        for visible_idx, job in enumerate(visible_jobs):
            actual_idx = self._completions_scroll_offset + visible_idx
            duration = job.duration
            duration_str = format_duration(duration) if duration is not None else "?"
            threads_str = str(job.threads) if job.threads is not None else "-"

            completed_str = "?"
            if job.end_time is not None:
                completed_str = datetime.fromtimestamp(job.end_time).strftime("%H:%M:%S")

            # Color based on success/failure status
            is_failed = id(job) in failed_job_ids
            rule_style = "red" if is_failed else "cyan"
            time_style = "red" if is_failed else "green"

            # Highlight selected job in selection mode
            if is_selecting and actual_idx == self._selected_completion_index:
                rule_style = "bold cyan on dark_blue" if not is_failed else "bold red on dark_blue"

            job_id_str = str(job.job_id) if job.job_id else str(actual_idx + 1)
            table.add_row(
                job_id_str,
                Text(job.rule, style=rule_style),
                threads_str,
                duration_str,
                f"[{time_style}]{completed_str}[/{time_style}]",
            )

        if not jobs:
            msg = f"[dim]No jobs matching '{self._filter_text}'[/dim]" if self._filter_text else ""
            msg = msg or "[dim]No completed jobs yet[/dim]"
            table.add_row("", msg, "", "", "")

        # Build title with scroll indicator
        total_jobs = len(jobs)
        if is_selecting and total_jobs > max_visible:
            start = self._completions_scroll_offset + 1
            end = min(self._completions_scroll_offset + max_visible, total_jobs)
            title = f"Recent Completions ({start}-{end} of {total_jobs})"
        else:
            title = "Recent Completions"
        if is_selecting:
            title += " [bold cyan]◀ select job[/bold cyan]"
        elif is_sorting:
            title += " [bold cyan]◀ sorting[/bold cyan]"
        border = "cyan" if is_selecting else (f"bold {FG_BLUE}" if is_sorting else FG_BLUE)
        return Panel(table, title=title, border_style=border, padding=0)

    def _make_summary_footer(self, progress: WorkflowProgress) -> Panel:
        """Create the job status summary as a one-line footer panel."""
        succeeded = progress.completed_jobs
        failed = progress.failed_jobs
        running = len(progress.running_jobs)
        incomplete = len(progress.incomplete_jobs_list)
        pending = progress.pending_jobs

        summary = Text()
        summary.append("Jobs: ", style="dim")
        summary.append(f"{succeeded}", style="green")
        summary.append(" succeeded", style="dim")
        summary.append("  │  ", style="dim")
        summary.append(f"{failed}", style="red" if failed > 0 else "dim")
        summary.append(" failed", style="dim")
        summary.append("  │  ", style="dim")
        summary.append(f"{running}", style="cyan" if running > 0 else "dim")
        summary.append(" running", style="dim")
        # Show incomplete count if there are incomplete jobs
        if incomplete > 0:
            summary.append("  │  ", style="dim")
            summary.append(f"{incomplete}", style="yellow")
            summary.append(" incomplete", style="dim")
        summary.append("  │  ", style="dim")
        summary.append(f"{pending}", style="yellow" if pending > 0 else "dim")
        summary.append(" pending", style="dim")

        border_style = "red" if failed > 0 else FG_BLUE
        return Panel(summary, border_style=border_style, padding=(0, 1))

    def _get_inferred_pending_rules(self, progress: WorkflowProgress) -> dict[str, int] | None:
        """Get inferred pending rules from completions and historical data."""
        if not self._estimator:
            return None

        # Registry is the single source of truth (populated from events or log parsing)
        all_completed = self._workflow_state.jobs.completed_job_infos()

        # If we have expected job counts, we can infer pending even without completions
        if not all_completed and not self._estimator.expected_job_counts:
            return None

        # Count completed jobs by rule (using ALL completed, not just recent)
        completed_by_rule: dict[str, int] = {}
        for job in all_completed:
            completed_by_rule[job.rule] = completed_by_rule.get(job.rule, 0) + 1

        # Count running jobs by rule
        running_by_rule: dict[str, int] = {}
        for job in progress.running_jobs:
            running_by_rule[job.rule] = running_by_rule.get(job.rule, 0) + 1

        # Only augment with historical counts if we don't have expected_job_counts
        if not self._estimator.expected_job_counts and self._estimator.rule_stats:
            for rule, stats in self._estimator.rule_stats.items():
                if rule not in completed_by_rule:
                    completed_by_rule[rule] = stats.count

        return self._estimator._infer_pending_rules(
            completed_by_rule, progress.pending_jobs, self._estimator.current_rules, running_by_rule
        )

    def _read_log_tail(self, log_path: Path, max_lines: int = 500) -> list[str]:
        """
        Read the last N lines of a log file efficiently.

        For large files, seeks near the end instead of reading the entire file.

        Args:
            log_path: Path to the log file.
            max_lines: Maximum number of lines to read.

        Returns:
            List of lines (most recent at end).
        """
        # Average bytes per line estimate for seeking
        BYTES_PER_LINE_ESTIMATE = 120

        try:
            # Check if cache is still valid
            stat = log_path.stat()
            mtime = stat.st_mtime
            file_size = stat.st_size
            if (
                self._cached_log_path == log_path
                and self._cached_log_mtime == mtime
                and self._cached_log_lines
            ):
                return self._cached_log_lines

            # For small files, just read the whole thing
            if file_size < BYTES_PER_LINE_ESTIMATE * max_lines * 2:
                content = log_path.read_text(errors="ignore")
                lines = content.splitlines()
            else:
                # For large files, seek near the end to avoid reading everything
                # Read extra bytes to ensure we get enough lines
                seek_bytes = BYTES_PER_LINE_ESTIMATE * max_lines * 2
                with open(log_path, "rb") as f:
                    # Seek to near the end
                    f.seek(max(0, file_size - seek_bytes))
                    # Read to end
                    content = f.read().decode("utf-8", errors="ignore")
                    lines = content.splitlines()
                    # Skip first line (likely partial from seek)
                    if lines and file_size > seek_bytes:
                        lines = lines[1:]

            # Take last max_lines
            result = lines[-max_lines:] if len(lines) > max_lines else lines

            # Update cache
            self._cached_log_path = log_path
            self._cached_log_mtime = mtime
            self._cached_log_lines = result

            return result
        except OSError:
            return ["[Error reading log file]"]

    def _get_completions_list(self, progress: WorkflowProgress) -> tuple[list[JobInfo], set[int]]:
        """Get merged list of completed and failed jobs with same order as table.

        Applies the same filtering and sorting as _make_completions_table() to ensure
        the selected index matches between the table display and log panel.

        Returns:
            Tuple of (jobs_list, failed_job_ids_set)
        """
        # Always return the full sorted list (used for index-based selection)
        return self._get_completions_sorted(progress)

    def _get_running_jobs_list(self, progress: WorkflowProgress) -> list[JobInfo]:
        """Get running jobs list with same order as table.

        Applies the same filtering and sorting as _make_running_table() to ensure
        the selected index matches between the table display and log panel.

        Returns:
            List of running jobs in display order.
        """
        jobs = self._filter_jobs(progress.running_jobs)

        # Apply custom sorting if running table is being sorted
        is_sorting = self._sort_table == "running"
        if is_sorting and jobs:
            # Build job data tuples for sorting (same as _make_running_table)
            job_data = self._build_running_job_data(jobs)
            job_data = self._sort_running_job_data(job_data)
            # Extract just the jobs from the sorted tuples
            jobs = [jd[0] for jd in job_data]

        return jobs

    def _make_job_log_panel(self, progress: WorkflowProgress) -> Panel:
        """Create the job log panel showing selected job's log content."""
        # Build job lists for both sources - use same ordering as tables
        running_jobs = self._get_running_jobs_list(progress)
        completions, failed_ids = self._get_completions_list(progress)

        # Determine which source to use and get selected job
        selected_job: JobInfo | None = None
        job_status: str = ""  # "", "completed", or "failed"

        if self._log_source == "running":
            if running_jobs:
                # Clamp index to valid bounds (consistent with _make_running_table)
                self._selected_job_index = max(
                    0, min(self._selected_job_index, len(running_jobs) - 1)
                )
                selected_job = running_jobs[self._selected_job_index]
        elif self._log_source == "completions":
            if completions:
                # Clamp index to valid bounds (consistent with _make_completions_table)
                self._selected_completion_index = max(
                    0, min(self._selected_completion_index, len(completions) - 1)
                )
                selected_job = completions[self._selected_completion_index]
                job_status = "failed" if id(selected_job) in failed_ids else "completed"

        # Determine subtitle based on mode
        if self._log_viewing_mode:
            mode_subtitle = "[dim]LOG: j/k scroll | g/G start/end | Ctrl-d/u page | Esc back[/dim]"
        else:
            mode_subtitle = "[dim]TABLE: j/k nav | h/l switch | Enter view log | Esc exit[/dim]"

        # Handle no jobs available
        if selected_job is None:
            source_name = "running jobs" if self._log_source == "running" else "completed jobs"
            return Panel(
                f"[dim]No {source_name}[/dim]",
                title="Job Log",
                subtitle=mode_subtitle,
                border_style=FG_BLUE,
            )

        # Use job.log_file (parsed from snakemake log, keyed by job_id)
        if selected_job.log_file is None:
            status_suffix = f" [{job_status}]" if job_status else ""
            return Panel(
                f"[dim]No log file for {selected_job.rule} (job {selected_job.job_id})[/dim]",
                title=f"Job Log: {selected_job.rule}{status_suffix}",
                subtitle=mode_subtitle,
                border_style=FG_BLUE,
            )
        log_path = self.workflow_dir / selected_job.log_file

        if not log_path.exists():
            status_suffix = f" [{job_status}]" if job_status else ""
            return Panel(
                f"[dim]No log file found for {selected_job.rule}[/dim]",
                title=f"Job Log: {selected_job.rule}{status_suffix}",
                subtitle=mode_subtitle,
                border_style=FG_BLUE,
            )

        # Read log content
        lines = self._read_log_tail(log_path, max_lines=500)

        if not lines:
            status_suffix = f" [{job_status}]" if job_status else ""
            return Panel(
                "[dim]Log file is empty[/dim]",
                title=f"Job Log: {selected_job.rule}{status_suffix}",
                subtitle=mode_subtitle,
                border_style=FG_BLUE,
            )

        # Calculate visible window based on console height
        # Panel takes ~5 lines for border, title, and padding
        panel_height = (self.console.height or 24) // 3 - 5
        visible_lines = max(5, panel_height)

        # Apply scroll offset (0 = show latest, positive = show older)
        total_lines = len(lines)
        end_index = total_lines - self._log_scroll_offset
        start_index = max(0, end_index - visible_lines)
        end_index = max(start_index + 1, end_index)

        # Clamp scroll offset
        max_offset = max(0, total_lines - visible_lines)
        self._log_scroll_offset = min(self._log_scroll_offset, max_offset)

        visible_content = lines[start_index:end_index]

        # Build display
        content = Text()
        for i, line in enumerate(visible_content):
            # Truncate long lines
            display_line = line[:117] + "..." if len(line) > 120 else line
            # Highlight recent lines
            style = "white" if i >= len(visible_content) - 3 else "dim"
            content.append(display_line + "\n", style=style)

        # Build title with scroll indicator and status
        scroll_indicator = ""
        if self._log_scroll_offset > 0:
            scroll_indicator = f" [line {start_index + 1}-{end_index}/{total_lines}]"
        elif total_lines > visible_lines:
            scroll_indicator = f" [latest {visible_lines}/{total_lines}]"

        status_suffix = f" [{job_status}]" if job_status else ""
        title = f"Job Log: {selected_job.rule}{status_suffix}{scroll_indicator}"

        return Panel(
            content,
            title=title,
            subtitle=mode_subtitle,
            border_style="cyan" if self._log_viewing_mode else FG_BLUE,
        )

    def _make_pending_jobs_panel(self, progress: WorkflowProgress) -> Panel:
        """Create the pending jobs panel showing estimated pending jobs by rule."""
        pending_count = progress.pending_jobs
        is_sorting = self._sort_table == "pending"
        is_selecting = self._job_selection_mode and self._log_source == "pending"

        title_suffix = ""
        if is_selecting:
            title_suffix = " [bold cyan]◀ select[/bold cyan]"
        elif is_sorting:
            title_suffix = " [bold cyan]◀ sorting[/bold cyan]"

        border = "cyan" if is_selecting else (f"bold {FG_BLUE}" if is_sorting else FG_BLUE)

        if pending_count <= 0:
            return Panel(
                "[dim]No pending jobs[/dim]",
                title="Pending Jobs" + title_suffix,
                border_style=border,
            )

        pending_rules = self._get_inferred_pending_rules(progress)
        if not pending_rules:
            msg = f"[yellow]{pending_count}[/yellow] [dim]jobs pending[/dim]"
            return Panel(msg, title="Pending Jobs" + title_suffix, border_style=border)

        table = Table(expand=True, show_header=True, header_style=f"bold {FG_BLUE}")
        table.add_column(f"Rule{self._sort_indicator('pending', 0)}", style="yellow", no_wrap=True)
        table.add_column(
            f"Est. Count{self._sort_indicator('pending', 1)}", justify="right", style="dim"
        )

        # Sort based on current sort settings or default to count descending
        rules_list = list(pending_rules.items())
        if is_sorting and self._sort_column == 0:
            rules_list.sort(key=lambda x: x[0].lower(), reverse=not self._sort_ascending)
        elif is_sorting:
            rules_list.sort(key=lambda x: x[1], reverse=not self._sort_ascending)
        else:
            rules_list.sort(key=lambda x: x[1], reverse=True)

        # Virtual scrolling
        max_visible = self._max_visible_rows(self._left_panel_count)
        total_rules = len(rules_list)

        if is_selecting and rules_list:
            # Clamp selected index to valid bounds
            self._selected_pending_index = max(
                0, min(self._selected_pending_index, total_rules - 1)
            )

            # Adjust scroll offset to keep selection visible
            if self._selected_pending_index < self._pending_scroll_offset:
                self._pending_scroll_offset = self._selected_pending_index
            elif self._selected_pending_index >= self._pending_scroll_offset + max_visible:
                self._pending_scroll_offset = self._selected_pending_index - max_visible + 1

            # Clamp scroll offset
            max_scroll = max(0, total_rules - max_visible)
            self._pending_scroll_offset = max(0, min(self._pending_scroll_offset, max_scroll))
        else:
            self._pending_scroll_offset = 0

        # Get visible slice
        visible_rules = rules_list[
            self._pending_scroll_offset : self._pending_scroll_offset + max_visible
        ]

        for visible_idx, (rule, count) in enumerate(visible_rules):
            actual_idx = self._pending_scroll_offset + visible_idx
            rule_style = "yellow"

            # Highlight selected row
            if is_selecting and actual_idx == self._selected_pending_index:
                rule_style = "bold yellow on dark_blue"

            table.add_row(Text(rule, style=rule_style), str(count))

        if total_rules > max_visible and not is_selecting:
            table.add_row(f"[dim]... and {total_rules - max_visible} more rules[/dim]", "")

        # Build title with scroll indicator
        if is_selecting and total_rules > max_visible:
            start = self._pending_scroll_offset + 1
            end = min(self._pending_scroll_offset + max_visible, total_rules)
            title = f"Pending Jobs (~{pending_count}, rules {start}-{end} of {total_rules})"
        else:
            title = f"Pending Jobs (~{pending_count})"
        title += title_suffix

        return Panel(
            table,
            title=title,
            border_style=border,
            padding=0,
        )

    def _make_failed_jobs_panel(self, progress: WorkflowProgress) -> Panel:
        """Create the failed jobs list panel."""
        if not progress.failed_jobs_list:
            return Panel(
                "[dim]No failed jobs[/dim]",
                title="Failed Jobs",
                border_style="dim",
            )

        table = Table(expand=True, show_header=True, header_style="bold red")
        table.add_column("#", justify="right", style="dim", width=3)
        table.add_column("Rule", style="red", no_wrap=True)
        table.add_column("Job ID", justify="right", style="dim")

        for idx, job in enumerate(progress.failed_jobs_list[:8]):  # Limit to 8 rows
            job_id_str = job.job_id if job.job_id else "-"
            table.add_row(str(idx + 1), job.rule, job_id_str)

        more_count = len(progress.failed_jobs_list) - 8
        if more_count > 0:
            table.add_row("", f"[dim]... and {more_count} more[/dim]", "")

        return Panel(
            table,
            title=f"Failed Jobs ({len(progress.failed_jobs_list)})",
            border_style="red",
            padding=0,
        )

    def _make_incomplete_jobs_panel(self, progress: WorkflowProgress) -> Panel:
        """Create the incomplete jobs list panel showing files that were in progress."""
        if not progress.incomplete_jobs_list:
            return Panel(
                "[dim]No incomplete jobs[/dim]",
                title="Incomplete Jobs",
                border_style="dim",
            )

        table = Table(expand=True, show_header=True, header_style="bold yellow")
        table.add_column("Output File", style="yellow", no_wrap=False)

        for job in progress.incomplete_jobs_list[:8]:  # Limit to 8 rows
            if job.output_file:
                # Show relative path if possible, otherwise full path
                try:
                    rel_path = job.output_file.relative_to(progress.workflow_dir)
                    table.add_row(str(rel_path))
                except ValueError:
                    table.add_row(str(job.output_file))
            else:
                table.add_row("[dim]unknown[/dim]")

        more_count = len(progress.incomplete_jobs_list) - 8
        if more_count > 0:
            table.add_row(f"[dim]... and {more_count} more[/dim]")

        return Panel(
            table,
            title=f"Incomplete Jobs ({len(progress.incomplete_jobs_list)})",
            border_style="yellow",
            padding=0,
        )

    def _parse_stats_from_logs(self, cutoff: float) -> dict[str, RuleTimingStats]:
        """Parse rule stats from log files created before the cutoff time."""
        from snakesee.parser import parse_completed_jobs_from_log

        stats_dict: dict[str, RuleTimingStats] = {}
        for log in self._available_logs:
            try:
                if log.stat().st_ctime >= cutoff:
                    continue
                for job in parse_completed_jobs_from_log(log):
                    if job.duration is not None:
                        if job.rule not in stats_dict:
                            stats_dict[job.rule] = RuleTimingStats(rule=job.rule)
                        stats_dict[job.rule].durations.append(job.duration)
            except OSError:
                continue
        return stats_dict

    def _get_filtered_stats(self) -> list[RuleTimingStats]:
        """Get rule stats filtered by cutoff time if viewing historical log."""
        from snakesee.parser import parse_metadata_files

        if self._cutoff_time is None:
            # Latest log: use stats from estimator, filtered by current workflow rules
            if self._estimator and self._estimator.rule_stats:
                current_rules = self._estimator.current_rules
                if current_rules is not None:
                    return [
                        stats
                        for stats in self._estimator.rule_stats.values()
                        if stats.rule in current_rules
                    ]
                return list(self._estimator.rule_stats.values())
            return []

        # Historical log: rebuild stats from metadata, filtering by cutoff time
        metadata_dir = self.workflow_dir / ".snakemake" / "metadata"
        stats_dict: dict[str, RuleTimingStats] = {}
        for job in parse_metadata_files(metadata_dir):
            if job.duration is not None and job.end_time is not None:
                if job.end_time < self._cutoff_time:
                    if job.rule not in stats_dict:
                        stats_dict[job.rule] = RuleTimingStats(rule=job.rule)
                    stats_dict[job.rule].durations.append(job.duration)

        # If no metadata found, parse stats from log files up to the cutoff
        if not stats_dict:
            stats_dict = self._parse_stats_from_logs(self._cutoff_time)

        return list(stats_dict.values())

    def _make_stats_panel(self) -> Panel:
        """Create the rule statistics panel."""
        is_sorting = self._sort_table == "stats"
        is_selecting = self._job_selection_mode and self._log_source == "stats"

        title_suffix = ""
        if is_selecting:
            title_suffix = " [bold cyan]◀ select[/bold cyan]"
        elif is_sorting:
            title_suffix = " [bold cyan]◀ sorting[/bold cyan]"

        border = "cyan" if is_selecting else (f"bold {FG_BLUE}" if is_sorting else FG_BLUE)

        # Check if estimation is disabled
        if not self.use_estimation:
            return Panel(
                "[dim]Estimation disabled[/dim]",
                title="Rule Statistics (Historical)" + title_suffix,
                border_style=border,
            )

        stats_list = self._get_filtered_stats()

        if not stats_list:
            return Panel(
                "[dim]No historical data available[/dim]",
                title="Rule Statistics (Historical)" + title_suffix,
                border_style=border,
            )

        header_style = "bold yellow on dark_blue" if is_sorting else "bold yellow"
        table = Table(expand=True, show_header=True, header_style=header_style)
        table.add_column(f"Rule{self._sort_indicator('stats', 0)}", style="cyan", no_wrap=True)
        table.add_column("Thr", justify="right", style="dim")
        table.add_column(f"Count{self._sort_indicator('stats', 1)}", justify="right")
        table.add_column(f"Avg Time{self._sort_indicator('stats', 2)}", justify="right")
        table.add_column(f"Std Dev{self._sort_indicator('stats', 3)}", justify="right", style="dim")

        # Apply filter if active
        if self._filter_text:
            stats_list = [s for s in stats_list if self._filter_text.lower() in s.rule.lower()]

        # Sort based on current settings or default to count descending
        if is_sorting:
            sort_keys = {
                0: lambda s: s.rule.lower(),
                1: lambda s: s.count,
                2: lambda s: s.mean_duration,
                3: lambda s: s.std_dev,
            }
            key_fn = sort_keys.get(self._sort_column, sort_keys[1])
            stats_list.sort(key=key_fn, reverse=not self._sort_ascending)
        else:
            stats_list.sort(key=lambda s: s.count, reverse=True)

        # Build flattened rows list first (for virtual scrolling)
        # Each row is (rule_display, threads_display, stats)
        all_rows: list[tuple[str, str, RuleTimingStats]] = []
        thread_stats_dict = self._workflow_state.rules.to_thread_stats_dict()
        for stats in stats_list:
            rule = stats.rule
            if rule in thread_stats_dict and thread_stats_dict[rule].stats_by_threads:
                # Has thread-specific data - show each thread count
                rule_thread_stats = thread_stats_dict[rule]
                sorted_threads = sorted(rule_thread_stats.stats_by_threads.keys())
                for i, threads in enumerate(sorted_threads):
                    ts = rule_thread_stats.stats_by_threads[threads]
                    # First row shows rule name, subsequent rows show blank
                    rule_display = rule if i == 0 else ""
                    all_rows.append((rule_display, str(threads), ts))
            else:
                # No thread data - show with "-" for threads
                all_rows.append((rule, "-", stats))

        # Virtual scrolling
        max_visible = self._max_visible_rows(self._right_panel_count)
        total_rows = len(all_rows)

        if is_selecting and all_rows:
            # Clamp selected index to valid bounds
            self._selected_stats_index = max(0, min(self._selected_stats_index, total_rows - 1))

            # Adjust scroll offset to keep selection visible
            if self._selected_stats_index < self._stats_scroll_offset:
                self._stats_scroll_offset = self._selected_stats_index
            elif self._selected_stats_index >= self._stats_scroll_offset + max_visible:
                self._stats_scroll_offset = self._selected_stats_index - max_visible + 1

            # Clamp scroll offset
            max_scroll = max(0, total_rows - max_visible)
            self._stats_scroll_offset = max(0, min(self._stats_scroll_offset, max_scroll))
        else:
            self._stats_scroll_offset = 0

        # Get visible slice
        visible_rows = all_rows[self._stats_scroll_offset : self._stats_scroll_offset + max_visible]

        for visible_idx, (rule_display, threads_display, stats) in enumerate(visible_rows):
            actual_idx = self._stats_scroll_offset + visible_idx
            rule_style = "cyan"

            # Highlight selected row
            if is_selecting and actual_idx == self._selected_stats_index:
                rule_style = "bold cyan on dark_blue"

            table.add_row(
                Text(rule_display, style=rule_style) if rule_display else Text(""),
                threads_display,
                str(stats.count),
                format_duration(stats.mean_duration),
                format_duration(stats.std_dev) if stats.std_dev > 0 else "-",
            )

        # Build title with scroll indicator
        title = "Rule Statistics (Historical)"
        if is_selecting and total_rows > max_visible:
            start = self._stats_scroll_offset + 1
            end = min(self._stats_scroll_offset + max_visible, total_rows)
            title = f"Rule Statistics ({start}-{end} of {total_rows})"
        title += title_suffix

        return Panel(table, title=title, border_style=border, padding=0)

    def _make_footer(self) -> Panel:
        """Create the footer with settings and key bindings."""
        footer = Text()
        now = datetime.now().strftime("%H:%M:%S")

        footer.append(f"Updated: {now}", style="dim")
        footer.append("  │  ", style="dim")
        footer.append("? help", style="bold cyan")
        footer.append("  │  ", style="dim")

        # Show log position (e.g., "Log: 1/10" or "Log: 3/10 [historical]")
        total_logs = len(self._available_logs)
        if total_logs > 0:
            current_pos = self._current_log_index + 1  # 1-indexed for display
            footer.append(f"Log: {current_pos}/{total_logs}", style=FG_BLUE)
            if self._current_log_index > 0:
                footer.append(" [historical]", style="yellow")
            footer.append("  │  ", style="dim")

        footer.append(f"Refresh: {self.refresh_rate}s", style="dim")
        footer.append("  │  ", style="dim")
        footer.append("ETA: ", style="dim")
        eta_style = FG_GREEN if self.use_estimation else "red"
        footer.append("ON" if self.use_estimation else "OFF", style=eta_style)
        footer.append("  │  ", style="dim")
        footer.append("Wildcard: ", style="dim")
        wc_style = FG_GREEN if self._use_wildcard_conditioning else "red"
        footer.append("ON" if self._use_wildcard_conditioning else "OFF", style=wc_style)
        footer.append("  │  ", style="dim")
        footer.append(f"Layout: {self._layout_mode.value}", style="dim")

        if self._filter_mode:
            footer.append("  │  ", style="dim")
            footer.append(f"Filter: /{self._filter_input}_", style="bold yellow")
        elif self._filter_text:
            footer.append("  │  ", style="dim")
            footer.append(f"Filter: {self._filter_text}", style="yellow")
            if self._filter_matches:
                match_info = f" ({self._filter_index + 1}/{len(self._filter_matches)})"
                footer.append(match_info, style="dim")

        if self._job_selection_mode:
            footer.append("  │  ", style="dim")
            footer.append("Job Log", style="bold cyan")
            footer.append(" (Esc exit)", style="dim")

        footer.append("  │  ")
        footer.append("snakesee", style=f"bold {FG_BLUE}")
        footer.append(" by ", style="dim")
        footer.append("Fulcrum Genomics", style=FG_BLUE)

        return Panel(footer, border_style=FG_BLUE, padding=(0, 1))

    def _make_layout(
        self,
        progress: WorkflowProgress,
        estimate: TimeEstimate | None,
    ) -> Layout:
        """Create the complete TUI layout."""
        self._update_panel_counts(progress)
        layout = Layout()

        # Easter egg takes over the whole screen
        if self._show_easter_egg:
            layout.split_column(
                Layout(name="easter_egg"),
                Layout(name="footer", size=3),
            )
            layout["easter_egg"].update(self._make_easter_egg_panel())
            layout["footer"].update(self._make_footer())
            return layout

        if self._show_help:
            # Show help overlay
            layout.split_column(
                Layout(name="header", size=3),
                Layout(name="help"),
                Layout(name="footer", size=3),
            )
            layout["header"].update(self._make_header(progress))
            layout["help"].update(self._make_help_panel())
            layout["footer"].update(self._make_footer())
            return layout

        if self._layout_mode == LayoutMode.MINIMAL:
            # Minimal: just header, progress, footer
            layout.split_column(
                Layout(name="header", size=3),
                Layout(name="progress", size=6),
                Layout(name="footer", size=3),
            )
            layout["header"].update(self._make_header(progress))
            layout["progress"].update(self._make_progress_panel(progress, estimate))
            layout["footer"].update(self._make_footer())

        elif self._layout_mode == LayoutMode.COMPACT:
            # Compact: header, progress, running jobs only
            layout.split_column(
                Layout(name="header", size=3),
                Layout(name="progress", size=6),
                Layout(name="running"),
                Layout(name="footer", size=3),
            )
            layout["header"].update(self._make_header(progress))
            layout["progress"].update(self._make_progress_panel(progress, estimate))
            layout["running"].update(self._make_running_table(progress))
            layout["footer"].update(self._make_footer())

        else:  # FULL
            layout.split_column(
                Layout(name="header", size=3),
                Layout(name="progress", size=6),
                Layout(name="body"),
                Layout(name="summary_footer", size=3),
                Layout(name="footer", size=3),
            )

            layout["body"].split_row(
                Layout(name="left", ratio=1),
                Layout(name="right", ratio=1),
            )

            # Check if we have incomplete jobs to show
            has_failed = bool(progress.failed_jobs_list)
            has_incomplete = bool(progress.incomplete_jobs_list)

            # Left column: running jobs, incomplete jobs (if any), pending jobs
            if has_incomplete:
                layout["left"].split_column(
                    Layout(name="running", ratio=1, minimum_size=3),
                    Layout(name="incomplete", ratio=1, minimum_size=3),
                    Layout(name="pending", ratio=1, minimum_size=3),
                )
            else:
                layout["left"].split_column(
                    Layout(name="running", ratio=1, minimum_size=3),
                    Layout(name="pending", ratio=1, minimum_size=3),
                )

            # Right column: completions, failed jobs (if any), stats
            if has_failed:
                layout["right"].split_column(
                    Layout(name="completions", ratio=1, minimum_size=3),
                    Layout(name="failed", ratio=1, minimum_size=3),
                    Layout(name="stats", ratio=1, minimum_size=3),
                )
                layout["failed"].update(self._make_failed_jobs_panel(progress))
            else:
                layout["right"].split_column(
                    Layout(name="completions", ratio=1, minimum_size=3),
                    Layout(name="stats", ratio=1, minimum_size=3),
                )

            layout["header"].update(self._make_header(progress))
            layout["progress"].update(self._make_progress_panel(progress, estimate))

            # Always show running jobs panel
            layout["running"].update(self._make_running_table(progress))

            # Show incomplete jobs panel if there are incomplete jobs
            if has_incomplete:
                layout["incomplete"].update(self._make_incomplete_jobs_panel(progress))

            # Show log panel only when viewing a job's log (not just navigating tables)
            # Log viewing is only available for running/completions tables
            if self._log_viewing_mode and self._log_source in ("running", "completions"):
                layout["pending"].update(self._make_job_log_panel(progress))
            else:
                layout["pending"].update(self._make_pending_jobs_panel(progress))
            layout["completions"].update(self._make_completions_table(progress))
            layout["stats"].update(self._make_stats_panel())
            layout["summary_footer"].update(self._make_summary_footer(progress))
            layout["footer"].update(self._make_footer())

        return layout

    def _get_cutoff_time(self) -> float | None:
        """Get the cutoff time for filtering (when the next log started)."""
        if self._current_log_index == 0:
            return None  # Latest log, no cutoff
        if self._current_log_index > 0 and len(self._available_logs) > 1:
            # Get the creation time of the next newer log
            next_log_index = self._current_log_index - 1
            if next_log_index >= 0:
                try:
                    return self._available_logs[next_log_index].stat().st_ctime
                except OSError:
                    pass
        return None

    def _poll_state(self) -> tuple[WorkflowProgress, TimeEstimate | None]:
        """Poll the current workflow state and estimate."""
        # Refresh log list if viewing latest
        if self._current_log_index == 0:
            self._refresh_log_list()

        # Get the selected log file and cutoff time for historical view
        log_file = self._get_current_log() if self._current_log_index > 0 else None
        self._cutoff_time = self._get_cutoff_time()

        # Read new events from logger plugin (if available)
        events = self._read_new_events()

        # Use incremental reader only for latest log (index 0)
        reader = self._log_reader if self._current_log_index == 0 else None

        progress = parse_workflow_state(
            self.workflow_dir,
            log_file=log_file,
            cutoff_time=self._cutoff_time,
            log_reader=reader,
        )

        # Infer total_jobs from the Job stats table when the progress line
        # hasn't appeared yet (it only appears after the first completion).
        # This lets the pending panel show correct counts immediately.
        #
        # Only infer for the latest/live run (log index 0). expected_job_counts is
        # parsed from the latest log, so inferring while browsing a historical run
        # would graft the live run's counts onto the wrong run's progress.
        if (
            progress.total_jobs == 0
            and self._estimator is not None
            and self._current_log_index == 0
        ):
            if not self._estimator.expected_job_counts:
                self._init_current_rules_from_log()
            if self._estimator.expected_job_counts:
                from dataclasses import replace

                inferred_total = sum(self._estimator.expected_job_counts.values())
                if inferred_total > 0:
                    progress = replace(progress, total_jobs=inferred_total)

        # Sync log reader's completed jobs to registry when events aren't available
        # This ensures the registry is the single source of truth regardless of
        # whether the snakesee logger plugin is being used
        if not events and self._log_reader:
            for job in self._log_reader.completed_jobs:
                if job.job_id:
                    self._workflow_state.jobs.apply_job_info(job, key=job.job_id)

        # Validate: compare event-based state with parsed state (before applying)
        # This logs discrepancies to help find bugs in either approach
        if events:
            self._validate_state(events, progress)

        # Apply events to enhance progress accuracy
        if events:
            progress = self._apply_events_to_progress(progress, events)
            self._force_refresh = True
        else:
            # Even without new events, merge registry-tracked failed jobs into progress
            # This ensures failed jobs discovered via earlier events are not lost
            # when log re-parsing misses them
            registry_failed = self._workflow_state.jobs.failed_job_infos()
            if registry_failed:
                from dataclasses import replace

                registry_failed_ids = {job.job_id for job in registry_failed if job.job_id}
                merged_failed = list(registry_failed)
                for job in progress.failed_jobs_list:
                    if job.job_id and job.job_id not in registry_failed_ids:
                        merged_failed.append(job)
                progress = replace(
                    progress,
                    failed_jobs=len(merged_failed),
                    failed_jobs_list=merged_failed,
                )

        # Always populate pending_jobs_list from log-based scheduled jobs
        # (even when no new events, we need this for wildcard-conditioned ETA)
        if not progress.pending_jobs_list:
            from dataclasses import replace

            # Registry is the single source of truth (populated from events or log parsing)
            all_completed = self._workflow_state.jobs.completed_job_infos()
            pending_jobs_list = self._compute_pending_jobs_from_scheduled(
                progress.running_jobs, all_completed, progress.failed_jobs_list
            )
            if pending_jobs_list:
                progress = replace(progress, pending_jobs_list=pending_jobs_list)

        # Update rule_stats with newly completed jobs (for Rule Statistics panel)
        self._update_rule_stats_from_completions(progress)

        estimate = None
        if self._estimator is not None:
            estimate = self._estimator.estimate_remaining(progress)

        # Periodically clean up stale cache entries
        self._cleanup_tool_progress_cache()

        return progress, estimate

    def run(self) -> None:
        """
        Run the TUI main loop.

        Continuously refreshes the display until the user presses 'q'
        or the workflow completes.
        """
        # Check if we're in a terminal that supports the TUI
        if not self.console.is_terminal:
            self.console.print(
                "[yellow]Warning:[/yellow] Not running in an interactive terminal. "
                "Use 'snakesee status' for non-interactive output.",
            )
            return

        try:
            import fcntl
            import os
            import select
            import termios
            import tty
        except ImportError:
            # termios/tty not available (Windows without WSL)
            self._run_simple()
            return

        # Initial state poll (before switching to raw mode for cleaner spinner)
        with self.console.status("[bold blue]Preparing display..."):
            progress, estimate = self._poll_state()

        # Save terminal settings and switch to raw mode for single-key input
        # Initialize before try block so finally can safely check
        fd: int | None = None
        old_settings: list[Any] | None = None

        try:
            fd = sys.stdin.fileno()
            old_settings = termios.tcgetattr(fd)
            # Set terminal to raw mode (cbreak would also work)
            tty.setcbreak(fd)

            with Live(
                self._make_layout(progress, estimate),
                console=self.console,
                refresh_per_second=1,
                screen=True,
            ) as live:
                last_update = get_clock().now()

                while self._running:
                    # Check for keyboard input (non-blocking)
                    if sys.stdin in select.select([sys.stdin], [], [], 0.1)[0]:
                        # Read all available input at once using os.read
                        # to avoid buffering issues with escape sequences
                        old_flags = fcntl.fcntl(fd, fcntl.F_GETFL)
                        fcntl.fcntl(fd, fcntl.F_SETFL, old_flags | os.O_NONBLOCK)
                        try:
                            data = os.read(fd, 32)
                            chars = data.decode("utf-8", errors="ignore")
                        except BlockingIOError:
                            chars = ""
                        finally:
                            fcntl.fcntl(fd, fcntl.F_SETFL, old_flags)

                        if not chars:
                            continue

                        # Parse the input - handle escape sequences
                        key = chars[0]
                        if chars.startswith("\x1b[Z"):
                            # Shift-Tab (backtab) - pass through as full sequence
                            key = "\x1b[Z"
                        elif key == "\x1b" and len(chars) >= 3:
                            seq = chars[1:3]
                            # Map known sequences to vim-style keys
                            if seq in ("[A", "OA"):  # Up arrow
                                key = "k"  # Map to k (vim up)
                            elif seq in ("[B", "OB"):  # Down arrow
                                key = "j"  # Map to j (vim down)
                            elif seq.startswith("[") or seq.startswith("O"):
                                # Unknown escape sequence - discard
                                continue
                            # Otherwise keep as Escape

                        if self._handle_key(key):
                            break

                    # Refresh at the specified rate or if forced (unless paused)
                    now = get_clock().now()
                    should_refresh = self._force_refresh or (
                        not self._paused and now - last_update >= self.refresh_rate
                    )

                    if should_refresh:
                        if not self._paused or self._force_refresh:
                            progress, estimate = self._poll_state()
                        live.update(self._make_layout(progress, estimate))
                        last_update = now
                        self._force_refresh = False

                        # Auto-exit when workflow completes (optional)
                        if progress.status in (
                            WorkflowStatus.COMPLETED,
                            WorkflowStatus.FAILED,
                            WorkflowStatus.INCOMPLETE,
                        ):
                            # Show final state for a moment before potentially exiting
                            time.sleep(1)

        except KeyboardInterrupt:
            pass  # Clean exit on Ctrl+C
        finally:
            # Restore terminal settings if they were saved
            if old_settings is not None and fd is not None:
                termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)

    def _run_simple(self) -> None:
        """
        Simple run loop for environments without select().

        Falls back to a non-interactive refresh loop.
        """
        self.console.print("[yellow]Running in simple mode (no keyboard input)[/yellow]")
        self.console.print("Press Ctrl+C to exit\n")

        try:
            while self._running:
                progress, estimate = self._poll_state()

                # Clear and redraw
                self.console.clear()
                self.console.print(self._make_layout(progress, estimate))

                time.sleep(self.refresh_rate)

                # Auto-exit when workflow completes
                if progress.status in (
                    WorkflowStatus.COMPLETED,
                    WorkflowStatus.FAILED,
                    WorkflowStatus.INCOMPLETE,
                ):
                    self.console.print("\n[bold]Workflow finished.[/bold]")
                    break

        except KeyboardInterrupt:
            pass
