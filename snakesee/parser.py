"""Incremental parser for the main Snakemake log (``.snakemake/log/*.snakemake.log``).

The parser is fed complete lines one at a time and keeps a dict of jobs keyed by
Snakemake jobid. It never re-reads anything: cost is proportional to the number of
*new* lines, so polling a large, long-running log stays cheap.

It is deliberately free of I/O and threading so it can be unit-tested directly.
"""

from __future__ import annotations

import re
import time
from collections.abc import Iterable

from snakesee.model import Job
from snakesee.model import JobStatus
from snakesee.model import RunStatus
from snakesee.model import RunSummary

# "[Thu Nov  5 12:16:01 2026]"
TIMESTAMP_RE = re.compile(r"^\[(\w{3} \w{3} +\d+ \d+:\d+:\d+ \d{4})\]")
# "rule foo:", "localrule foo:", "checkpoint foo:", "localcheckpoint foo:"
RULE_START_RE = re.compile(r"^(?:local)?(?:rule|checkpoint) (\w+):\s*$")
# "Error in rule foo:"
ERROR_START_RE = re.compile(r"^Error in rule (\w+):\s*$")
# Indented block field: "    jobid: 5". Exactly four spaces so that deeper-indented
# continuation lines (e.g. the shell command in an error block) are ignored.
FIELD_RE = re.compile(r"^    (\w+):\s?(.*)$")
# Snakemake <9: "Finished job 5."   Snakemake 9: "Finished jobid: 5 (Rule: foo)"
FINISHED_RE = re.compile(r"^Finished (?:job |jobid:\s*)(\d+)")
# "15 of 50 steps (30%) done"
PROGRESS_RE = re.compile(r"^(\d+) of (\d+) steps \(")
# snakemake-executor-plugin-lsf:
# "Job 3233 has been submitted with LSF jobid 101867798 (log: /path/to/x.out)."
LSF_SUBMIT_RE = re.compile(
    r"^Job (\d+) has been submitted with LSF jobid (\d+)(?: \(log: (.+)\))?\.?\s*$"
)
# Generic executors / cluster-generic: "Submitted job 5 with external jobid '12345'."
GENERIC_SUBMIT_RE = re.compile(r"^Submitted job (\d+) with external jobid '(.*)'")
LSF_BSUB_ID_RE = re.compile(r"Job <(\d+)>")
FIRST_NUMBER_RE = re.compile(r"\d+")
# Job stats table rows: "run_total_model            241"
JOB_STATS_ROW_RE = re.compile(r"^(\S+)\s+(\d+)\s*$")
VERSION_RE = re.compile(r"^\s+Snakemake version:\s*(\S+)")
COMMAND_RE = re.compile(r"^\s+Command:\s*(.*)$")

LOG_SUFFIX_NOISE = " (check log file(s) for error details)"
MESSAGE_SUFFIX_NOISE = "For further error details"


def parse_timestamp(text: str) -> float | None:
    """Parse a Snakemake timestamp body like ``Thu Nov  5 12:16:01 2026`` (local time)."""
    try:
        return time.mktime(time.strptime(text, "%a %b %d %H:%M:%S %Y"))
    except (ValueError, OverflowError):
        return None


def _extract_external_id(raw: str) -> str:
    """Pull a job id out of whatever the submit command printed.

    ``bsub`` prints ``Job <123> is submitted to queue <q>.``; ``sbatch --parsable``
    prints ``123;cluster``; most others print just the id.
    """
    m = LSF_BSUB_ID_RE.search(raw)
    if m:
        return m.group(1)
    m = FIRST_NUMBER_RE.search(raw)
    return m.group(0) if m else raw.strip()


def _split_list(value: str) -> list[str]:
    return [v.strip() for v in value.split(",") if v.strip()]


