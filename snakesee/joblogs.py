"""Where a job's logs live, and fetching their content. Called from worker threads."""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from snakesee.model import Job
from snakesee.tail import read_tail

BPEEK_TIMEOUT = 30.0
MAX_LOG_BYTES = 2 * 1024 * 1024


class SourceKind(Enum):
    """Kinds of job output the log viewer can show."""

    STDOUT = "stdout"
    STDERR = "stderr"
    BPEEK = "bpeek"
    RULE_LOG = "rule log"


@dataclass(frozen=True, slots=True)
class LogSource:
    """One viewable output of a job."""

    kind: SourceKind
    label: str
    path: Path | None = None
    external_id: str | None = None


def resolve(path_str: str, workflow_dir: Path) -> Path:
    """Resolve a path from the Snakemake log relative to the workflow directory."""
    p = Path(path_str)
    return p if p.is_absolute() else workflow_dir / p


def stderr_path_for(stdout_path: Path) -> Path:
    """Cluster stderr log: same path as stdout with ``.err`` instead of ``.out``."""
    if stdout_path.suffix == ".out":
        return stdout_path.with_suffix(".err")
    return stdout_path.with_name(stdout_path.name + ".err")


def sources_for(job: Job, workflow_dir: Path) -> list[LogSource]:
    """All log sources available for a job, in display order."""
    sources: list[LogSource] = []
    if job.cluster_log:
        out = resolve(job.cluster_log, workflow_dir)
        sources.append(LogSource(SourceKind.STDOUT, "stdout", path=out))
        sources.append(LogSource(SourceKind.STDERR, "stderr", path=stderr_path_for(out)))
    if job.external_id:
        sources.append(LogSource(SourceKind.BPEEK, "bpeek", external_id=job.external_id))
    for i, log in enumerate(job.rule_logs):
        label = "rule log" if len(job.rule_logs) == 1 else f"rule log {i + 1}"
        sources.append(LogSource(SourceKind.RULE_LOG, label, path=resolve(log, workflow_dir)))
    return sources


def bpeek_available() -> bool:
    """Whether the LSF ``bpeek`` command is on PATH."""
    return shutil.which("bpeek") is not None


def fetch(source: LogSource, max_lines: int = 20000) -> list[str]:
    """Fetch the content of a log source as lines. Never raises; errors become text."""
    if source.kind is SourceKind.BPEEK:
        return _fetch_bpeek(source.external_id or "", max_lines)
    assert source.path is not None
    try:
        lines, truncated = read_tail(source.path, MAX_LOG_BYTES)
    except FileNotFoundError:
        hint = ""
        if source.kind in (SourceKind.STDOUT, SourceKind.STDERR):
            hint = "  LSF writes .out/.err only when a job ends; for a running job press b (bpeek)."
        return [f"[file does not exist (yet): {source.path}]", hint]
    except OSError as e:
        return [f"[cannot read {source.path}: {e}]"]
    if truncated:
        lines.insert(0, f"[... showing last {MAX_LOG_BYTES // 1024} KiB of {source.path} ...]")
    if not lines:
        lines = [f"[empty: {source.path}]"]
    return lines[-max_lines:]


def _fetch_bpeek(external_id: str, max_lines: int) -> list[str]:
    if not external_id:
        return ["[no cluster job id known for this job]"]
    try:
        proc = subprocess.run(
            ["bpeek", external_id],
            capture_output=True,
            text=True,
            errors="replace",
            timeout=BPEEK_TIMEOUT,
            stdin=subprocess.DEVNULL,
        )
    except FileNotFoundError:
        return ["[bpeek not found on PATH]"]
    except subprocess.TimeoutExpired:
        return [f"[bpeek {external_id} timed out after {BPEEK_TIMEOUT:.0f}s]"]
    lines = proc.stdout.splitlines()
    err = proc.stderr.strip()
    if err:
        lines.append(f"[bpeek: {err}]")
    if not lines:
        lines = [f"[bpeek {external_id}: no output]"]
    return lines[-max_lines:]
