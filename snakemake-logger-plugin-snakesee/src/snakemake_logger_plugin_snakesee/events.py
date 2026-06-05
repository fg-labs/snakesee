"""Event types and dataclasses for snakesee logger plugin."""

import json
from dataclasses import asdict, dataclass, fields
from enum import Enum
from typing import Any


class EventType(str, Enum):
    """Event types emitted by the logger plugin."""

    WORKFLOW_STARTED = "workflow_started"
    JOB_SUBMITTED = "job_submitted"
    JOB_QUEUED = "job_queued"
    JOB_STARTED = "job_started"
    JOB_FINISHED = "job_finished"
    JOB_ERROR = "job_error"
    PROGRESS = "progress"


@dataclass(frozen=True, slots=True)
class SnakeseeEvent:
    """A single event from the Snakemake workflow.

    Attributes:
        event_type: Type of the event.
        timestamp: Unix timestamp when the event occurred.
        job_id: Snakemake job ID (for job events).
        rule_name: Name of the rule (for job events).
        wildcards: Wildcard values for the job.
        threads: Number of threads allocated to the job.
        resources: Resource requirements for the job.
        input_files: List of input file paths.
        output_files: List of output file paths.
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
    wildcards: dict[str, str] | None = None
    threads: int | None = None
    resources: dict[str, Any] | None = None
    input_files: list[str] | None = None
    output_files: list[str] | None = None
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

    def to_json(self) -> str:
        """Serialize to compact JSON string.

        Returns:
            JSON string representation of the event.
        """
        data = asdict(self)
        data["event_type"] = self.event_type.value
        # Remove None values to reduce file size
        data = {k: v for k, v in data.items() if v is not None}
        return json.dumps(data, separators=(",", ":"), default=str)

    @classmethod
    def from_json(cls, json_str: str) -> "SnakeseeEvent":
        """Deserialize from JSON string.

        Args:
            json_str: JSON string to parse.

        Returns:
            Parsed SnakeseeEvent instance.

        Raises:
            ValueError: If the JSON is invalid or missing required fields.
        """
        data = json.loads(json_str)
        data["event_type"] = EventType(data["event_type"])
        # Drop unknown keys for forward compatibility, mirroring the reader side:
        # a newer event file must not raise TypeError when parsed by an older
        # build that doesn't model some field yet.
        known = {f.name for f in fields(cls)}
        data = {k: v for k, v in data.items() if k in known}
        return cls(**data)
