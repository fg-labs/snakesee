"""Tests for surfacing remote job info in the TUI (Phase 0 degradation floor)."""

from __future__ import annotations

from pathlib import Path

from snakesee.models import JobInfo
from snakesee.persistence.backend import IncompleteJob
from snakesee.tui.renderables import make_remote_job_info

ARN = "arn:aws:batch:us-east-1:123456789012:job/abc123"


class TestMakeRemoteJobInfo:
    """make_remote_job_info turns a JobInfo into display lines."""

    def test_local_job_has_no_remote_lines(self) -> None:
        """A job with no external id produces no lines at all."""
        job = JobInfo(rule="align", job_id="1")
        assert make_remote_job_info(job) == []

    def test_bare_id_shows_id_only(self) -> None:
        """A bare external id with no region shows the id but no console link."""
        job = JobInfo(rule="align", job_id="1", external_jobid="abc123", executor="aws-batch")
        lines = make_remote_job_info(job)
        assert [line.plain for line in lines] == ["aws-batch job: abc123"]

    def test_arn_yields_console_link(self) -> None:
        """An ARN carries its region, so a console link can be built."""
        job = JobInfo(rule="align", job_id="1", external_jobid=ARN, executor="aws-batch")
        lines = make_remote_job_info(job)
        assert lines[0].plain == f"aws-batch job: {ARN}"
        assert any("console:" in line.plain and "region=us-east-1" in line.plain for line in lines)

    def test_region_and_log_stream_yield_both_links(self) -> None:
        """With region + log stream, both console and CloudWatch links appear."""
        job = JobInfo(
            rule="align",
            job_id="1",
            external_jobid="abc123",
            executor="aws-batch",
            region="eu-west-1",
            log_stream="JobDef/default/abc",
        )
        lines = make_remote_job_info(job)
        assert any("console:" in line.plain for line in lines)
        assert any("logs:" in line.plain and "cloudwatch" in line.plain for line in lines)

    def test_falls_back_to_remote_label_without_executor(self) -> None:
        """When the executor name is unknown, a neutral 'remote' label is used."""
        job = JobInfo(rule="align", job_id="1", external_jobid="abc123")
        assert make_remote_job_info(job)[0].plain == "remote job: abc123"

    def test_shows_queue_and_queue_wait(self) -> None:
        """Queue name and queue wait (start_time - queued_at) are surfaced."""
        job = JobInfo(
            rule="align",
            job_id="1",
            external_jobid="abc123",
            executor="aws-batch",
            queue="graviton-spot",
            queued_at=100.0,
            start_time=142.0,  # 42s queue wait
        )
        lines = make_remote_job_info(job)
        assert any("queue:" in line.plain and "graviton-spot" in line.plain for line in lines)
        assert any("queued for:" in line.plain for line in lines)


class TestIncompleteJobCarriesExternalId:
    """The incomplete->JobInfo wiring preserves external_jobid (parser.core)."""

    def test_external_jobid_preserved(self) -> None:
        """Building a running JobInfo from an IncompleteJob keeps the external id.

        This mirrors the construction in parser.core.parse_workflow_state without
        needing a populated .snakemake DB.
        """
        incomplete = IncompleteJob(
            start_time=100.0,
            output_file=Path("results/sample.bam"),
            rule="align",
            external_jobid=ARN,
        )
        job = JobInfo(
            rule=incomplete.rule or "unknown",
            start_time=incomplete.start_time,
            output_file=incomplete.output_file,
            external_jobid=incomplete.external_jobid,
        )
        assert job.external_jobid == ARN
        # And it round-trips into display lines.
        assert make_remote_job_info(job)[0].plain.endswith(ARN)
