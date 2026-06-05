"""Tests for deep remote observability: retries, exit codes, reasons, spot (Phase 3)."""

from __future__ import annotations

from snakesee.events import EventType
from snakesee.events import SnakeseeEvent
from snakesee.models import JobInfo
from snakesee.remote_links import is_spot_interruption
from snakesee.state.job_registry import JobRegistry
from snakesee.tui.renderables import make_remote_job_info


class TestSpotDetection:
    def test_detects_spot_markers(self) -> None:
        assert is_spot_interruption("Spot interruption: capacity reclaimed")
        assert is_spot_interruption("EC2 Spot instance was reclaimed")
        assert is_spot_interruption("Host EC2 (instance i-abc) terminated due to spot")

    def test_non_spot_reasons(self) -> None:
        assert not is_spot_interruption(None)
        assert not is_spot_interruption("")
        assert not is_spot_interruption("Essential container in task exited")
        # Host terminated without any spot hint is not classified as spot.
        assert not is_spot_interruption("Host EC2 (instance i-abc) terminated")


class TestApplyEventPopulatesObservability:
    def test_error_event_carries_exit_code_and_reason(self) -> None:
        reg = JobRegistry()
        reg.apply_event(
            SnakeseeEvent(
                event_type=EventType.JOB_ERROR,
                timestamp=200.0,
                job_id=7,
                rule_name="align",
                executor="aws-batch",
                exit_code=137,
                status_reason="Spot interruption: capacity reclaimed",
                attempt=2,
            )
        )
        job = reg.get_by_job_id("7")
        assert job is not None
        assert job.exit_code == 137
        assert job.status_reason == "Spot interruption: capacity reclaimed"
        assert job.attempt == 2
        info = job.to_job_info()
        assert info.exit_code == 137
        assert info.attempt == 2

    def test_finished_event_carries_exit_code_zero(self) -> None:
        # The common clean-finish path: exit_code 0 must survive (not be dropped
        # as falsy) through apply_event and to_job_info.
        reg = JobRegistry()
        reg.apply_event(
            SnakeseeEvent(
                event_type=EventType.JOB_FINISHED,
                timestamp=200.0,
                job_id=7,
                rule_name="align",
                executor="aws-batch",
                exit_code=0,
                attempt=1,
                stopped_at=200.0,
            )
        )
        job = reg.get_by_job_id("7")
        assert job is not None
        assert job.exit_code == 0
        assert job.to_job_info().exit_code == 0

    def test_job_info_round_trip_preserves_observability(self) -> None:
        from snakesee.state.job_registry import Job

        info = JobInfo(
            rule="align",
            job_id="7",
            external_jobid="abc",
            attempt=3,
            exit_code=0,
            status_reason="ok",
        )
        job = Job.from_job_info(info)
        assert (job.attempt, job.exit_code, job.status_reason) == (3, 0, "ok")
        back = job.to_job_info()
        assert (back.attempt, back.exit_code, back.status_reason) == (3, 0, "ok")


class TestEnrichRunningObservability:
    """A running remote job's detail backfills observability fields from the registry."""

    def test_running_job_backfills_exit_code_zero(
        self, snakemake_dir: object, tmp_path: object
    ) -> None:
        from pathlib import Path

        from snakesee.models import WorkflowProgress
        from snakesee.models import WorkflowStatus
        from snakesee.tui.data_source import WorkflowDataSource

        assert isinstance(tmp_path, Path)
        ds = WorkflowDataSource(workflow_dir=tmp_path, use_estimation=False)
        base = WorkflowProgress(
            workflow_dir=tmp_path, status=WorkflowStatus.RUNNING, total_jobs=1, completed_jobs=0
        )
        # A started remote job whose registry record carries attempt/exit_code=0;
        # the freshly-built running JobInfo must be backfilled (0 not dropped).
        result = ds.apply_events_to_progress(
            base,
            [
                SnakeseeEvent(
                    event_type=EventType.JOB_STARTED,
                    timestamp=150.0,
                    job_id=7,
                    rule_name="align",
                    executor="aws-batch",
                    external_jobid="arn:aws:batch:us-east-1:1:job/abc",
                    attempt=2,
                    exit_code=0,
                    started_at=142.0,
                )
            ],
        )
        running = [j for j in result.running_jobs if j.job_id == "7"]
        assert len(running) == 1
        assert running[0].attempt == 2
        assert running[0].exit_code == 0


class TestObservabilityDisplay:
    def test_failed_remote_job_shows_exit_code_reason_and_spot(self) -> None:
        job = JobInfo(
            rule="align",
            job_id="7",
            external_jobid="arn:aws:batch:us-east-1:1:job/abc",
            executor="aws-batch",
            attempt=2,
            exit_code=137,
            status_reason="Spot interruption: capacity reclaimed",
        )
        lines = make_remote_job_info(job)
        assert any("attempt: 2" in line.plain for line in lines)
        assert any("exit code: 137" in line.plain for line in lines)
        assert any("spot interrupted" in line.plain for line in lines)
        assert any("reason:" in line.plain and "capacity reclaimed" in line.plain for line in lines)

    def test_first_attempt_not_shown(self) -> None:
        # attempt == 1 is the normal case; don't clutter the detail with it.
        job = JobInfo(rule="align", job_id="7", external_jobid="abc", attempt=1)
        lines = make_remote_job_info(job)
        assert not any("attempt:" in line.plain for line in lines)

    def test_clean_success_has_no_reason_or_spot(self) -> None:
        job = JobInfo(rule="align", job_id="7", external_jobid="abc", exit_code=0)
        lines = make_remote_job_info(job)
        assert any("exit code: 0" in line.plain for line in lines)
        assert not any("spot interrupted" in line.plain for line in lines)
        assert not any("reason:" in line.plain for line in lines)
