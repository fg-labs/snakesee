"""Tests for per-job and workflow cost estimate display (Phase 7)."""

from __future__ import annotations

import pytest

from snakesee.events import EventType
from snakesee.events import SnakeseeEvent
from snakesee.models import JobInfo
from snakesee.state.job_registry import JobRegistry
from snakesee.tui.renderables import format_cost
from snakesee.tui.renderables import make_remote_job_info


class TestFormatCost:
    def test_small_cost_four_decimals(self) -> None:
        assert format_cost(0.0123) == "$0.0123"

    def test_large_cost_two_decimals(self) -> None:
        assert format_cost(1234.5) == "$1,234.50"


class TestCostPlumbing:
    def test_apply_event_populates_cost(self) -> None:
        reg = JobRegistry()
        reg.apply_event(
            SnakeseeEvent(
                event_type=EventType.JOB_FINISHED,
                timestamp=200.0,
                job_id=7,
                rule_name="align",
                executor="aws-batch",
                cost_estimate=0.0456,
                stopped_at=200.0,
            )
        )
        job = reg.get_by_job_id("7")
        assert job is not None
        assert job.cost_estimate == 0.0456
        assert job.to_job_info().cost_estimate == 0.0456

    def test_total_cost_estimate_sums_jobs(self) -> None:
        reg = JobRegistry()
        for jid, cost in ((1, 0.10), (2, 0.05), (3, None)):
            reg.apply_event(
                SnakeseeEvent(
                    event_type=EventType.JOB_FINISHED,
                    timestamp=1.0,
                    job_id=jid,
                    rule_name="r",
                    cost_estimate=cost,
                    stopped_at=1.0,
                )
            )
        assert reg.total_cost_estimate() == pytest.approx(0.15)

    def test_total_cost_none_when_no_estimates(self) -> None:
        reg = JobRegistry()
        reg.apply_event(
            SnakeseeEvent(event_type=EventType.JOB_FINISHED, timestamp=1.0, job_id=1, rule_name="r")
        )
        assert reg.total_cost_estimate() is None

    def test_round_trip_preserves_cost(self) -> None:
        from snakesee.state.job_registry import Job

        info = JobInfo(rule="r", job_id="1", external_jobid="abc", cost_estimate=0.99)
        assert Job.from_job_info(info).to_job_info().cost_estimate == 0.99


class TestCostDisplay:
    def test_per_job_cost_shown(self) -> None:
        job = JobInfo(rule="align", job_id="7", external_jobid="abc", cost_estimate=0.0123)
        lines = make_remote_job_info(job)
        assert any("est. cost: $0.0123" in line for line in lines)

    def test_no_cost_line_when_absent(self) -> None:
        job = JobInfo(rule="align", job_id="7", external_jobid="abc")
        assert not any("est. cost:" in line for line in make_remote_job_info(job))

    def test_header_shows_workflow_cost(self) -> None:
        from io import StringIO
        from pathlib import Path

        from rich.console import Console

        from snakesee.models import WorkflowProgress
        from snakesee.models import WorkflowStatus
        from snakesee.tui.renderables import make_header

        progress = WorkflowProgress(
            workflow_dir=Path("/wf"),
            status=WorkflowStatus.RUNNING,
            total_jobs=3,
            completed_jobs=2,
            total_cost_estimate=0.42,
        )
        buf = StringIO()
        Console(file=buf, width=200).print(
            make_header(progress, "/wf", paused=False, event_reader=None)
        )
        out = buf.getvalue()
        assert "Cost:" in out and "$0.42" in out and "(est)" in out
