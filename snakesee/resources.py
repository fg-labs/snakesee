"""LSF resource usage: memory, run time, state. Called from worker threads only.

Two sources:

- Running jobs: one batched ``bjobs -o ... -json`` call for all active jobs.
- Finished jobs: the "Resource usage summary" LSF writes into the job's ``.out``
  file (at the top, before the job's own output). Read once per job.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

BJOBS_FIELDS = "jobid stat max_mem avg_mem memlimit run_time"
BJOBS_TIMEOUT = 60.0
BJOBS_BATCH = 500
REPORT_SCAN_BYTES = 16 * 1024

_UNIT_MB = {"": 1 / 1024, "k": 1 / 1024, "m": 1.0, "g": 1024.0, "t": 1024.0**2, "p": 1024.0**3}
_MEM_RE = re.compile(r"^\s*([\d.]+)\s*([kmgtp]?)(?:b|bytes?)?\s*$", re.IGNORECASE)
_NUMBER_RE = re.compile(r"[\d.]+")
_REPORT_FIELD_RE = re.compile(r"^\s*([A-Za-z][A-Za-z ]*?)\s*:\s*(.+?)\s*$")
_TERM_RE = re.compile(r"^(TERM_\w+):?\s*(.*)$")
_EXIT_RE = re.compile(r"^Exited with (?:exit code \d+|signal termination: .+)\.?$")


@dataclass(frozen=True, slots=True)
class JobResources:
    """Resource usage of one job, from bjobs (live) or the LSF report (final)."""

    stat: str | None = None
    """LSF job state: PEND, RUN, DONE, EXIT, PSUSP, USUSP, SSUSP, ..."""
    max_mem_mb: float | None = None
    avg_mem_mb: float | None = None
    mem_limit_mb: float | None = None
    """bjobs MEMLIMIT, or 'Total Requested Memory' from the report."""
    run_time_s: float | None = None
    cpu_time_s: float | None = None
    max_threads: int | None = None
    lsf_note: str = ""
    """Termination reason from the report, e.g. ``TERM_MEMLIMIT: job killed ...``."""
    final: bool = False
    """True when read from the finished job's report (won't change any more)."""

    @property
    def mem_fraction(self) -> float | None:
        """Max memory as a fraction of the limit/request."""
        if self.max_mem_mb is None or not self.mem_limit_mb:
            return None
        return self.max_mem_mb / self.mem_limit_mb


def parse_mem_mb(text: str | None) -> float | None:
    """``"9.9 Gbytes"``, ``"23 G"``, ``"10912 MB"`` -> MB. Bare numbers are KB (LSF default)."""
    if not text:
        return None
    m = _MEM_RE.match(text)
    if not m:
        return None
    return float(m.group(1)) * _UNIT_MB[m.group(2).lower()]


def _parse_seconds(text: str | None) -> float | None:
    if not text:
        return None
    m = _NUMBER_RE.search(text)
    return float(m.group(0)) if m else None


# ------------------------------------------------------------------------ bjobs
def bjobs_available() -> bool:
    """Whether LSF's ``bjobs`` is on PATH."""
    return shutil.which("bjobs") is not None


def parse_bjobs_json(text: str) -> dict[str, JobResources]:
    """Parse ``bjobs -o ... -json`` output. Unknown jobs and malformed records are skipped."""
    try:
        data = json.loads(text)
    except ValueError:
        return {}
    out: dict[str, JobResources] = {}
    for rec in data.get("RECORDS", []) if isinstance(data, dict) else []:
        if not isinstance(rec, dict) or "ERROR" in rec or not rec.get("STAT"):
            continue
        jobid = str(rec.get("JOBID", "")).strip()
        if not jobid:
            continue
        out[jobid] = JobResources(
            stat=str(rec["STAT"]).strip(),
            max_mem_mb=parse_mem_mb(rec.get("MAX_MEM")),
            avg_mem_mb=parse_mem_mb(rec.get("AVG_MEM")),
            mem_limit_mb=parse_mem_mb(rec.get("MEMLIMIT")),
            run_time_s=_parse_seconds(rec.get("RUN_TIME")),
        )
    return out


def query_bjobs(external_ids: list[str]) -> dict[str, JobResources]:
    """Query LSF for many jobs at once (batched). Failures yield no entries."""
    out: dict[str, JobResources] = {}
    for start in range(0, len(external_ids), BJOBS_BATCH):
        batch = external_ids[start : start + BJOBS_BATCH]
        try:
            proc = subprocess.run(
                ["bjobs", "-o", BJOBS_FIELDS, "-json", *batch],
                capture_output=True,
                text=True,
                errors="replace",
                timeout=BJOBS_TIMEOUT,
                stdin=subprocess.DEVNULL,
            )
        except (OSError, subprocess.TimeoutExpired):
            continue
        out.update(parse_bjobs_json(proc.stdout))
    return out


# ------------------------------------------------------------------- LSF report
def parse_lsf_report(text: str) -> JobResources | None:
    """Parse the LSF job report in a ``.out`` file. None if no resource summary is present."""
    lines = text.splitlines()
    try:
        start = next(i for i, ln in enumerate(lines) if ln.strip() == "Resource usage summary:")
    except StopIteration:
        return None
    note = ""
    for ln in lines[:start]:
        s = ln.strip()
        m = _TERM_RE.match(s)
        if m:
            note = f"{m.group(1)}: {m.group(2)}".rstrip(": ")
        elif not note and _EXIT_RE.match(s):
            note = s
    fields: dict[str, str] = {}
    for ln in lines[start + 1 : start + 30]:
        if ln.strip().startswith("The output (if any)"):
            break
        m = _REPORT_FIELD_RE.match(ln)
        if m:
            fields[m.group(1).lower()] = m.group(2)
    if not fields:
        return None
    threads = _parse_seconds(fields.get("max threads"))
    return JobResources(
        max_mem_mb=parse_mem_mb(fields.get("max memory")),
        avg_mem_mb=parse_mem_mb(fields.get("average memory")),
        mem_limit_mb=parse_mem_mb(fields.get("total requested memory")),
        run_time_s=_parse_seconds(fields.get("run time")),
        cpu_time_s=_parse_seconds(fields.get("cpu time")),
        max_threads=int(threads) if threads is not None else None,
        lsf_note=note,
        final=True,
    )


def read_lsf_report(path: Path) -> JobResources | None:
    """Find the report at the start of ``path`` (or, failing that, its end).

    Raises:
        OSError: if the file can't be read (e.g. not written yet).
    """
    with open(path, "rb") as fh:
        head = fh.read(REPORT_SCAN_BYTES)
        res = parse_lsf_report(head.decode("utf-8", errors="replace"))
        if res is not None:
            return res
        fh.seek(0, 2)
        size = fh.tell()
        if size <= REPORT_SCAN_BYTES:
            return None
        fh.seek(max(REPORT_SCAN_BYTES, size - REPORT_SCAN_BYTES))
        tail = fh.read()
    return parse_lsf_report(tail.decode("utf-8", errors="replace"))