class LogParser:
    """Stateful, incremental parser for one Snakemake log file."""

    def __init__(self) -> None:
        self.jobs: dict[int, Job] = {}
        self.rule_counts: dict[str, int] = {}
        """Per-rule job counts from the latest ``Job stats`` table."""
        self._status = RunStatus.STARTING
        self._steps_done = 0
        self._steps_total = 0
        self._version = ""
        self._command = ""
        self._counts = {s: 0 for s in JobStatus}
        self._changed: set[int] = set()
        self._summary_changed = True

        self._last_time: float | None = None
        self._last_time_raw = ""
        # Current multi-line block: ("rule"|"error", rule name, fields so far, jobid or None)
        self._block_kind: str | None = None
        self._block_rule = ""
        self._block_fields: dict[str, str] = {}
        self._block_job: Job | None = None
        self._in_job_stats = False
        self._saw_error = False

    # ----------------------------------------------------------------- public API
    def feed(self, lines: Iterable[str]) -> None:
        """Consume complete lines (without trailing newlines)."""
        for line in lines:
            self._feed_line(line)

    def drain_changes(self) -> list[Job]:
        """Return copies of jobs changed since the last call, and reset the change set."""
        changed = [self.jobs[j].copy() for j in self._changed]
        self._changed.clear()
        return changed

    def drain_summary(self) -> RunSummary | None:
        """Return a new summary if anything run-level changed since the last call."""
        if not self._summary_changed:
            return None
        self._summary_changed = False
        return self.summary()

    def summary(self) -> RunSummary:
        """Snapshot of run-level state."""
        total = self._steps_total or sum(
            n for rule, n in self.rule_counts.items() if rule != "total"
        )
        return RunSummary(
            status=self._status,
            steps_done=self._steps_done,
            steps_total=total,
            n_active=self._counts[JobStatus.ACTIVE],
            n_done=self._counts[JobStatus.DONE],
            n_failed=self._counts[JobStatus.FAILED],
            snakemake_version=self._version,
            command=self._command,
        )

    # ------------------------------------------------------------------ internals
    def _feed_line(self, line: str) -> None:  # noqa: C901 - flat dispatch is clearest here
        # Inside a block, indented "key: value" lines belong to it.
        if self._block_kind is not None:
            m = FIELD_RE.match(line)
            if m:
                self._block_field(m.group(1), m.group(2))
                return
            if line.startswith("        "):  # deeper continuation (shell command etc.)
                return
            self._end_block()

        if self._in_job_stats:
            if self._job_stats_line(line):
                return

        if line.startswith("["):
            m = TIMESTAMP_RE.match(line)
            if m:
                raw = m.group(1)
                if raw != self._last_time_raw:  # consecutive lines often repeat it
                    self._last_time_raw = raw
                    self._last_time = parse_timestamp(raw)
                return

        m = RULE_START_RE.match(line)
        if m:
            self._start_block("rule", m.group(1))
            return
        m = ERROR_START_RE.match(line)
        if m:
            self._start_block("error", m.group(1))
            return
        m = FINISHED_RE.match(line)
        if m:
            self._finish(int(m.group(1)))
            return
        m = LSF_SUBMIT_RE.match(line)
        if m:
            self._submitted(int(m.group(1)), m.group(2), m.group(3))
            return
        m = GENERIC_SUBMIT_RE.match(line)
        if m:
            self._submitted(int(m.group(1)), _extract_external_id(m.group(2)), None)
            return
        m = PROGRESS_RE.match(line)
        if m:
            self._steps_done = int(m.group(1))
            self._steps_total = int(m.group(2))
            self._summary_changed = True
            return
        if line.startswith("Job stats:"):
            self._in_job_stats = True
            self.rule_counts = {}
            return
        self._run_level_line(line)

    def _run_level_line(self, line: str) -> None:
        new_status: RunStatus | None = None
        if line.startswith("Complete log"):
            if self._status not in (RunStatus.FAILED, RunStatus.NOTHING_TO_DO):
                new_status = RunStatus.FAILED if self._saw_error else RunStatus.SUCCEEDED
        elif line.startswith("Nothing to be done"):
            new_status = RunStatus.NOTHING_TO_DO
        elif line.startswith("Exiting because a job execution failed") or line.startswith(
            "Cancelling snakemake on user request"
        ):
            new_status = RunStatus.FAILED
        elif line.startswith("Shutting down, this might take some time") or line.startswith(
            "Will exit after finishing currently running jobs"
        ):
            new_status = RunStatus.DRAINING
        else:
            m = VERSION_RE.match(line)
            if m:
                self._version = m.group(1)
                self._summary_changed = True
                return
            m = COMMAND_RE.match(line)
            if m:
                self._command = m.group(1)
                self._summary_changed = True
                return
        if new_status is not None and new_status != self._status:
            self._status = new_status
            self._summary_changed = True

    def _job_stats_line(self, line: str) -> bool:
        """Handle a line inside the Job stats table. Returns True if consumed."""
        stripped = line.strip()
        if not stripped or stripped.startswith("job ") or stripped.startswith("---"):
            return True
        m = JOB_STATS_ROW_RE.match(stripped)
        if not m:
            self._in_job_stats = False
            return False
        name, count = m.group(1), int(m.group(2))
        self.rule_counts[name] = count
        if name == "total":
            self._in_job_stats = False
            if not self._steps_total or self._steps_done == 0:
                self._steps_total = count
        self._summary_changed = True
        return True

    def _start_block(self, kind: str, rule: str) -> None:
        self._block_kind = kind
        self._block_rule = rule
        self._block_fields = {}
        self._block_job = None

    def _block_field(self, key: str, value: str) -> None:
        self._block_fields[key] = value
        if key == "jobid" and self._block_job is None:
            try:
                jobid = int(value)
            except ValueError:
                return
            self._block_job = self._open_job(jobid)
            for k, v in self._block_fields.items():
                if k != "jobid":
                    self._apply_field(self._block_job, k, v)
        elif self._block_job is not None:
            self._apply_field(self._block_job, key, value)

    def _open_job(self, jobid: int) -> Job:
        """Create/update the job a block refers to."""
        job = self.jobs.get(jobid)
        if self._block_kind == "rule":
            if job is None:
                job = Job(jobid=jobid, rule=self._block_rule, submit_time=self._last_time)
                self.jobs[jobid] = job
                self._counts[JobStatus.ACTIVE] += 1
                self._summary_changed = True
                if self._status == RunStatus.STARTING:
                    self._status = RunStatus.RUNNING
            else:
                # Retry (--retries) or restart: same jobid shows up again.
                job.attempts += 1
                job.submit_time = self._last_time
                job.end_time = None
                job.error_message = ""
                self._set_status(job, JobStatus.ACTIVE)
        else:  # error block
            if job is None:
                job = Job(jobid=jobid, rule=self._block_rule)
                self.jobs[jobid] = job
                self._counts[JobStatus.ACTIVE] += 1
            job.end_time = self._last_time
            self._saw_error = True
            self._set_status(job, JobStatus.FAILED)
        self._changed.add(jobid)
        return job

    def _apply_field(self, job: Job, key: str, value: str) -> None:
        changed = True
        if self._block_kind == "rule":
            if key == "wildcards":
                job.wildcards = value
            elif key == "threads":
                try:
                    job.threads = int(value)
                except ValueError:
                    changed = False
            elif key == "output":
                job.output = value
            elif key == "log":
                job.rule_logs = _split_list(value)
            else:
                changed = False
        else:
            if key == "message":
                idx = value.find(MESSAGE_SUFFIX_NOISE)
                job.error_message = (value[:idx] if idx >= 0 else value).strip()
            elif key == "external_jobid" and not job.external_id:
                job.external_id = value.strip()
            elif key == "log" and not job.cluster_log and not job.rule_logs:
                job.rule_logs = _split_list(value.replace(LOG_SUFFIX_NOISE, ""))
            else:
                changed = False
        if changed:
            self._changed.add(job.jobid)

    def _end_block(self) -> None:
        self._block_kind = None
        self._block_job = None
        self._block_fields = {}

    def _finish(self, jobid: int) -> None:
        job = self.jobs.get(jobid)
        if job is None:
            return
        job.end_time = self._last_time
        self._set_status(job, JobStatus.DONE)
        self._changed.add(jobid)

    def _submitted(self, jobid: int, external_id: str, cluster_log: str | None) -> None:
        job = self.jobs.get(jobid)
        if job is None:
            return
        job.external_id = external_id
        if cluster_log:
            job.cluster_log = cluster_log.strip()
        self._changed.add(jobid)

    def _set_status(self, job: Job, status: JobStatus) -> None:
        if job.status is status:
            return
        self._counts[job.status] -= 1
        self._counts[status] += 1
        job.status = status
        self._summary_changed = True
