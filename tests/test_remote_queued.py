"""Tests for the QUEUED state and remote-field enrichment (Phase 1)."""

from __future__ import annotations

from snakesee.events import EventType
from snakesee.events import SnakeseeEvent
from snakesee.state.job_registry import JobRegistry
from snakesee.state.job_registry import JobStatus

ARN = "arn:aws:batch:us-east-1:1:job/abc"


def _event(event_type: EventType, **kwargs: object) -> SnakeseeEvent:
    return SnakeseeEvent(event_type=event_type, timestamp=kwargs.pop("timestamp", 0.0), **kwargs)  # type: ignore[arg-type]


class TestQueuedTransitions:
    """A remote job moves pending -> queued -> running -> completed via events."""

    def test_job_queued_event_sets_queued_status(self) -> None:
        reg = JobRegistry()
        reg.apply_event(
            _event(
                EventType.JOB_QUEUED,
                timestamp=100.0,
                job_id=7,
                rule_name="align",
                executor="aws-batch",
                external_jobid=ARN,
                queued_at=100.0,
            )
        )
        job = reg.get_by_job_id("7")
        assert job is not None
        assert job.status is JobStatus.QUEUED
        assert job.queued_at == 100.0
        assert job.external_jobid == ARN
        assert job.executor == "aws-batch"
        assert reg.queued() == [job]

    def test_queued_then_started_moves_out_of_queued(self) -> None:
        reg = JobRegistry()
        reg.apply_event(_event(EventType.JOB_QUEUED, timestamp=100.0, job_id=7, rule_name="align"))
        reg.apply_event(_event(EventType.JOB_STARTED, timestamp=150.0, job_id=7, started_at=142.0))
        job = reg.get_by_job_id("7")
        assert job is not None
        assert job.status is JobStatus.RUNNING
        # start_time is the executor's true start, not the event emission time.
        assert job.start_time == 142.0
        assert reg.queued() == []
        assert reg.running() == [job]

    def test_queue_wait_computed(self) -> None:
        reg = JobRegistry()
        reg.apply_event(_event(EventType.JOB_QUEUED, timestamp=100.0, job_id=7, queued_at=100.0))
        reg.apply_event(_event(EventType.JOB_STARTED, timestamp=160.0, job_id=7, started_at=142.0))
        job = reg.get_by_job_id("7")
        assert job is not None
        assert job.queue_wait == 42.0  # 142 - 100

    def test_finished_uses_stopped_at_for_execution_window(self) -> None:
        reg = JobRegistry()
        reg.apply_event(_event(EventType.JOB_QUEUED, timestamp=100.0, job_id=7, queued_at=100.0))
        reg.apply_event(_event(EventType.JOB_STARTED, timestamp=150.0, job_id=7, started_at=142.0))
        reg.apply_event(_event(EventType.JOB_FINISHED, timestamp=210.0, job_id=7, stopped_at=200.0))
        job = reg.get_by_job_id("7")
        assert job is not None
        assert job.status is JobStatus.COMPLETED
        assert job.end_time == 200.0
        # duration is the execution window (200 - 142), excluding the 42s queue wait.
        assert job.duration == 58.0


class TestRemoteFieldEnrichment:
    """apply_event carries remote fields onto an existing job across events."""

    def test_started_event_populates_remote_fields(self) -> None:
        reg = JobRegistry()
        reg.apply_event(
            _event(
                EventType.JOB_STARTED,
                timestamp=150.0,
                job_id=7,
                rule_name="align",
                executor="aws-batch",
                external_jobid=ARN,
                region="us-east-1",
                log_stream="JobDef/default/abc",
                started_at=142.0,
            )
        )
        job = reg.get_by_job_id("7")
        assert job is not None
        assert job.external_jobid == ARN
        assert job.executor == "aws-batch"
        assert job.region == "us-east-1"
        assert job.log_stream == "JobDef/default/abc"
        # to_job_info round-trips the remote fields.
        info = job.to_job_info()
        assert info.external_jobid == ARN
        assert info.region == "us-east-1"

    def test_unknown_event_type_still_ignored(self) -> None:
        reg = JobRegistry()
        assert reg.apply_event(_event(EventType.PROGRESS, timestamp=1.0)) is None


