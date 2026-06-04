"""Workflow data source: pure-data layer for the snakesee TUI.

This module contains :class:`WorkflowDataSource`, which owns the non-presentation
state of the TUI: polling, estimator initialization, event/log readers, event
handlers, filter/sort helpers, log tail caching, and tool-progress caching.

:class:`snakesee.tui.app.SnakeseeApp` composes a ``WorkflowDataSource``
and delegates data-layer calls to it.
"""

import heapq
import itertools
import logging
import math
import time
from collections.abc import Iterable
from contextlib import AbstractContextManager
from contextlib import nullcontext
from pathlib import Path
from typing import TYPE_CHECKING
from typing import Any
from typing import Literal

if TYPE_CHECKING:
    from snakesee.state.rule_registry import ThreadTimingStats
    from snakesee.types import ProgressCallback

from snakesee.constants import ADAPTIVE_CACHE_TTL_MULTIPLIER
from snakesee.constants import DEFAULT_REFRESH_RATE
from snakesee.constants import MAX_CACHE_TTL
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
from snakesee.parser import IncrementalLogReader
from snakesee.parser import parse_workflow_state
from snakesee.plugins import parse_tool_progress
from snakesee.plugins.base import ToolProgress
from snakesee.state.paths import WorkflowPaths
from snakesee.state.workflow_state import WorkflowState
from snakesee.validation import EventAccumulator
from snakesee.validation import ValidationLogger
from snakesee.validation import compare_states

# Sortable DataTable identifiers. Mirrors the values of `snakesee.tui.app.SortTable`;
# defined here to avoid a circular import between `app.py` and `data_source.py`.
SortTableName = Literal["running", "completions", "pending", "stats"]

logger = logging.getLogger(__name__)


class _NullProgress:
    """No-op stand-in for ``rich.progress.Progress`` used to load the estimator
    silently at runtime, where rendering Rich output would corrupt the live
    Textual display."""

    def add_task(self, *args: Any, **kwargs: Any) -> int:
        return 0

    def update(self, *args: Any, **kwargs: Any) -> None:
        pass


_NULL_PROGRESS = _NullProgress()


