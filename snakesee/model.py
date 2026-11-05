"""Core data model: jobs and overall run state, as reconstructed from the Snakemake log."""

from __future__ import annotations

from dataclasses import dataclass
from dataclasses import field
from enum import Enum


class JobStatus(Enum):
    """Lifecycle of a single Snakemake job as visible from the main log."""

    ACTIVE = "active"  # submitted/started, not yet finished (pending *or* running on the cluster)
    DONE = "done"
    FAILED = "failed"


class RunStatus(Enum):
    """Overall state of the Snakemake run."""

    STARTING = "starting"  # log exists, no jobs yet (DAG building etc.)
    RUNNING = "running"
    DRAINING = "draining"  # a job failed; Snakemake is waiting for running jobs to finish
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    NOTHING_TO_DO = "nothing to do"


@dataclass(slots=True)
class Job:
    """One Snakemake job (identified by its Snakemake ``jobid``)."""

    jobid: int
    rule: str
    status: JobStatus = JobStatus.ACTIVE
    wildcards: str = ""
    threads: int | None = None
    output: str = ""
    rule_logs: list[str] = field(default_factory=list)
    """Paths from the rule's ``log:`` directive."""
    external_id: str | None = None
    """Cluster job id (e.g. the LSF jobid)."""
    cluster_log: str | None = None
    """Cluster stdout log path (e.g. ``.snakemake/lsf_logs/.../<uuid>.out``)."""
    submit_time: float | None = None
    end_time: float | None = None
    error_message: str = ""
    attempts: int = 1

    def copy(self) -> Job:
        """Return a shallow copy safe to hand to another thread."""
        return Job(
            jobid=self.jobid,
            rule=self.rule,
            status=self.status,
            wildcards=self.wildcards,
            threads=self.threads,
            output=self.output,
            rule_logs=list(self.rule_logs),
            external_id=self.external_id,
            cluster_log=self.cluster_log,
            submit_time=self.submit_time,
            end_time=self.end_time,
            error_message=self.error_message,
            attempts=self.attempts,
        )


@dataclass(frozen=True, slots=True)
class RunSummary:
    """Immutable snapshot of run-level information for display."""

    status: RunStatus = RunStatus.STARTING
    steps_done: int = 0
    steps_total: int = 0
    """Total from the latest ``N of M steps`` line, or the Job stats table before that."""
    n_active: int = 0
    n_done: int = 0
    n_failed: int = 0
    snakemake_version: str = ""
    command: str = ""
