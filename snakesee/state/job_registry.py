"""Job registry for centralized job state management.

This module provides a single source of truth for all job state in a workflow,
replacing the scattered job tracking across multiple components.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from dataclasses import field
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from snakesee.events import SnakeseeEvent
    from snakesee.models import JobInfo


class JobStatus(Enum):
    """Status of a job in the workflow."""

    PENDING = "pending"
    SUBMITTED = "submitted"
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    INCOMPLETE = "incomplete"


@dataclass
class Job:
    """Mutable job state container.

    Unlike the frozen JobInfo dataclass, this class allows state updates
    as jobs progress through their lifecycle.

    Attributes:
        key: Unique key for this job (rule + output hash or job_id).
        rule: The Snakemake rule name.
        status: Current job status.
        job_id: Snakemake job ID (may be None initially).
        start_time: Unix timestamp when job started.
        end_time: Unix timestamp when job completed.
        wildcards: Dictionary of wildcard values.
        threads: Number of threads allocated.
        input_size: Total input file size in bytes.
        log_file: Path to job's log file.
        external_jobid: External executor job id/ARN (e.g. AWS Batch), if remote.
        executor: Remote executor identifier (e.g. "aws-batch"), if applicable.
        region: Cloud region, used to build console deep links (if known).
        log_stream: Backend log stream identifier (e.g. CloudWatch stream), if known.
        queued_at: Epoch seconds the job entered the remote queue, if known.
        queue: The remote queue / compute environment the job was routed to, if known.
        attempt: Attempt number for a retried/preempted remote job, if known.
        exit_code: Container/process exit code for a finished remote job, if known.
        status_reason: Backend-provided reason string (e.g. failure cause), if any.
        termination_category: Why the job died (e.g. "spot", "oom"), if classified.
        termination_source: Provenance of the classification (e.g. "aws_instance_state").
        termination_confidence: How sure the producer was ("high" / "low").
        cost_estimate: Estimated USD cost of the job (list/market price, not billed).
    """

    key: str
    rule: str
    status: JobStatus = JobStatus.PENDING
    job_id: str | None = None
    start_time: float | None = None
    end_time: float | None = None
    wildcards: dict[str, str] = field(default_factory=dict)
    threads: int | None = None
    input_size: int | None = None
    log_file: Path | None = None
    external_jobid: str | None = None
    executor: str | None = None
    region: str | None = None
    log_stream: str | None = None
    queued_at: float | None = None
    queue: str | None = None
    attempt: int | None = None
    exit_code: int | None = None
    status_reason: str | None = None
    termination_category: str | None = None
    termination_source: str | None = None
    termination_confidence: str | None = None
    cost_estimate: float | None = None
    stats_recorded: bool = False

    @property
    def queue_wait(self) -> float | None:
        """Seconds spent queued before execution began (remote jobs only).

        For a remote job, ``start_time`` is the true execution start (from the
        executor) and ``queued_at`` is when it entered the queue, so their
        difference is the queue wait. Returns None when either is unknown.
        """
        if self.queued_at is None or self.start_time is None:
            return None
        wait = self.start_time - self.queued_at
        return wait if wait >= 0 else None

    @property
    def elapsed(self) -> float | None:
        """Elapsed time in seconds since job started."""
        if self.start_time is None:
            return None
        if self.end_time is not None:
            return self.end_time - self.start_time
        from snakesee.state.clock import get_clock

        return get_clock().now() - self.start_time

    @property
    def duration(self) -> float | None:
        """Total duration for completed jobs."""
        if self.start_time is not None and self.end_time is not None:
            return self.end_time - self.start_time
        return None

    def to_job_info(self) -> JobInfo:
        """Convert to immutable JobInfo for backward compatibility."""
        from snakesee.models import JobInfo

        return JobInfo(
            rule=self.rule,
            job_id=self.job_id,
            wildcards=self.wildcards if self.wildcards else None,
            start_time=self.start_time,
            end_time=self.end_time,
            input_size=self.input_size,
            threads=self.threads,
            log_file=self.log_file,
            external_jobid=self.external_jobid,
            executor=self.executor,
            region=self.region,
            log_stream=self.log_stream,
            queued_at=self.queued_at,
            queue=self.queue,
            attempt=self.attempt,
            exit_code=self.exit_code,
            status_reason=self.status_reason,
            termination_category=self.termination_category,
            termination_source=self.termination_source,
            termination_confidence=self.termination_confidence,
            cost_estimate=self.cost_estimate,
        )

    @classmethod
    def from_job_info(cls, job_info: JobInfo, key: str | None = None) -> Job:
        """Create a Job from a JobInfo instance."""
        # Determine status based on JobInfo fields
        if job_info.end_time is not None:
            status = JobStatus.COMPLETED
        elif job_info.start_time is not None:
            status = JobStatus.RUNNING
        else:
            status = JobStatus.PENDING

        # Generate key if not provided
        if key is None:
            # Use job_id if available, otherwise use deterministic hash
            # This matches apply_job_info() to avoid duplicate entries
            key = job_info.job_id or f"{job_info.rule}:{hash(job_info)}"

        return cls(
            key=key,
            rule=job_info.rule,
            status=status,
            job_id=job_info.job_id,
            start_time=job_info.start_time,
            end_time=job_info.end_time,
            wildcards=dict(job_info.wildcards) if job_info.wildcards else {},
            threads=job_info.threads,
            input_size=job_info.input_size,
            log_file=job_info.log_file,
            external_jobid=job_info.external_jobid,
            executor=job_info.executor,
            region=job_info.region,
            log_stream=job_info.log_stream,
            queued_at=job_info.queued_at,
            queue=job_info.queue,
            attempt=job_info.attempt,
            exit_code=job_info.exit_code,
            status_reason=job_info.status_reason,
            termination_category=job_info.termination_category,
            termination_source=job_info.termination_source,
            termination_confidence=job_info.termination_confidence,
            cost_estimate=job_info.cost_estimate,
        )


class JobRegistry:
    """Central registry for all job state.

    Provides O(1) lookup by job key or job_id, and efficient iteration
    over jobs by status.

    Example:
        >>> registry = JobRegistry()
        >>> job = registry.get_or_create("job_1", "align")
        >>> job.status = JobStatus.RUNNING
        >>> job.start_time = time.time()
        >>> registry.update_indexes(job)
        >>> running = registry.running()  # [job]
    """

    def __init__(self) -> None:
        """Initialize empty registry."""
        self._lock = threading.RLock()
        self._jobs: dict[str, Job] = {}
        self._by_job_id: dict[str, str] = {}  # job_id -> key
        self._by_status: dict[JobStatus, set[str]] = {status: set() for status in JobStatus}
        self._by_rule: dict[str, set[str]] = {}

    def __len__(self) -> int:
        """Return number of jobs in registry."""
        with self._lock:
            return len(self._jobs)

    def __contains__(self, key: str) -> bool:
        """Check if job exists by key."""
        with self._lock:
            return key in self._jobs

    def get(self, key: str) -> Job | None:
        """Get job by key."""
        with self._lock:
            return self._jobs.get(key)

    def get_by_job_id(self, job_id: str) -> Job | None:
        """Get job by Snakemake job_id."""
        with self._lock:
            key = self._by_job_id.get(job_id)
            return self._jobs.get(key) if key else None

    def get_or_create(self, key: str, rule: str) -> tuple[Job, bool]:
        """Get existing job or create a new one.

        Args:
            key: Unique job key.
            rule: Rule name for new job.

        Returns:
            Tuple of (job, created) where created is True if job was new.
        """
        with self._lock:
            if key in self._jobs:
                return self._jobs[key], False

            job = Job(key=key, rule=rule)
            self._add_unlocked(job)
            return job, True

    def add(self, job: Job) -> None:
        """Add a job to the registry."""
        with self._lock:
            self._add_unlocked(job)

    def _add_unlocked(self, job: Job) -> None:
        """Internal add with index updates (caller must hold lock)."""
        self._jobs[job.key] = job
        self._by_status[job.status].add(job.key)

        if job.job_id is not None:
            self._by_job_id[job.job_id] = job.key

        if job.rule not in self._by_rule:
            self._by_rule[job.rule] = set()
        self._by_rule[job.rule].add(job.key)

    def update_indexes(self, job: Job, old_status: JobStatus | None = None) -> None:
        """Update indexes after job state change.

        Args:
            job: The job that was updated.
            old_status: Previous status if status changed, for index update.
        """
        with self._lock:
            self._update_indexes_unlocked(job, old_status)

    def _update_indexes_unlocked(self, job: Job, old_status: JobStatus | None = None) -> None:
        """Update indexes without acquiring lock (caller must hold lock)."""
        # Update job_id index if needed
        if job.job_id is not None and job.job_id not in self._by_job_id:
            self._by_job_id[job.job_id] = job.key

        # Update status index if status changed
        if old_status is not None and old_status != job.status:
            self._by_status[old_status].discard(job.key)
            self._by_status[job.status].add(job.key)

    def set_status(self, job: Job, status: JobStatus) -> None:
        """Update job status and indexes."""
        with self._lock:
            old_status = job.status
            job.status = status
            self._update_indexes_unlocked(job, old_status)

    def running(self) -> list[Job]:
        """Get all running jobs."""
        with self._lock:
            return [self._jobs[key] for key in self._by_status[JobStatus.RUNNING]]

    def completed(self) -> list[Job]:
        """Get all completed jobs."""
        with self._lock:
            return [self._jobs[key] for key in self._by_status[JobStatus.COMPLETED]]

    def failed(self) -> list[Job]:
        """Get all failed jobs."""
        with self._lock:
            return [self._jobs[key] for key in self._by_status[JobStatus.FAILED]]

    def incomplete(self) -> list[Job]:
        """Get all incomplete jobs."""
        with self._lock:
            return [self._jobs[key] for key in self._by_status[JobStatus.INCOMPLETE]]

    def submitted(self) -> list[Job]:
        """Get all submitted jobs that haven't started yet."""
        with self._lock:
            return [self._jobs[key] for key in self._by_status[JobStatus.SUBMITTED]]

    def queued(self) -> list[Job]:
        """Get all jobs queued on a remote executor (submitted, awaiting a node)."""
        with self._lock:
            return [self._jobs[key] for key in self._by_status[JobStatus.QUEUED]]

    def pending(self) -> list[Job]:
        """Get all pending jobs (not yet submitted)."""
        with self._lock:
            return [self._jobs[key] for key in self._by_status[JobStatus.PENDING]]

    def by_rule(self, rule: str) -> list[Job]:
        """Get all jobs for a specific rule."""
        with self._lock:
            keys = self._by_rule.get(rule, set())
            return [self._jobs[key] for key in keys]

    def all_jobs(self) -> list[Job]:
        """Get all jobs in the registry."""
        with self._lock:
            return list(self._jobs.values())

    def running_job_infos(self) -> list[JobInfo]:
        """Get running jobs as JobInfo for backward compatibility."""
        return [job.to_job_info() for job in self.running()]

    def completed_job_infos(self) -> list[JobInfo]:
        """Get completed jobs as JobInfo for backward compatibility."""
        return [job.to_job_info() for job in self.completed()]

    def failed_job_infos(self) -> list[JobInfo]:
        """Get failed jobs as JobInfo for backward compatibility."""
        return [job.to_job_info() for job in self.failed()]

    def submitted_job_infos(self) -> list[JobInfo]:
        """Get submitted jobs as JobInfo for backward compatibility."""
        return [job.to_job_info() for job in self.submitted()]

    def queued_job_infos(self) -> list[JobInfo]:
        """Get queued (remote, awaiting node) jobs as JobInfo."""
        return [job.to_job_info() for job in self.queued()]

    def total_cost_estimate(self) -> float | None:
        """Sum of per-job cost estimates across all jobs, or None if none have one."""
        with self._lock:
            costs = [
                job.cost_estimate for job in self._jobs.values() if job.cost_estimate is not None
            ]
        return sum(costs) if costs else None

    def clear(self) -> None:
        """Clear all jobs from the registry."""
        with self._lock:
            self._jobs.clear()
            self._by_job_id.clear()
            for status_set in self._by_status.values():
                status_set.clear()
            self._by_rule.clear()

    def apply_job_info(self, job_info: JobInfo, key: str | None = None) -> Job:
        """Add or update a job from a JobInfo.

        Args:
            job_info: JobInfo to apply.
            key: Optional key. If None, uses job_id or generates one.

        Returns:
            The created or updated Job.
        """
        with self._lock:
            # Determine key
            if key is None:
                key = job_info.job_id or f"{job_info.rule}:{hash(job_info)}"

            # Look up existing job without additional lock acquisition
            existing = self._jobs.get(key)
            if existing is None and job_info.job_id:
                existing_key = self._by_job_id.get(job_info.job_id)
                if existing_key:
                    existing = self._jobs.get(existing_key)

            if existing:
                # Update existing job
                old_status = existing.status
                if job_info.start_time is not None:
                    existing.start_time = job_info.start_time
                if job_info.end_time is not None:
                    existing.end_time = job_info.end_time
                    existing.status = JobStatus.COMPLETED
                elif job_info.start_time is not None:
                    existing.status = JobStatus.RUNNING
                if job_info.threads is not None:
                    existing.threads = job_info.threads
                if job_info.wildcards:
                    existing.wildcards = dict(job_info.wildcards)
                if job_info.input_size is not None:
                    existing.input_size = job_info.input_size
                if job_info.log_file is not None:
                    existing.log_file = job_info.log_file
                if job_info.job_id is not None and existing.job_id is None:
                    existing.job_id = job_info.job_id
                self._update_indexes_unlocked(existing, old_status)
                return existing
            else:
                # Create new job
                job = Job.from_job_info(job_info, key)
                self._add_unlocked(job)
                return job

    def apply_event(self, event: SnakeseeEvent) -> Job | None:
        """Apply a snakesee event to update job state.

        Args:
            event: The event to apply.

        Returns:
            The updated Job, or None if event couldn't be applied.
        """
        from snakesee.events import EventType

        # Ignore non-job events (e.g., WORKFLOW_STARTED, PROGRESS)
        # These don't have job_id/rule_name and would create synthetic "unknown" jobs
        if event.event_type not in {
            EventType.JOB_SUBMITTED,
            EventType.JOB_QUEUED,
            EventType.JOB_STARTED,
            EventType.JOB_FINISHED,
            EventType.JOB_ERROR,
        }:
            return None

        with self._lock:
            # Get rule name from event
            rule_name = event.rule_name or "unknown"

            # Use job_id as key if available (convert int to str for key)
            job_id_str = str(event.job_id) if event.job_id is not None else None
            key = job_id_str or f"{rule_name}:{event.timestamp}"

            # Look up job without additional lock acquisition
            job = self._jobs.get(key)
            if job is None and job_id_str:
                existing_key = self._by_job_id.get(job_id_str)
                if existing_key:
                    job = self._jobs.get(existing_key)

            if job is None:
                # Create new job from event
                job = Job(key=key, rule=rule_name)
                self._add_unlocked(job)
                if job_id_str:
                    job.job_id = job_id_str
                    self._by_job_id[job_id_str] = key
                if event.wildcards:
                    job.wildcards = dict(event.wildcards)
                if event.threads:
                    job.threads = event.threads

            # Carry remote-executor enrichment onto the job whenever present, so
            # the external id / links / queue timing survive across event types.
            self._apply_remote_fields(job, event)

            old_status = job.status

            # Update based on event type. For remote jobs the executor supplies
            # the true execution-window timestamps (started_at/stopped_at); prefer
            # them over the event emission time so duration excludes queue wait.
            if event.event_type == EventType.JOB_SUBMITTED:
                job.status = JobStatus.SUBMITTED
            elif event.event_type == EventType.JOB_QUEUED:
                # Only move *forward* into QUEUED. Remote event streams can deliver
                # out of order or re-deliver; a stale JOB_QUEUED arriving after the
                # job already started/finished must not demote it back to QUEUED.
                if job.status in (JobStatus.PENDING, JobStatus.SUBMITTED):
                    job.status = JobStatus.QUEUED
                # The queue timestamp is valid history regardless of the transition;
                # if the executor didn't supply one, the event time is the best proxy.
                if job.queued_at is None:
                    job.queued_at = (
                        event.queued_at if event.queued_at is not None else event.timestamp
                    )
            elif event.event_type == EventType.JOB_STARTED:
                job.status = JobStatus.RUNNING
                job.start_time = (
                    event.started_at if event.started_at is not None else event.timestamp
                )
                if event.threads:
                    job.threads = event.threads
            elif event.event_type == EventType.JOB_FINISHED:
                job.status = JobStatus.COMPLETED
                job.end_time = event.stopped_at if event.stopped_at is not None else event.timestamp
            elif event.event_type == EventType.JOB_ERROR:
                job.status = JobStatus.FAILED
                job.end_time = event.stopped_at if event.stopped_at is not None else event.timestamp

            self._update_indexes_unlocked(job, old_status)
            return job

    @staticmethod
    def _apply_remote_fields(job: Job, event: SnakeseeEvent) -> None:
        """Copy remote-executor fields from an event onto a job (when present)."""
        if event.external_jobid is not None:
            job.external_jobid = event.external_jobid
        if event.executor is not None:
            job.executor = event.executor
        if event.region is not None:
            job.region = event.region
        if event.log_stream is not None:
            job.log_stream = event.log_stream
        if event.queue is not None:
            job.queue = event.queue
        if event.attempt is not None:
            job.attempt = event.attempt
        if event.exit_code is not None:
            job.exit_code = event.exit_code
        if event.status_reason is not None:
            job.status_reason = event.status_reason
        if event.termination_category is not None:
            job.termination_category = event.termination_category
        if event.termination_source is not None:
            job.termination_source = event.termination_source
        if event.termination_confidence is not None:
            job.termination_confidence = event.termination_confidence
        if event.cost_estimate is not None:
            job.cost_estimate = event.cost_estimate
        if event.queued_at is not None and job.queued_at is None:
            job.queued_at = event.queued_at

    def store_threads(self, job_id: str, threads: int) -> None:
        """Store thread count for a job by job_id.

        This is used when thread info comes from events before the job
        is fully tracked.
        """
        with self._lock:
            key = self._by_job_id.get(job_id)
            if key:
                job = self._jobs.get(key)
                if job:
                    job.threads = threads
