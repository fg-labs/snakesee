"""Shared test fixtures for snakesee tests."""

import json
import sqlite3
import time
from pathlib import Path

import pytest

from snakesee.events import EventType
from snakesee.events import SnakeseeEvent
from snakesee.models import JobInfo
from snakesee.models import TimeEstimate
from snakesee.models import WorkflowProgress
from snakesee.models import WorkflowStatus
from tests.db_helpers import create_metadata_schema

# =============================================================================
# Factory Functions for Test Data
# =============================================================================


def make_job_info(
    rule: str = "test_rule",
    job_id: str | None = None,
    start_time: float | None = None,
    end_time: float | None = None,
    threads: int | None = None,
    wildcards: dict[str, str] | None = None,
    output_file: Path | None = None,
) -> JobInfo:
    """Create a JobInfo with sensible defaults."""
    return JobInfo(
        rule=rule,
        job_id=job_id,
        start_time=start_time,
        end_time=end_time,
        threads=threads,
        wildcards=wildcards,
        output_file=output_file,
    )


def make_workflow_progress(
    status: WorkflowStatus = WorkflowStatus.RUNNING,
    total_jobs: int = 100,
    completed_jobs: int = 50,
    failed_jobs: int = 0,
    running_jobs: list[JobInfo] | None = None,
    recent_completions: list[JobInfo] | None = None,
    failed_jobs_list: list[JobInfo] | None = None,
    incomplete_jobs_list: list[JobInfo] | None = None,
    start_time: float | None = None,
    workflow_dir: Path | None = None,
) -> WorkflowProgress:
    """Create a WorkflowProgress with sensible defaults."""
    return WorkflowProgress(
        workflow_dir=workflow_dir or Path("."),
        status=status,
        total_jobs=total_jobs,
        completed_jobs=completed_jobs,
        failed_jobs=failed_jobs,
        running_jobs=running_jobs or [],
        recent_completions=recent_completions or [],
        failed_jobs_list=failed_jobs_list or [],
        incomplete_jobs_list=incomplete_jobs_list or [],
        start_time=start_time or time.time() - 300,  # 5 minutes ago by default
    )


def make_time_estimate(
    seconds_remaining: float = 300.0,
    confidence: float = 0.8,
    method: str = "weighted",
    lower_bound: float | None = None,
    upper_bound: float | None = None,
) -> TimeEstimate:
    """Create a TimeEstimate with sensible defaults."""
    return TimeEstimate(
        seconds_remaining=seconds_remaining,
        confidence=confidence,
        method=method,
        lower_bound=lower_bound or seconds_remaining * 0.7,
        upper_bound=upper_bound or seconds_remaining * 1.3,
    )


def make_snakesee_event(
    event_type: EventType,
    rule_name: str = "test_rule",
    job_id: int = 1,
    timestamp: float | None = None,
    threads: int | None = None,
    wildcards: tuple[tuple[str, str], ...] | None = None,
    duration: float | None = None,
    total_jobs: int | None = None,
    completed_jobs: int | None = None,
) -> SnakeseeEvent:
    """Create a SnakeseeEvent for testing."""
    return SnakeseeEvent(
        event_type=event_type,
        timestamp=timestamp or time.time(),
        rule_name=rule_name,
        job_id=job_id,
        threads=threads,
        wildcards=wildcards,
        duration=duration,
        total_jobs=total_jobs,
        completed_jobs=completed_jobs,
    )


# =============================================================================
# Original Fixtures
# =============================================================================


@pytest.fixture
def snakemake_dir(tmp_path: Path) -> Path:
    """Create a mock .snakemake directory structure."""
    smk_dir = tmp_path / ".snakemake"
    smk_dir.mkdir()
    (smk_dir / "log").mkdir()
    (smk_dir / "metadata").mkdir()
    (smk_dir / "incomplete").mkdir()
    (smk_dir / "locks").mkdir()
    return smk_dir


@pytest.fixture
def metadata_dir(tmp_path: Path) -> Path:
    """Create a mock metadata directory with sample data."""
    meta_dir = tmp_path / ".snakemake" / "metadata"
    meta_dir.mkdir(parents=True)

    # Use recent timestamps relative to now for realistic temporal weighting tests
    now = time.time()
    day_seconds = 86400

    # Create metadata for align rule (100s duration)
    # Spread across different days for temporal weighting
    for i in range(5):
        days_ago = (4 - i) * 2  # 8, 6, 4, 2, 0 days ago (oldest to newest)
        base_time = now - (days_ago * day_seconds)
        metadata = {
            "rule": "align",
            "starttime": base_time,
            "endtime": base_time + 100.0,  # 100s duration
        }
        (meta_dir / f"align_{i}").write_text(json.dumps(metadata))

    # Create metadata for sort rule (50s duration)
    for i in range(3):
        days_ago = (2 - i) * 3  # 6, 3, 0 days ago
        base_time = now - (days_ago * day_seconds)
        metadata = {
            "rule": "sort",
            "starttime": base_time,
            "endtime": base_time + 50.0,  # 50s duration
        }
        (meta_dir / f"sort_{i}").write_text(json.dumps(metadata))

    return meta_dir


@pytest.fixture
def metadata_db(tmp_path: Path) -> Path:
    """Create a mock .snakemake directory with SQLite metadata DB."""
    smk_dir = tmp_path / ".snakemake"
    smk_dir.mkdir(parents=True)
    (smk_dir / "log").mkdir()
    (smk_dir / "locks").mkdir()

    db_path = smk_dir / "metadata.db"
    namespace = str(smk_dir.resolve())

    conn = sqlite3.connect(str(db_path))
    create_metadata_schema(conn)

    now = time.time()
    day_seconds = 86400

    # Same data as the filesystem metadata_dir fixture
    for i in range(5):
        days_ago = (4 - i) * 2
        base_time = now - (days_ago * day_seconds)
        conn.execute(
            """INSERT INTO snakemake_metadata
               (namespace, target, rule, starttime, endtime, record_format_version)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (namespace, f"align_{i}.bam", "align", base_time, base_time + 100.0, 6),
        )

    for i in range(3):
        days_ago = (2 - i) * 3
        base_time = now - (days_ago * day_seconds)
        conn.execute(
            """INSERT INTO snakemake_metadata
               (namespace, target, rule, starttime, endtime, record_format_version)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (namespace, f"sort_{i}.bam", "sort", base_time, base_time + 50.0, 6),
        )

    conn.commit()
    conn.close()
    return db_path


@pytest.fixture
def sample_log_content() -> str:
    """Sample snakemake log content for testing."""
    return """Building DAG of jobs...
Using shell: /bin/bash
rule align:
    jobid: 1
    output: sample1.bam
Finished job 1.
rule sort:
    jobid: 2
    output: sample1.sorted.bam
Finished job 2.
2 of 10 steps (20%) done
"""