def _parse_log_start_time(log_path: Path) -> float | None:
    """Return the workflow-start time from a Snakemake log's first timestamp.

    Scans the log for the first ``[Www Mmm DD HH:MM:SS YYYY]`` timestamp line and
    returns it as a Unix timestamp. Unlike the log file's mtime, this value is
    fixed for the life of the run, so it is a stable reference for deciding
    whether an events file belongs to the current run.

    Args:
        log_path: Path to the Snakemake log file.

    Returns:
        The first log timestamp as a Unix timestamp, or None if the file can't be
        read or contains no recognizable timestamp yet.
    """
    from snakesee.parser.patterns import TIMESTAMP_PATTERN
    from snakesee.parser.utils import _parse_timestamp

    try:
        with open(log_path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                if match := TIMESTAMP_PATTERN.match(line.lstrip()):
                    return _parse_timestamp(match.group(1))
    except OSError:
        return None
    return None


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


class WorkflowDataSource:
    """Pure data layer for snakesee TUI.

    Owns polling, estimator state, event/log readers, event handlers, filter/sort
    helpers, log tail caching, and tool-progress caching. Rendering and input
    handling live in :class:`snakesee.tui.app.SnakeseeApp`.
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
    ) -> None:
        """Initialize the data source.

        Args:
            workflow_dir: Path to workflow directory containing ``.snakemake/``.
            refresh_rate: Refresh interval in seconds (used to size cache TTL).
            use_estimation: Whether to enable time estimation.
            profile_path: Optional path to a timing profile for bootstrapping estimates.
            use_wildcard_conditioning: Whether to enable wildcard-conditioned estimates.
            weighting_strategy: Strategy for weighting historical data ("index" or "time").
            half_life_logs: Half-life in run count for index-based weighting.
            half_life_days: Half-life in days for time-based weighting.
        """
        self.workflow_dir = workflow_dir
        self.refresh_rate = refresh_rate
        self.use_estimation = use_estimation
        self.profile_path = profile_path
        self.weighting_strategy = weighting_strategy
        self.half_life_logs = half_life_logs
        self.half_life_days = half_life_days

        self._use_wildcard_conditioning: bool = use_wildcard_conditioning

        self._estimator: TimeEstimator | None = None

        # Log file navigation
        self._available_logs: list[Path] = []
        self._current_log_index: int = 0  # 0 = most recent
        self._latest_log_path: Path | None = None  # Track latest log to detect new workflows
        self.refresh_log_list()

        # Cutoff time for historical view (updated in poll_state)
        self._cutoff_time: float | None = None

        # Cached log tail data
        self._cached_log_path: Path | None = None
        self._cached_log_lines: list[str] = []
        self._cached_log_mtime: float = 0

        # Tool progress cache (to avoid parsing job logs on every refresh).
        # Cache stores: (cached_time, file_mtime, progress) - invalidates if file changes.
        self._tool_progress_cache: dict[str, tuple[float, float, ToolProgress | None]] = {}
        # Adaptive TTL: scales with refresh rate to avoid cache outliving refresh cycles.
        self._tool_progress_cache_ttl: float = min(
            ADAPTIVE_CACHE_TTL_MULTIPLIER * refresh_rate, MAX_CACHE_TTL
        )

        # Event reader for real-time events from logger plugin
        self._event_reader: EventReader | None = None
        self._events_enabled: bool = True
        self.init_event_reader()

        # All scheduled jobs from log (for pending job estimation without logger plugin)
        self._all_scheduled_jobs: dict[str, JobInfo] = {}

        # Incremental log reader for efficient polling
        self._log_reader: IncrementalLogReader | None = None
        self.init_log_reader()

        # Validation: compare event-based state with parsed state
        self._event_accumulator: EventAccumulator | None = None
        self._validation_logger: ValidationLogger | None = None
        self.init_validation()

        # Centralized workflow state
        self._workflow_state: WorkflowState = WorkflowState.create(
            workflow_dir=workflow_dir,
        )

        self.init_estimator()

    # --------------------------------------------------- public properties
    @property
    def use_wildcard_conditioning(self) -> bool:
        """Whether wildcard-conditioned estimates are enabled."""
        return self._use_wildcard_conditioning

    @use_wildcard_conditioning.setter
    def use_wildcard_conditioning(self, value: bool) -> None:
        self._use_wildcard_conditioning = value

    @property
    def current_log_index(self) -> int:
        """Index into ``available_logs``; 0 = most recent."""
        return self._current_log_index

    @current_log_index.setter
    def current_log_index(self, value: int) -> None:
        self._current_log_index = value

    @property
    def available_log_count(self) -> int:
        """Number of historical log files currently discovered."""
        return len(self._available_logs)

    @property
    def event_reader(self) -> EventReader | None:
        """Event reader for the current workflow run, if any."""
        return self._event_reader

    def build_running_job_data(
        self, jobs: list[JobInfo]
    ) -> list[tuple[JobInfo, float | None, float | None, float | None, ToolProgress | None]]:
        """Build per-job tuples of (job, elapsed, remaining, start_time, tool_progress)."""
        return self._build_running_job_data(jobs)

    def thread_stats_dict(self) -> "dict[str, ThreadTimingStats]":
        """Return per-rule, per-thread timing statistics."""
        return self._workflow_state.rules.to_thread_stats_dict()

    # ------------------------------------------------------------------ logs
    def refresh_log_list(self) -> None:
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

    def get_current_log(self) -> Path | None:
        """Get the currently selected log file."""
        if not self._available_logs:
            return None
        if self._current_log_index < len(self._available_logs):
            return self._available_logs[self._current_log_index]
        return self._available_logs[0] if self._available_logs else None

    # ----------------------------------------------------------- estimator
    def init_estimator(self, *, show_progress: bool = True) -> None:
        """Initialize or reinitialize the time estimator.

        At startup the load can take many seconds and the user needs feedback
        before the App's compose() returns, so a transient Rich progress spinner
        is rendered directly to the terminal. This is a pragmatic exception to the
        data source being otherwise rendering-agnostic.

        At runtime (re-init triggered by a key press while the Textual UI is
        live), rendering Rich output would corrupt the display, so callers pass
        ``show_progress=False`` to load silently.

        Args:
            show_progress: Render a transient Rich progress spinner during load.
                Set False when the Textual UI is already running.
        """
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

        # Skip loading entirely if there's nothing to load
        if not has_metadata and not has_profile and not log_paths:
            return

        # Render a real Rich progress spinner only at startup; load silently when
        # the Textual UI is already on screen (a Rich render would corrupt it).
        progress_cm: AbstractContextManager[Any]
        if show_progress:
            from rich.console import Console
            from rich.progress import BarColumn
            from rich.progress import MofNCompleteColumn
            from rich.progress import Progress
            from rich.progress import SpinnerColumn
            from rich.progress import TextColumn

            progress_cm = Progress(
                SpinnerColumn(),
                TextColumn("[progress.description]{task.description}"),
                BarColumn(),
                MofNCompleteColumn(),
                console=Console(),
                transient=True,
            )
        else:
            progress_cm = nullcontext(_NULL_PROGRESS)

        with progress_cm as progress:
            # Load from profile first if available
            if has_profile:
                task = progress.add_task("Loading profile...", total=1)
                try:
                    from snakesee.profile import load_profile

                    assert self.profile_path is not None
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
        """Parse current rules, job counts, cores, and all scheduled jobs from the latest log.

        Always resets the inferred-run state first so that stale fields from a previous
        run can't survive into the next workflow when a new log appears before snakemake
        has emitted the corresponding job_stats / scheduled_jobs blocks.
        """
        from snakesee.parser import parse_all_jobs_from_log
        from snakesee.parser import parse_cores_from_log
        from snakesee.parser import parse_job_stats_counts_from_log
        from snakesee.parser import parse_job_stats_from_log

        if self._estimator is None:
            return

        # Clear previous-run inference state before re-parsing.
        self._estimator.current_rules = None
        self._estimator.expected_job_counts = None
        self._all_scheduled_jobs = {}

        paths = WorkflowPaths(self.workflow_dir)
        log_path = paths.find_latest_log()
        if log_path is None:
            return

        # Parse rule names for filtering
        current_rules = parse_job_stats_from_log(log_path)
        if current_rules:
            self._estimator.current_rules = current_rules

        # Parse job counts for accurate pending job inference
        job_counts = parse_job_stats_counts_from_log(log_path)
        if job_counts:
            self._estimator.expected_job_counts = job_counts

        # Parse "Provided cores: N" for definitive parallelism info
        cores = parse_cores_from_log(log_path)
        if cores is not None:
            self._estimator.set_provided_cores(cores)

        # Parse all scheduled jobs with wildcards for pending job estimation
        all_jobs = parse_all_jobs_from_log(log_path)
        if all_jobs:
            self._all_scheduled_jobs = {job.job_id: job for job in all_jobs if job.job_id}

    # ------------------------------------------------------- events / readers
    def init_event_reader(self) -> None:
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

        # Get the current log file's start time for validation. Use the first
        # timestamp recorded in the log (the workflow-start time) rather than the
        # file's mtime: mtime is the last-append time and drifts forward as the
        # run writes, so after a minute of activity it would make the current
        # run's own event file look "stale". The first log timestamp is fixed for
        # the life of the run.
        paths = WorkflowPaths(self.workflow_dir)
        log_path = paths.find_latest_log()
        log_start_time = _parse_log_start_time(log_path) if log_path is not None else None

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

    def init_log_reader(self) -> None:
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

    def init_validation(self) -> None:
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

    def validate_state(self, events: list[SnakeseeEvent], parsed: WorkflowProgress) -> None:
        """Compare event-based state with parsed state and log discrepancies.

        Args:
            events: New events to process.
            parsed: Current parsed workflow progress.
        """
        # Initialize validation if not yet done (event file may have appeared)
        if self._event_accumulator is None:
            self.init_validation()

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

    def read_new_events(self) -> list[SnakeseeEvent]:
        """Read new events from the event file if available.

        Returns:
            List of new events, or empty list if no events or event reading disabled.
        """
        if not self._events_enabled or self._event_reader is None:
            # Try to initialize if event file now exists (with validation)
            if self._events_enabled and self._event_reader is None:
                self.init_event_reader()

            if self._event_reader is None:
                return []

        return self._event_reader.read_new_events()

    # ------------------------------------------------------ event handlers
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

    def _handle_job_queued_event(self, event: SnakeseeEvent) -> None:
        """Handle JOB_QUEUED event - mark a remote job as queued (awaiting a node).

        The registry already transitioned the job to QUEUED via ``apply_event``;
        here we just ensure a job record exists with the rule name and remote
        fields so it appears in the queued list. Queued jobs are deliberately not
        added to ``running_jobs`` — that is the whole point of the distinction.
        """
        from snakesee.state.job_registry import Job
        from snakesee.state.job_registry import JobStatus

        if event.job_id is None:
            return
        job_id_str = str(event.job_id)

        existing = self._workflow_state.jobs.get_by_job_id(job_id_str)
        if existing is None:
            new_job = Job(
                key=job_id_str,
                rule=event.rule_name or "unknown",
                status=JobStatus.QUEUED,
                job_id=job_id_str,
                wildcards=dict(event.wildcards) if event.wildcards else {},
                threads=event.threads,
                external_jobid=event.external_jobid,
                executor=event.executor,
                region=event.region,
                log_stream=event.log_stream,
                queued_at=event.queued_at if event.queued_at is not None else event.timestamp,
            )
            self._workflow_state.jobs.add(new_job)

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

        # For a remote job the executor reports the true execution start; prefer it
        # over the event emission time so elapsed/duration/queue_wait exclude queue
        # wait. Local jobs have no started_at and fall back to the event timestamp.
        start = event.started_at if event.started_at is not None else event.timestamp

        # Transition job from SUBMITTED to RUNNING
        registry_job = self._workflow_state.jobs.get_by_job_id(job_id_str)
        if registry_job is not None:
            registry_job.start_time = start
            self._workflow_state.jobs.set_status(registry_job, JobStatus.RUNNING)

        threads = event.threads or (registry_job.threads if registry_job else None)
        for i, job in enumerate(running_jobs):
            if job.job_id == job_id_str:
                running_jobs[i] = JobInfo(
                    rule=job.rule,
                    job_id=job.job_id,
                    start_time=start,
                    end_time=job.end_time,
                    output_file=job.output_file,
                    wildcards=event.wildcards_dict or job.wildcards,
                    input_size=job.input_size,
                    threads=threads or job.threads,
                )
                break
        else:
            # Job wasn't already in the running list (e.g. the event arrived
            # before the next log parse saw it) - append a fresh entry so it
            # shows up as running immediately rather than after the next re-parse.
            rule = event.rule_name or (registry_job.rule if registry_job else "unknown")
            running_jobs.append(
                JobInfo(
                    rule=rule,
                    job_id=job_id_str,
                    start_time=start,
                    wildcards=event.wildcards_dict,
                    threads=threads,
                )
            )

    def _handle_job_finished_event(
        self,
        event: SnakeseeEvent,
        running_jobs: list[JobInfo],
        completions: list[JobInfo],
    ) -> None:
        """Handle JOB_FINISHED event - transition to COMPLETED.

        Mutates ``running_jobs`` to drop the finished job and ``completions`` to
        either patch the existing entry or append a new one. The previous version
        only patched ``completions``, which could leave the job in ``running_jobs``
        until the next log re-parse and miss it from completions entirely if the
        log parser hadn't seen the completion line yet.
        """
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

        # Drop the finished job from the running list immediately so the UI
        # doesn't keep showing it as running until the next log re-parse.
        running_jobs[:] = [job for job in running_jobs if job.job_id != job_id_str]

        for i, job in enumerate(completions):
            if job.job_id == job_id_str:
                completions[i] = JobInfo(
                    rule=job.rule,
                    job_id=job.job_id,
                    start_time=event.timestamp - event.duration,
                    end_time=event.timestamp,
                    output_file=job.output_file,
                    wildcards=job.wildcards,
                    input_size=job.input_size,
                    threads=threads or job.threads,
                )
                return

        # Job wasn't already in completions (e.g. event arrived before the next
        # log parse) - append a fresh entry so it shows up in recent completions.
        rule = event.rule_name or (registry_job.rule if registry_job else "unknown")
        completions.append(
            JobInfo(
                rule=rule,
                job_id=job_id_str,
                start_time=event.timestamp - event.duration,
                end_time=event.timestamp,
                wildcards=event.wildcards_dict,
                threads=threads,
            )
        )

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

    def apply_events_to_progress(
        self, progress: WorkflowProgress, events: list[SnakeseeEvent]
    ) -> WorkflowProgress:
        """Apply event updates to enhance progress accuracy.

        Events from the logger plugin provide more accurate timing and
        status information than log parsing. For remote executors this also
        populates ``queued_jobs_list`` (jobs awaiting a node) and keeps those
        jobs out of ``running_jobs``.

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
            elif event.event_type == EventType.JOB_QUEUED:
                self._handle_job_queued_event(event)
            elif event.event_type == EventType.JOB_STARTED:
                self._handle_job_started_event(event, new_running_jobs)
            elif event.event_type == EventType.JOB_FINISHED:
                self._handle_job_finished_event(event, new_running_jobs, new_completions)
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

        # Remote jobs that are queued (submitted to the executor, awaiting a node)
        # are tracked separately so they don't masquerade as RUNNING. A job the log
        # parser thinks is "running" but the registry knows is QUEUED is filtered
        # out of the running list here.
        queued_jobs_list = self._workflow_state.jobs.queued_job_infos()
        queued_ids = {job.job_id for job in queued_jobs_list if job.job_id}
        if queued_ids:
            new_running_jobs = [j for j in new_running_jobs if j.job_id not in queued_ids]

        # Fill remote fields (external id, links, queue timing) onto running jobs
        # from the registry — log-parsed/started JobInfos don't carry them.
        new_running_jobs = [self._enrich_remote_fields(job) for job in new_running_jobs]

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
            queued_jobs_list=queued_jobs_list,
            start_time=progress.start_time,
            log_file=progress.log_file,
        )

    def _enrich_remote_fields(self, job: JobInfo) -> JobInfo:
        """Return a copy of ``job`` with remote fields filled in from the registry.

        Log-parsed and event-constructed running JobInfos don't carry the external
        id / executor / region / log stream; the registry does. When the registry
        has them for this job and the JobInfo lacks them, merge them in so the
        running view can show the external id and links.
        """
        if job.job_id is None:
            return job
        registry_job = self._workflow_state.jobs.get_by_job_id(job.job_id)
        if registry_job is None or registry_job.external_jobid is None:
            return job
        from dataclasses import replace

        # Per-field merge that prefers values already on the JobInfo, so this never
        # clobbers job-specific data and also backfills any partially-missing fields.
        return replace(
            job,
            external_jobid=job.external_jobid or registry_job.external_jobid,
            executor=job.executor or registry_job.executor,
            region=job.region or registry_job.region,
            log_stream=job.log_stream or registry_job.log_stream,
            queued_at=job.queued_at if job.queued_at is not None else registry_job.queued_at,
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

    # --------------------------------------------------- filter / sort helpers
    def filter_jobs(self, jobs: list[JobInfo], filter_text: str | None) -> list[JobInfo]:
        """Filter jobs by rule name if filter is active.

        Args:
            jobs: List of jobs to filter.
            filter_text: Text to filter by (case-insensitive substring match).

        Returns:
            Filtered list of jobs (all jobs if filter_text is empty).
        """
        if not filter_text:
            return jobs

        return [j for j in jobs if filter_text.lower() in j.rule.lower()]

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

    def get_tool_progress(self, job: JobInfo) -> ToolProgress | None:
        """Get tool-specific progress for a running job.

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

        # Parse and cache the result with current mtime.  Third-party plugins
        # may raise arbitrary exceptions; catch broadly so a bad plugin can
        # never crash the TUI.
        try:
            progress = parse_tool_progress(job.rule, log_path)
        except Exception:  # noqa: BLE001 - intentional broad catch to protect TUI from plugin errors
            logger.debug(
                "Failed to parse tool progress for %s: %s", job.rule, log_path, exc_info=True
            )
            progress = None
        self._tool_progress_cache[cache_key] = (now, current_mtime, progress)
        return progress

    def cleanup_tool_progress_cache(self) -> None:
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

    def update_cache_ttl(self) -> None:
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
            tool_progress = self.get_tool_progress(job)

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
        sort_column: int,
        sort_ascending: bool,
    ) -> list[tuple[JobInfo, float | None, float | None, float | None, ToolProgress | None]]:
        """Sort running job data based on the given sort settings."""
        if not job_data:
            return job_data
        sort_keys = {
            0: lambda x: x[0].rule.lower(),
            1: lambda x: x[3] or 0,
            2: lambda x: x[1] or 0,
            3: lambda x: x[2] if x[2] is not None else float("inf"),
        }
        key_fn = sort_keys.get(sort_column, sort_keys[0])
        return sorted(job_data, key=key_fn, reverse=not sort_ascending)

    def get_completions_sorted(
        self,
        progress: WorkflowProgress,
        *,
        filter_text: str | None,
        sort_table: SortTableName | None,
        sort_column: int,
        sort_ascending: bool,
        limit: int | None = None,
    ) -> tuple[list[JobInfo], set[int], list[str]]:
        """Get merged, filtered, and sorted completions + failed jobs.

        When ``limit`` is provided and no selection mode is active, uses
        :func:`heapq` for ``O(n * log(limit))`` top-N selection instead of a
        full ``O(n * log(n))`` sort.

        Args:
            progress: Current workflow progress.
            filter_text: Optional filter text (case-insensitive substring match on rule name).
            sort_table: Currently sorted table name (only "completions" triggers custom sort).
            sort_column: 0-indexed column to sort by when ``sort_table == "completions"``.
            sort_ascending: Sort direction when sorting.
            limit: If set, return at most this many items using heap selection.
                When None, returns the full sorted list.

        Returns:
            Tuple of (sorted jobs list, set of failed job ids, list of unique rule names
            for filter navigation).
        """
        failed_job_ids = {id(job) for job in progress.failed_jobs_list}

        # Determine effective sort key and direction
        is_sorting = sort_table == "completions"
        if is_sorting:
            sort_keys: dict[int, Any] = {
                0: lambda j: j.rule.lower(),
                1: lambda j: j.threads or 0,
                2: lambda j: j.duration or 0,
                3: lambda j: j.end_time or 0,
            }
            key_fn = sort_keys.get(sort_column, sort_keys[3])
            descending = not sort_ascending
        else:
            key_fn = lambda j: j.end_time or 0  # noqa: E731
            descending = True

        # Stream completions + failures through filter into heap selection to
        # avoid materializing the full merged list on the hot path.
        merged: Iterable[JobInfo] = itertools.chain(
            progress.recent_completions, progress.failed_jobs_list
        )
        filter_lower = filter_text.lower() if filter_text else ""
        if filter_lower:
            merged = (j for j in merged if filter_lower in j.rule.lower())

        # Use heap selection when we only need the top N items
        if limit is not None:
            if descending:
                jobs = heapq.nlargest(limit, merged, key=key_fn)
            else:
                jobs = heapq.nsmallest(limit, merged, key=key_fn)
        else:
            jobs = sorted(merged, key=key_fn, reverse=descending)

        # Compute filter matches for n/N navigation (preserve insertion order)
        filter_matches = list(dict.fromkeys(j.rule for j in jobs)) if filter_lower else []

        return jobs, failed_job_ids, filter_matches

    def get_completions_list(
        self,
        progress: WorkflowProgress,
        *,
        filter_text: str | None,
        sort_table: SortTableName | None,
        sort_column: int,
        sort_ascending: bool,
    ) -> tuple[list[JobInfo], set[int]]:
        """Get merged list of completed and failed jobs with same order as table.

        Applies the same filtering and sorting as ``_make_completions_table()`` to
        ensure the selected index matches between the table display and log panel.

        Returns:
            Tuple of (jobs_list, failed_job_ids_set).
        """
        # Always return the full sorted list (used for index-based selection)
        jobs, failed_job_ids, _ = self.get_completions_sorted(
            progress,
            filter_text=filter_text,
            sort_table=sort_table,
            sort_column=sort_column,
            sort_ascending=sort_ascending,
        )
        return jobs, failed_job_ids

    def get_running_jobs_list(
        self,
        progress: WorkflowProgress,
        *,
        filter_text: str | None,
        sort_table: SortTableName | None,
        sort_column: int,
        sort_ascending: bool,
    ) -> list[JobInfo]:
        """Get running jobs list with same order as table.

        Applies the same filtering and sorting as ``_make_running_table()`` to
        ensure the selected index matches between the table display and log panel.

        Returns:
            List of running jobs in display order.
        """
        jobs = self.filter_jobs(progress.running_jobs, filter_text)

        # Apply custom sorting if running table is being sorted
        if sort_table == "running" and jobs:
            # Build job data tuples for sorting
            job_data = self._build_running_job_data(jobs)
            job_data = self._sort_running_job_data(job_data, sort_column, sort_ascending)
            # Extract just the jobs from the sorted tuples
            jobs = [jd[0] for jd in job_data]

        return jobs

    # ---------------------------------------------------- pending / stats
    def get_inferred_pending_rules(self, progress: WorkflowProgress) -> dict[str, int] | None:
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

    def _parse_stats_from_logs(self, cutoff: float) -> dict[str, RuleTimingStats]:
        """Parse rule stats from log files created before the cutoff time."""
        from snakesee.parser import parse_completed_jobs_from_log

        stats_dict: dict[str, RuleTimingStats] = {}
        for log in self._available_logs:
            try:
                # Use st_mtime for cross-platform consistency (st_ctime is
                # inode-change time on POSIX, creation time on Windows).
                if log.stat().st_mtime >= cutoff:
                    continue
                for job in parse_completed_jobs_from_log(log):
                    if job.duration is not None:
                        if job.rule not in stats_dict:
                            stats_dict[job.rule] = RuleTimingStats(rule=job.rule)
                        stats_dict[job.rule].durations.append(job.duration)
            except OSError:
                continue
        return stats_dict

    def get_filtered_stats(self) -> list[RuleTimingStats]:
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

    # ------------------------------------------------------------- log tail
    def read_log_tail(self, log_path: Path, max_lines: int = 500) -> list[str]:
        """Read the last N lines of a log file efficiently.

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

    # ----------------------------------------------------- cutoff / poll
    def get_cutoff_time(self) -> float | None:
        """Get the cutoff time for filtering (when the next log started)."""
        if self._current_log_index == 0:
            return None  # Latest log, no cutoff
        if self._current_log_index > 0 and len(self._available_logs) > 1:
            # Cutoff is the start of the next newer log. Use st_mtime (cross-platform)
            # rather than st_ctime, which is inode-change time on POSIX.
            next_log_index = self._current_log_index - 1
            if next_log_index >= 0:
                try:
                    return self._available_logs[next_log_index].stat().st_mtime
                except OSError:
                    pass
        return None

    def poll_state(self) -> tuple[WorkflowProgress, TimeEstimate | None]:
        """Poll the current workflow state and estimate.

        For the latest run (``current_log_index == 0``) we merge live events and
        the in-memory ``JobRegistry`` into the parsed log to enrich timing and
        catch jobs the parser missed. For historical runs we deliberately skip
        all of that — events and the registry describe the *current* run and
        would otherwise leak into older log views.

        Returns:
            Tuple of (workflow progress, optional time estimate).
        """
        # Refresh log list if viewing latest
        if self._current_log_index == 0:
            self.refresh_log_list()

        is_latest_view = self._current_log_index == 0

        # Get the selected log file and cutoff time for historical view
        log_file = self.get_current_log() if not is_latest_view else None
        self._cutoff_time = self.get_cutoff_time()

        # Live events / readers only apply to the latest run.
        events = self.read_new_events() if is_latest_view else []
        reader = self._log_reader if is_latest_view else None

        progress = parse_workflow_state(
            self.workflow_dir,
            log_file=log_file,
            cutoff_time=self._cutoff_time,
            log_reader=reader,
        )

        # Sync log reader's completed jobs to registry when events aren't available
        # This ensures the registry is the single source of truth regardless of
        # whether the snakesee logger plugin is being used
        if is_latest_view and not events and self._log_reader:
            for job in self._log_reader.completed_jobs:
                if job.job_id:
                    self._workflow_state.jobs.apply_job_info(job, key=job.job_id)

        # Validate: compare event-based state with parsed state (before applying)
        # This logs discrepancies to help find bugs in either approach
        if is_latest_view and events:
            self.validate_state(events, progress)

        # Apply events to enhance progress accuracy
        if is_latest_view and events:
            progress = self.apply_events_to_progress(progress, events)
        elif is_latest_view:
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
        # (even when no new events, we need this for wildcard-conditioned ETA).
        # Historical views derive pending purely from the parsed progress so
        # the live registry can't bleed into a finished run.
        if is_latest_view and not progress.pending_jobs_list:
            from dataclasses import replace

            # Registry is the single source of truth (populated from events or log parsing)
            all_completed = self._workflow_state.jobs.completed_job_infos()
            pending_jobs_list = self._compute_pending_jobs_from_scheduled(
                progress.running_jobs, all_completed, progress.failed_jobs_list
            )
            if pending_jobs_list:
                progress = replace(progress, pending_jobs_list=pending_jobs_list)

        # Infer total_jobs from the Job stats table when the progress line hasn't
        # appeared yet (it only emerges after the first completion). This lets the
        # pending panel show correct counts immediately on a fresh run.
        #
        # Scoped to the latest/live run (log index 0): expected_job_counts is parsed
        # from the latest log, so inferring it onto a historical run's progress would
        # graft the live run's totals onto the wrong run.
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

        # Update rule_stats with newly completed jobs (for Rule Statistics panel)
        self._update_rule_stats_from_completions(progress)

        estimate = None
        if self._estimator is not None:
            estimate = self._estimator.estimate_remaining(progress)

        # Periodically clean up stale cache entries
        self.cleanup_tool_progress_cache()

        return progress, estimate
