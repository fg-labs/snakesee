"""Log handler for snakesee logger plugin."""

import atexit
import time
from pathlib import Path
from typing import Any

from snakemake_interface_logger_plugins.base import LogHandlerBase

from snakemake_logger_plugin_snakesee.events import EventType, SnakeseeEvent
from snakemake_logger_plugin_snakesee.remote_adapter import event_from_payload, payload_from_record
from snakemake_logger_plugin_snakesee.settings import LogHandlerSettings
from snakemake_logger_plugin_snakesee.writer import EventWriter

# Import LogEvent enum - handle both old and new interface versions
try:
    from snakemake_interface_logger_plugins.common import LogEvent
except ImportError:
    # Fallback for older versions
    from enum import Enum

    class LogEvent(str, Enum):  # type: ignore[no-redef]
        """Fallback LogEvent enum for compatibility."""

        ERROR = "error"
        WORKFLOW_STARTED = "workflow_started"
        JOB_INFO = "job_info"
        JOB_STARTED = "job_started"
        JOB_FINISHED = "job_finished"
        JOB_ERROR = "job_error"
        SHELLCMD = "shellcmd"
        GROUP_INFO = "group_info"
        GROUP_ERROR = "group_error"
        RESOURCES_INFO = "resources_info"
        DEBUG_DAG = "debug_dag"
        PROGRESS = "progress"


