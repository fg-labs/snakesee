"""Event types and reader for Snakemake logger plugin integration.

This module provides types and utilities for reading real-time events
from the snakemake-logger-plugin-snakesee plugin. Events provide more
accurate and timely job status information than log parsing.
"""

import logging
import threading
from dataclasses import dataclass
from dataclasses import fields
from enum import Enum
from pathlib import Path
from typing import Any

import orjson

logger = logging.getLogger(__name__)

# Default event file name (matches plugin default)
EVENT_FILE_NAME = ".snakesee_events.jsonl"


class EventType(str, Enum):
    """Event types from the snakesee logger plugin.

    These mirror the event types defined in the logger plugin.
    """

    WORKFLOW_STARTED = "workflow_started"
    JOB_SUBMITTED = "job_submitted"
    JOB_QUEUED = "job_queued"
    JOB_STARTED = "job_started"
    JOB_FINISHED = "job_finished"
    JOB_ERROR = "job_error"
    PROGRESS = "progress"


@dataclass(frozen=True, slots=True)
class SnakeseeEvent:
    """A single event from the logger plugin.

    This is a frozen dataclass to ensure events are immutable once parsed.

    Attributes:
        event_type: Type of the event.
        timestamp: Unix timestamp when the event occurred.
        job_id: Snakemake job ID (for job events).
        rule_name: Name of the rule (for job events).
        wildcards: Wildcard values for the job.
        threads: Number of threads allocated to the job.
        resources: Resource requirements for the job.
        input_files: Tuple of input file paths.
        output_files: Tuple of output file paths.
        duration: Job duration in seconds (for finished/error events).
        error_message: Error message (for error events).
        completed_jobs: Number of completed jobs (for progress events).
        total_jobs: Total number of jobs (for progress events).
        workflow_id: Unique workflow identifier.
        executor: Remote executor identifier (e.g. "aws-batch"), if applicable.
        external_jobid: External executor job id/ARN, for remote jobs.
        remote_status: Raw backend status string (e.g. "RUNNING"), for remote jobs.
        queued_at: Epoch seconds the job entered the remote queue, if known.
        started_at: Epoch seconds the job began executing on a remote node, if known.
        stopped_at: Epoch seconds the job stopped executing remotely, if known.
        attempt: 1-based attempt number for retried/preempted remote jobs.
        exit_code: Container/process exit code for a finished remote job.
        status_reason: Backend-provided reason string (e.g. failure cause).
        queue: The remote queue the job was routed to, if known.
        log_stream: Backend log stream identifier (e.g. CloudWatch stream).
        region: Cloud region, used to build console deep links.
    """

    event_type: EventType
    timestamp: float
    job_id: int | None = None
    rule_name: str | None = None
    wildcards: tuple[tuple[str, str], ...] | None = None
    threads: int | None = None
    resources: tuple[tuple[str, Any], ...] | None = None
    input_files: tuple[str, ...] | None = None
    output_files: tuple[str, ...] | None = None
    duration: float | None = None
    error_message: str | None = None
    completed_jobs: int | None = None
    total_jobs: int | None = None
    workflow_id: str | None = None
    # Remote-executor enrichment (all optional; absent for local jobs).
    executor: str | None = None
    external_jobid: str | None = None
    remote_status: str | None = None
    queued_at: float | None = None
    started_at: float | None = None
    stopped_at: float | None = None
    attempt: int | None = None
    exit_code: int | None = None
    status_reason: str | None = None
    queue: str | None = None
    log_stream: str | None = None
    region: str | None = None

    @classmethod
    def from_json(cls, json_str: str | bytes) -> "SnakeseeEvent":
        """Parse from JSON line.

        Args:
            json_str: JSON string or bytes to parse.

        Returns:
            Parsed SnakeseeEvent instance.

        Raises:
            ValueError: If the JSON is invalid or has an unknown event type.
            orjson.JSONDecodeError: If the JSON cannot be parsed.
        """
        data = orjson.loads(json_str)
        data["event_type"] = EventType(data["event_type"])

        # Convert dicts to tuples for frozen dataclass compatibility
        if "wildcards" in data and data["wildcards"] is not None:
            data["wildcards"] = tuple(sorted(data["wildcards"].items()))
        if "resources" in data and data["resources"] is not None:
            data["resources"] = tuple(sorted(data["resources"].items()))
        if "input_files" in data and data["input_files"] is not None:
            data["input_files"] = tuple(data["input_files"])
        if "output_files" in data and data["output_files"] is not None:
            data["output_files"] = tuple(data["output_files"])

        # Drop unknown keys for forward compatibility: a newer plugin may emit
        # fields this build of snakesee doesn't model yet, and cls(**data) would
        # otherwise raise on the unexpected keyword. Skipping them lets an older
        # reader keep working against a newer event stream.
        known = {f.name for f in fields(cls)}
        data = {k: v for k, v in data.items() if k in known}

        return cls(**data)

    @property
    def wildcards_dict(self) -> dict[str, str] | None:
        """Get wildcards as a dictionary.

        Returns:
            Wildcards as a dict, or None if not set.
        """
        if self.wildcards is None:
            return None
        return dict(self.wildcards)


class EventReader:
    """Streaming reader for snakesee event files.

    Reads events incrementally from a JSONL file, tracking the current
    position to only return new events on subsequent calls.

    Attributes:
        event_file: Path to the event file.
    """

    def __init__(self, event_file: Path) -> None:
        """Initialize the event reader.

        Args:
            event_file: Path to the event file.
        """
        self.event_file = event_file
        self._offset: int = 0
        self._lock = threading.RLock()

    def read_new_events(self) -> list[SnakeseeEvent]:
        """Read events added since last call.

        Returns:
            List of new events. Empty list if no new events or file doesn't exist.
        """
        if not self.event_file.exists():
            return []

        # Get current offset under lock (minimize lock hold time)
        with self._lock:
            offset = self._offset

        # Perform file I/O without holding the lock to avoid blocking
        events: list[SnakeseeEvent] = []
        new_offset = offset
        try:
            with open(self.event_file, "r", encoding="utf-8") as f:
                f.seek(offset)
                for line in f:
                    line = line.strip()
                    if line:
                        try:
                            events.append(SnakeseeEvent.from_json(line))
                        except (orjson.JSONDecodeError, ValueError, KeyError, TypeError) as e:
                            # Skip malformed lines but log for debugging
                            logger.debug(
                                "Skipping malformed event line: %s... (%s)",
                                line[:50],
                                e,
                            )
                            continue
                new_offset = f.tell()
        except OSError as e:
            # File access error - log and return empty list
            logger.warning("Error reading event file %s: %s", self.event_file, e)
            return events

        # Update offset under lock
        with self._lock:
            self._offset = new_offset

        return events

    def reset(self) -> None:
        """Reset reader to start of file.

        Call this to re-read all events from the beginning.
        """
        with self._lock:
            self._offset = 0

    @property
    def has_events(self) -> bool:
        """Check if the event file exists and has content.

        Returns:
            True if event file exists and is non-empty.
        """
        if not self.event_file.exists():
            return False
        return self.event_file.stat().st_size > 0


def get_event_file_path(workflow_dir: Path) -> Path:
    """Get the path to the event file for a workflow.

    Args:
        workflow_dir: Path to the workflow directory.

    Returns:
        Path to the event file.
    """
    return workflow_dir / EVENT_FILE_NAME