class TestOutOfOrderEvents:
    """Stale/out-of-order remote events must not corrupt the state machine."""

    def test_late_queued_does_not_demote_running(self) -> None:
        reg = JobRegistry()
        reg.apply_event(_event(EventType.JOB_STARTED, timestamp=150.0, job_id=7, started_at=142.0))
        # A stale JOB_QUEUED arrives after the job already started.
        reg.apply_event(_event(EventType.JOB_QUEUED, timestamp=100.0, job_id=7, queued_at=100.0))
        job = reg.get_by_job_id("7")
        assert job is not None
        assert job.status is JobStatus.RUNNING  # not demoted
        assert reg.queued() == []

    def test_late_queued_does_not_resurrect_completed(self) -> None:
        reg = JobRegistry()
        reg.apply_event(_event(EventType.JOB_STARTED, timestamp=150.0, job_id=7, started_at=142.0))
        reg.apply_event(_event(EventType.JOB_FINISHED, timestamp=210.0, job_id=7, stopped_at=200.0))
        reg.apply_event(_event(EventType.JOB_QUEUED, timestamp=100.0, job_id=7))
        job = reg.get_by_job_id("7")
        assert job is not None
        assert job.status is JobStatus.COMPLETED  # not resurrected

    def test_finished_without_started_has_no_duration(self) -> None:
        reg = JobRegistry()
        reg.apply_event(_event(EventType.JOB_FINISHED, timestamp=210.0, job_id=7, stopped_at=200.0))
        job = reg.get_by_job_id("7")
        assert job is not None
        assert job.status is JobStatus.COMPLETED
        assert job.start_time is None
        assert job.duration is None
        assert job.queue_wait is None  # no start_time

    def test_local_started_without_started_at_uses_event_timestamp(self) -> None:
        # Regression: a plain (non-remote) JOB_STARTED has no started_at.
        reg = JobRegistry()
        reg.apply_event(_event(EventType.JOB_STARTED, timestamp=150.0, job_id=7, rule_name="align"))
        job = reg.get_by_job_id("7")
        assert job is not None
        assert job.start_time == 150.0  # falls back to event timestamp


class TestDataSourceQueuedRouting:
    """apply_events_to_progress routes queued jobs to queued_jobs_list, not running."""

    def test_queued_job_not_in_running(self, snakemake_dir: object, tmp_path: object) -> None:
        from pathlib import Path

        from snakesee.models import WorkflowProgress
        from snakesee.models import WorkflowStatus
        from snakesee.tui.data_source import WorkflowDataSource

        assert isinstance(tmp_path, Path)
        ds = WorkflowDataSource(workflow_dir=tmp_path, use_estimation=False)
        base = WorkflowProgress(
            workflow_dir=tmp_path, status=WorkflowStatus.RUNNING, total_jobs=2, completed_jobs=0
        )
        events = [
            _event(
                EventType.JOB_QUEUED,
                timestamp=100.0,
                job_id=7,
                rule_name="align",
                executor="aws-batch",
                external_jobid=ARN,
                queued_at=100.0,
            ),
        ]
        result = ds.apply_events_to_progress(base, events)
        assert [j.job_id for j in result.queued_jobs_list] == ["7"]
        assert all(j.job_id != "7" for j in result.running_jobs)
        # The queued JobInfo carries the external id for display.
        assert result.queued_jobs_list[0].external_jobid == ARN

    def test_queued_then_started_appears_running_not_queued(
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
        ds.apply_events_to_progress(
            base, [_event(EventType.JOB_QUEUED, timestamp=100.0, job_id=7, rule_name="align")]
        )
        result = ds.apply_events_to_progress(
            base, [_event(EventType.JOB_STARTED, timestamp=150.0, job_id=7, started_at=142.0)]
        )
        assert result.queued_jobs_list == ()
        assert [j.job_id for j in result.running_jobs] == ["7"]

    def test_running_jobinfo_uses_true_started_at(
        self, snakemake_dir: object, tmp_path: object
    ) -> None:
        # The running JobInfo's start_time must be the executor's started_at, not
        # the event emission time, so elapsed/duration exclude queue wait.
        from pathlib import Path

        from snakesee.models import WorkflowProgress
        from snakesee.models import WorkflowStatus
        from snakesee.tui.data_source import WorkflowDataSource

        assert isinstance(tmp_path, Path)
        ds = WorkflowDataSource(workflow_dir=tmp_path, use_estimation=False)
        base = WorkflowProgress(
            workflow_dir=tmp_path, status=WorkflowStatus.RUNNING, total_jobs=1, completed_jobs=0
        )
        result = ds.apply_events_to_progress(
            base,
            [
                _event(
                    EventType.JOB_STARTED,
                    timestamp=150.0,
                    job_id=7,
                    rule_name="align",
                    started_at=142.0,
                )
            ],
        )
        running = [j for j in result.running_jobs if j.job_id == "7"]
        assert len(running) == 1
        assert running[0].start_time == 142.0


class TestHeaderQueuedCount:
    """The header surfaces the queued count when remote jobs are queued."""

    def test_header_shows_queued_count(self) -> None:
        from io import StringIO
        from pathlib import Path

        from rich.console import Console

        from snakesee.models import JobInfo
        from snakesee.models import WorkflowProgress
        from snakesee.models import WorkflowStatus
        from snakesee.tui.renderables import make_header

        progress = WorkflowProgress(
            workflow_dir=Path("/wf"),
            status=WorkflowStatus.RUNNING,
            total_jobs=3,
            completed_jobs=0,
            queued_jobs_list=[JobInfo(rule="align", job_id="7")],
        )
        panel = make_header(progress, "/wf", paused=False, event_reader=None)
        buf = StringIO()
        Console(file=buf, width=200).print(panel)
        assert "Queued: 1" in buf.getvalue()

    def test_header_omits_queued_when_none(self) -> None:
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
            completed_jobs=0,
        )
        panel = make_header(progress, "/wf", paused=False, event_reader=None)
        buf = StringIO()
        Console(file=buf, width=200).print(panel)
        assert "Queued" not in buf.getvalue()