class LogHandler(LogHandlerBase):
    """Snakesee logger plugin that writes events to a JSONL file.

    This plugin captures job lifecycle events from Snakemake and writes
    them to a JSON Lines file that snakesee can read for real-time
    workflow monitoring.
    """

    def __post_init__(self) -> None:
        """Initialize the event writer."""
        # Determine workflow directory from common_settings
        workflow_dir = getattr(self.common_settings, "directory", None)
        if workflow_dir is None:
            workflow_dir = Path.cwd()
        elif not isinstance(workflow_dir, Path):
            workflow_dir = Path(workflow_dir)

        # Get settings with defaults. self.settings is typed as the base
        # LogHandlerSettingsBase (or None); narrow to this plugin's concrete
        # LogHandlerSettings so its event_file/buffer_size fields resolve.
        settings = self.settings
        if not isinstance(settings, LogHandlerSettings):
            settings = LogHandlerSettings()

        event_path = workflow_dir / settings.event_file
        self._writer = EventWriter(event_path, buffer_size=settings.buffer_size)
        self._job_start_times: dict[int, float] = {}
        self._job_rules: dict[int, str] = {}
        self._job_wildcards: dict[int, dict[str, str]] = {}

        # Set baseFilename for compatibility with Snakemake's logger manager
        self.baseFilename = str(event_path)

        # Register cleanup on exit to ensure events are flushed
        # even if Snakemake doesn't call close()
        atexit.register(self._cleanup)

    @property
    def writes_to_stream(self) -> bool:
        """Whether this handler writes to stdout/stderr."""
        return False

    @property
    def writes_to_file(self) -> bool:
        """Whether this handler writes to files."""
        return True

    @property
    def has_filter(self) -> bool:
        """Whether this handler filters log records."""
        return True

    @property
    def has_formatter(self) -> bool:
        """Whether this handler formats log records."""
        return False

    @property
    def needs_rulegraph(self) -> bool:
        """Whether this handler needs the rule graph."""
        return False

    def emit(self, record: Any) -> None:
        """Process a log record and write events.

        Args:
            record: The log record from Snakemake.
        """
        timestamp = time.time()

        # Remote executors attach a structured remote-job-state payload to an
        # ordinary log record instead of using a Snakemake LogEvent — the
        # LogEvent enum is fixed upstream and an executor cannot extend it. Handle
        # that channel first; such records may have no ``event`` attribute at all.
        remote_payload = payload_from_record(record)
        if remote_payload is not None:
            remote_event = event_from_payload(remote_payload, timestamp)
            if remote_event is not None:
                self._writer.write(remote_event)
            return

        event = getattr(record, "event", None)
        if event is None:
            return

        # Map LogEvent to handler methods
        if event == LogEvent.JOB_INFO:
            self._handle_job_info(record, timestamp)
        elif event == LogEvent.JOB_STARTED:
            self._handle_job_started(record, timestamp)
        elif event == LogEvent.JOB_FINISHED:
            self._handle_job_finished(record, timestamp)
        elif event == LogEvent.JOB_ERROR:
            self._handle_job_error(record, timestamp)
        elif event == LogEvent.PROGRESS:
            self._handle_progress(record, timestamp)
        elif event == LogEvent.WORKFLOW_STARTED:
            self._handle_workflow_started(record, timestamp)

    def _extract_wildcards(self, record: Any) -> dict[str, str] | None:
        """Extract wildcards from a record.

        Args:
            record: The log record.

        Returns:
            Dictionary of wildcard names to values, or None.
        """
        if not hasattr(record, "wildcards"):
            return None

        wc = record.wildcards
        if wc is None:
            return None

        # Handle different wildcard representations
        if hasattr(wc, "__dict__"):
            # Wildcards object with attributes
            return {k: str(v) for k, v in vars(wc).items() if not k.startswith("_")}
        elif isinstance(wc, dict):
            return {str(k): str(v) for k, v in wc.items()}

        return None

    def _extract_resources(self, record: Any) -> dict[str, Any] | None:
        """Extract resources from a record.

        Args:
            record: The log record.

        Returns:
            Dictionary of resource names to values, or None.
        """
        if not hasattr(record, "resources"):
            return None

        res = record.resources
        if res is None:
            return None

        def is_serializable(v: Any) -> bool:
            """Check if a value is JSON serializable."""
            return isinstance(v, (str, int, float, bool, type(None)))

        def extract_value(v: Any) -> Any:
            """Try to extract a serializable value."""
            if is_serializable(v):
                return v
            # Try to convert to string if it's a simple type
            try:
                s = str(v)
                # Skip object representations like "<snakemake.io.AttributeGuard...>"
                if s.startswith("<") and "object at" in s:
                    return None
                return s
            except Exception:
                return None

        result: dict[str, Any] = {}

        if hasattr(res, "__dict__"):
            for k, v in vars(res).items():
                if not k.startswith("_"):
                    extracted = extract_value(v)
                    if extracted is not None:
                        result[k] = extracted
        elif isinstance(res, dict):
            for k, v in res.items():
                extracted = extract_value(v)
                if extracted is not None:
                    result[k] = extracted

        return result if result else None

    def _handle_job_info(self, record: Any, timestamp: float) -> None:
        """Handle job submission event.

        Args:
            record: The log record.
            timestamp: Event timestamp.
        """
        jobid = getattr(record, "jobid", None)
        # rule_name attribute, not name (which is the logger name)
        rule_name = getattr(record, "rule_name", None)
        if rule_name is None:
            rule_name = getattr(record, "rule", "unknown")
        wildcards = self._extract_wildcards(record)
        threads = getattr(record, "threads", None)
        resources = self._extract_resources(record)

        # Store for later correlation
        if jobid is not None:
            self._job_rules[jobid] = rule_name
            if wildcards:
                self._job_wildcards[jobid] = wildcards

        # Extract file lists
        input_files = None
        if hasattr(record, "input") and record.input:
            input_files = [str(f) for f in record.input]

        output_files = None
        if hasattr(record, "output") and record.output:
            output_files = [str(f) for f in record.output]

        event = SnakeseeEvent(
            event_type=EventType.JOB_SUBMITTED,
            timestamp=timestamp,
            job_id=jobid,
            rule_name=rule_name,
            wildcards=wildcards,
            threads=threads,
            resources=resources,
            input_files=input_files,
            output_files=output_files,
        )
        self._writer.write(event)

    def _handle_job_started(self, record: Any, timestamp: float) -> None:
        """Handle job execution start.

        Args:
            record: The log record.
            timestamp: Event timestamp.
        """
        # JOB_STARTED uses 'jobs' attribute (list of job IDs)
        jobs = getattr(record, "jobs", None)
        if jobs is None:
            # Fallback to jobid or job_ids
            job_ids = getattr(record, "job_ids", None)
            if job_ids is None:
                jobid = getattr(record, "jobid", None)
                if jobid is not None:
                    jobs = [jobid]
                else:
                    return
            else:
                jobs = job_ids

        # Ensure we have a list
        if isinstance(jobs, int):
            jobs = [jobs]

        for jid in jobs:
            self._job_start_times[jid] = timestamp
            event = SnakeseeEvent(
                event_type=EventType.JOB_STARTED,
                timestamp=timestamp,
                job_id=jid,
                rule_name=self._job_rules.get(jid),
                wildcards=self._job_wildcards.get(jid),
            )
            self._writer.write(event)

    def _handle_job_finished(self, record: Any, timestamp: float) -> None:
        """Handle job completion.

        Args:
            record: The log record.
            timestamp: Event timestamp.
        """
        # JOB_FINISHED uses 'job_id' attribute
        jobid = getattr(record, "job_id", None)
        if jobid is None:
            jobid = getattr(record, "jobid", None)
        if jobid is None:
            return

        start_time = self._job_start_times.pop(jobid, None)
        duration = timestamp - start_time if start_time else None

        event = SnakeseeEvent(
            event_type=EventType.JOB_FINISHED,
            timestamp=timestamp,
            job_id=jobid,
            rule_name=self._job_rules.pop(jobid, None),
            wildcards=self._job_wildcards.pop(jobid, None),
            duration=duration,
        )
        self._writer.write(event)

    def _handle_job_error(self, record: Any, timestamp: float) -> None:
        """Handle job failure.

        Args:
            record: The log record.
            timestamp: Event timestamp.
        """
        jobid = getattr(record, "jobid", None)
        # Only emit job_error events for actual jobs (with job_id)
        # RuleException messages come through without job_id and should be ignored
        # since the actual job failure event follows with the job_id
        if jobid is None:
            return

        # rule_name attribute, not name (which is the logger name)
        rule_name = getattr(record, "rule_name", None)
        if rule_name is None:
            rule_name = getattr(record, "rule", None)

        # Fall back to stored rule name if not in record
        if rule_name is None:
            rule_name = self._job_rules.get(jobid)

        # jobid is guaranteed non-None here (early return at line 318)
        start_time = self._job_start_times.pop(jobid, None)
        duration = timestamp - start_time if start_time else None

        # Extract error message
        error_msg = None
        if hasattr(record, "msg"):
            error_msg = str(record.msg)
        elif hasattr(record, "message"):
            error_msg = str(record.message)

        wildcards = self._job_wildcards.pop(jobid, None)
        self._job_rules.pop(jobid, None)

        event = SnakeseeEvent(
            event_type=EventType.JOB_ERROR,
            timestamp=timestamp,
            job_id=jobid,
            rule_name=rule_name,
            wildcards=wildcards,
            duration=duration,
            error_message=error_msg,
        )
        self._writer.write(event)

    def _handle_progress(self, record: Any, timestamp: float) -> None:
        """Handle workflow progress update.

        Args:
            record: The log record.
            timestamp: Event timestamp.
        """
        done = getattr(record, "done", 0)
        total = getattr(record, "total", 0)

        event = SnakeseeEvent(
            event_type=EventType.PROGRESS,
            timestamp=timestamp,
            completed_jobs=done,
            total_jobs=total,
        )
        self._writer.write(event)

    def _handle_workflow_started(self, record: Any, timestamp: float) -> None:
        """Handle workflow initialization.

        Truncates the event file to clear stale data from previous runs,
        then writes the workflow_started event.

        Args:
            record: The log record.
            timestamp: Event timestamp.
        """
        # Clear stale events from previous workflow runs
        self._writer.truncate()

        workflow_id = getattr(record, "workflow_id", None)

        event = SnakeseeEvent(
            event_type=EventType.WORKFLOW_STARTED,
            timestamp=timestamp,
            workflow_id=workflow_id,
        )
        self._writer.write(event)

    def close(self) -> None:
        """Clean up resources."""
        self._cleanup()

    def _cleanup(self) -> None:
        """Internal cleanup - safe to call multiple times."""
        if hasattr(self, "_writer") and self._writer is not None:
            try:
                self._writer.close()
            except Exception:
                pass  # Ignore errors during cleanup
