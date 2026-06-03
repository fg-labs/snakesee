"""Tests for the WorkflowDataSource extraction from WorkflowMonitorTUI."""

from pathlib import Path
from unittest.mock import MagicMock

from snakesee.constants import DEFAULT_REFRESH_RATE
from snakesee.tui.data_source import WorkflowDataSource
from snakesee.tui.data_source import _parse_log_start_time


class TestWorkflowDataSourceConstruction:
    """Tests for constructing a WorkflowDataSource and basic attribute setup."""

    def test_default_construction(self, snakemake_dir: Path, tmp_path: Path) -> None:
        """A data source built with defaults exposes the expected attributes."""
        ds = WorkflowDataSource(workflow_dir=tmp_path)
        assert ds.workflow_dir == tmp_path
        assert ds.use_estimation is True
        assert ds.refresh_rate == DEFAULT_REFRESH_RATE
        assert ds._use_wildcard_conditioning is True
        # Empty log dir → no available logs but the registry is initialized.
        assert ds._available_logs == []
        assert ds._workflow_state is not None

    def test_custom_construction(self, snakemake_dir: Path, tmp_path: Path) -> None:
        """Non-default constructor arguments are stored on the data source."""
        ds = WorkflowDataSource(
            workflow_dir=tmp_path,
            refresh_rate=5.0,
            use_estimation=False,
            use_wildcard_conditioning=False,
            half_life_logs=20,
            half_life_days=14.0,
        )
        assert ds.refresh_rate == 5.0
        assert ds.use_estimation is False
        assert ds._use_wildcard_conditioning is False
        assert ds.half_life_logs == 20
        assert ds.half_life_days == 14.0
        # use_estimation=False short-circuits estimator construction.
        assert ds._estimator is None


class TestWorkflowDataSourcePolling:
    """Tests for the high-level polling API."""

    def test_poll_state_returns_progress_and_estimate(
        self, snakemake_dir: Path, tmp_path: Path
    ) -> None:
        """poll_state always returns a (progress, estimate) tuple."""
        ds = WorkflowDataSource(workflow_dir=tmp_path)
        progress, _ = ds.poll_state()
        assert progress is not None

    def test_poll_state_is_idempotent(self, snakemake_dir: Path, tmp_path: Path) -> None:
        """Polling twice on an empty workflow does not crash and returns stable shape."""
        ds = WorkflowDataSource(workflow_dir=tmp_path)
        progress_1, _ = ds.poll_state()
        progress_2, _ = ds.poll_state()
        assert progress_1.workflow_dir == progress_2.workflow_dir


class TestWorkflowDataSourceFilterAndCutoff:
    """Tests for the lightweight helper APIs."""

    def test_filter_jobs_no_filter_returns_input(self, snakemake_dir: Path, tmp_path: Path) -> None:
        """Empty filter text returns the input list unchanged."""
        from snakesee.models import JobInfo

        ds = WorkflowDataSource(workflow_dir=tmp_path)
        jobs = [JobInfo(rule="align"), JobInfo(rule="sort")]
        assert ds.filter_jobs(jobs, None) == jobs
        assert ds.filter_jobs(jobs, "") == jobs

    def test_filter_jobs_substring_match(self, snakemake_dir: Path, tmp_path: Path) -> None:
        """Filter text matches case-insensitively against rule names."""
        from snakesee.models import JobInfo

        ds = WorkflowDataSource(workflow_dir=tmp_path)
        jobs = [
            JobInfo(rule="align_reads"),
            JobInfo(rule="sort_bam"),
            JobInfo(rule="ALIGN_contigs"),
        ]
        filtered = ds.filter_jobs(jobs, "align")
        assert len(filtered) == 2
        assert all("align" in j.rule.lower() for j in filtered)

    def test_get_cutoff_time_latest_log_returns_none(
        self, snakemake_dir: Path, tmp_path: Path
    ) -> None:
        """When viewing the latest log, the cutoff time is None."""
        ds = WorkflowDataSource(workflow_dir=tmp_path)
        ds._current_log_index = 0
        assert ds.get_cutoff_time() is None


class TestJobFinishedEventHandling:
    """Regression tests for ``_handle_job_finished_event``."""

    def test_finished_event_drops_job_from_running(
        self, snakemake_dir: Path, tmp_path: Path
    ) -> None:
        """JOB_FINISHED removes the job from the running list immediately."""
        from snakesee.events import EventType
        from snakesee.events import SnakeseeEvent
        from snakesee.models import JobInfo

        ds = WorkflowDataSource(workflow_dir=tmp_path, use_estimation=False)
        running = [
            JobInfo(rule="align", job_id="42", start_time=900.0),
            JobInfo(rule="sort", job_id="43", start_time=950.0),
        ]
        completions: list[JobInfo] = []
        event = SnakeseeEvent(
            event_type=EventType.JOB_FINISHED,
            timestamp=1000.0,
            job_id=42,
            rule_name="align",
            duration=100.0,
            threads=2,
        )
        ds._handle_job_finished_event(event, running, completions)
        assert [j.job_id for j in running] == ["43"]

    def test_finished_event_appends_completion_when_absent(
        self, snakemake_dir: Path, tmp_path: Path
    ) -> None:
        """If the completion isn't already tracked, JOB_FINISHED appends a fresh entry."""
        from snakesee.events import EventType
        from snakesee.events import SnakeseeEvent
        from snakesee.models import JobInfo

        ds = WorkflowDataSource(workflow_dir=tmp_path, use_estimation=False)
        running = [JobInfo(rule="align", job_id="42", start_time=900.0)]
        completions: list[JobInfo] = []
        event = SnakeseeEvent(
            event_type=EventType.JOB_FINISHED,
            timestamp=1000.0,
            job_id=42,
            rule_name="align",
            duration=100.0,
            threads=4,
        )
        ds._handle_job_finished_event(event, running, completions)
        assert len(completions) == 1
        assert completions[0].job_id == "42"
        assert completions[0].rule == "align"
        assert completions[0].end_time == 1000.0
        assert completions[0].start_time == 900.0
        assert completions[0].threads == 4

    def test_finished_event_patches_existing_completion(
        self, snakemake_dir: Path, tmp_path: Path
    ) -> None:
        """If a completion row already exists, the event patches it (no duplicate)."""
        from snakesee.events import EventType
        from snakesee.events import SnakeseeEvent
        from snakesee.models import JobInfo

        ds = WorkflowDataSource(workflow_dir=tmp_path, use_estimation=False)
        running: list[JobInfo] = []
        completions = [
            JobInfo(rule="align", job_id="42", start_time=850.0, end_time=990.0),
        ]
        event = SnakeseeEvent(
            event_type=EventType.JOB_FINISHED,
            timestamp=1000.0,
            job_id=42,
            rule_name="align",
            duration=100.0,
            threads=4,
        )
        ds._handle_job_finished_event(event, running, completions)
        assert len(completions) == 1
        assert completions[0].end_time == 1000.0
        assert completions[0].start_time == 900.0  # event.timestamp - duration
        assert completions[0].threads == 4


class TestJobStartedEventHandling:
    """Regression tests for ``_handle_job_started_event``."""

    def test_started_event_patches_existing_running_job(
        self, snakemake_dir: Path, tmp_path: Path
    ) -> None:
        """If the job is already in the running list, the event patches it in place."""
        from snakesee.events import EventType
        from snakesee.events import SnakeseeEvent
        from snakesee.models import JobInfo

        ds = WorkflowDataSource(workflow_dir=tmp_path, use_estimation=False)
        running = [JobInfo(rule="align", job_id="42")]
        event = SnakeseeEvent(
            event_type=EventType.JOB_STARTED,
            timestamp=1000.0,
            job_id=42,
            rule_name="align",
            threads=4,
        )
        ds._handle_job_started_event(event, running)
        assert len(running) == 1
        assert running[0].job_id == "42"
        assert running[0].start_time == 1000.0
        assert running[0].threads == 4

    def test_started_event_appends_running_job_when_absent(
        self, snakemake_dir: Path, tmp_path: Path
    ) -> None:
        """If the job isn't in the running list yet, JOB_STARTED appends a fresh entry.

        Otherwise a JOB_STARTED arriving before the next log parse would never
        surface in the live running list until a later re-parse caught up.
        """
        from snakesee.events import EventType
        from snakesee.events import SnakeseeEvent
        from snakesee.models import JobInfo

        ds = WorkflowDataSource(workflow_dir=tmp_path, use_estimation=False)
        running: list[JobInfo] = []
        event = SnakeseeEvent(
            event_type=EventType.JOB_STARTED,
            timestamp=1000.0,
            job_id=42,
            rule_name="align",
            threads=4,
        )
        ds._handle_job_started_event(event, running)
        assert len(running) == 1
        assert running[0].job_id == "42"
        assert running[0].rule == "align"
        assert running[0].start_time == 1000.0
        assert running[0].threads == 4


class TestParseLogStartTime:
    """Tests for ``_parse_log_start_time`` (workflow-start reference for events)."""

    def test_returns_first_timestamp(self, tmp_path: Path) -> None:
        """Returns the first log timestamp, not a later one."""
        from snakesee.parser.utils import _parse_timestamp

        log = tmp_path / "run.log"
        log.write_text(
            "Building DAG of jobs...\n"
            "[Mon Dec 15 22:34:30 2025]\n"
            "rule align:\n"
            "[Mon Dec 15 22:35:00 2025]\n"
        )
        assert _parse_log_start_time(log) == _parse_timestamp("Mon Dec 15 22:34:30 2025")

    def test_returns_first_timestamp_when_indented(self, tmp_path: Path) -> None:
        """Indented timestamp lines (group/pipe job blocks) are still recognized."""
        from snakesee.parser.utils import _parse_timestamp

        log = tmp_path / "run.log"
        log.write_text("    [Mon Dec 15 22:34:30 2025]\n")
        assert _parse_log_start_time(log) == _parse_timestamp("Mon Dec 15 22:34:30 2025")

    def test_returns_none_without_timestamp(self, tmp_path: Path) -> None:
        """A log with no recognizable timestamp yields None."""
        log = tmp_path / "run.log"
        log.write_text("Building DAG of jobs...\nno timestamps here\n")
        assert _parse_log_start_time(log) is None

    def test_returns_none_for_missing_file(self, tmp_path: Path) -> None:
        """A missing log file yields None rather than raising."""
        assert _parse_log_start_time(tmp_path / "missing.log") is None


class TestInitEstimatorProgressMode:
    """Tests that estimator init renders progress only when asked to."""

    def _workflow_with_log(self, tmp_path: Path) -> Path:
        """Create a workflow dir with a log so the estimator load path runs.

        The log must match ``*.snakemake.log`` to be discovered by WorkflowPaths.
        """
        log_dir = tmp_path / ".snakemake" / "log"
        log_dir.mkdir(parents=True)
        (log_dir / "2025-12-15T223430.snakemake.log").write_text("[Mon Dec 15 22:34:30 2025]\n")
        return tmp_path

    def test_silent_mode_does_not_render(self, tmp_path: Path) -> None:
        """show_progress=False loads without instantiating a Rich Progress."""
        from unittest.mock import patch

        workflow_dir = self._workflow_with_log(tmp_path)
        ds = WorkflowDataSource(workflow_dir=workflow_dir, use_estimation=True)
        with patch("rich.progress.Progress") as mock_progress:
            ds.init_estimator(show_progress=False)
        mock_progress.assert_not_called()
        assert ds._estimator is not None

    def test_startup_mode_renders(self, tmp_path: Path) -> None:
        """show_progress=True renders a Rich Progress while loading."""
        from unittest.mock import patch

        workflow_dir = self._workflow_with_log(tmp_path)
        ds = WorkflowDataSource(workflow_dir=workflow_dir, use_estimation=True)
        with patch("rich.progress.Progress") as mock_progress:
            ds.init_estimator(show_progress=True)
        mock_progress.assert_called_once()


class TestTotalJobsInference:
    """Pending counts must be correct before the first job completes.

    The Snakemake progress line that carries ``total_jobs`` only appears after the
    first completion, so until then ``poll_state`` infers the total from the Job
    stats table's expected job counts (see ``#64``).
    """

    def _mock_estimator(self, expected_job_counts: dict[str, int] | None) -> MagicMock:
        estimator = MagicMock()
        estimator.expected_job_counts = expected_job_counts
        estimator.estimate_remaining.return_value = None
        return estimator

    def test_total_jobs_inferred_from_expected_counts(
        self, snakemake_dir: Path, tmp_path: Path
    ) -> None:
        """total_jobs is inferred from expected_job_counts when no progress line exists."""
        from unittest.mock import patch

        from tests.conftest import make_workflow_progress

        ds = WorkflowDataSource(workflow_dir=tmp_path)
        ds._estimator = self._mock_estimator({"align": 5, "sort": 3})
        progress = make_workflow_progress(total_jobs=0, completed_jobs=0, running_jobs=[])

        with patch.object(ds, "read_new_events", return_value=[]):
            with patch("snakesee.tui.data_source.parse_workflow_state", return_value=progress):
                result, _ = ds.poll_state()

        assert result.total_jobs == 8  # 5 + 3
        assert result.pending_jobs == 8  # all pending; none running or completed

    def test_pending_count_accounts_for_running_jobs(
        self, snakemake_dir: Path, tmp_path: Path
    ) -> None:
        """pending_jobs reflects running jobs even before the first completion."""
        from unittest.mock import patch

        from tests.conftest import make_job_info
        from tests.conftest import make_workflow_progress

        ds = WorkflowDataSource(workflow_dir=tmp_path)
        ds._estimator = self._mock_estimator({"align": 5, "sort": 3})
        running = [make_job_info(rule="align", job_id="1"), make_job_info(rule="align", job_id="2")]
        progress = make_workflow_progress(total_jobs=0, completed_jobs=0, running_jobs=running)

        with patch.object(ds, "read_new_events", return_value=[]):
            with patch("snakesee.tui.data_source.parse_workflow_state", return_value=progress):
                result, _ = ds.poll_state()

        assert result.total_jobs == 8
        assert result.pending_jobs == 6  # 8 - 2 running

    def test_no_inference_without_estimator(self, snakemake_dir: Path, tmp_path: Path) -> None:
        """total_jobs stays 0 when there is no estimator to infer from."""
        from unittest.mock import patch

        from tests.conftest import make_workflow_progress

        ds = WorkflowDataSource(workflow_dir=tmp_path, use_estimation=False)
        assert ds._estimator is None
        progress = make_workflow_progress(total_jobs=0, completed_jobs=0, running_jobs=[])

        with patch.object(ds, "read_new_events", return_value=[]):
            with patch("snakesee.tui.data_source.parse_workflow_state", return_value=progress):
                result, _ = ds.poll_state()

        assert result.total_jobs == 0

    def test_no_override_when_progress_line_exists(
        self, snakemake_dir: Path, tmp_path: Path
    ) -> None:
        """A real total_jobs from the progress line is never overridden by inference."""
        from unittest.mock import patch

        from tests.conftest import make_workflow_progress

        ds = WorkflowDataSource(workflow_dir=tmp_path)
        ds._estimator = self._mock_estimator({"align": 5, "sort": 3})
        progress = make_workflow_progress(total_jobs=10, completed_jobs=1, running_jobs=[])

        with patch.object(ds, "read_new_events", return_value=[]):
            with patch("snakesee.tui.data_source.parse_workflow_state", return_value=progress):
                result, _ = ds.poll_state()

        assert result.total_jobs == 10  # progress line wins; not overwritten with 8

    def test_no_inference_in_historical_view(self, snakemake_dir: Path, tmp_path: Path) -> None:
        """No inference while browsing a historical run (log index > 0).

        expected_job_counts is parsed from the latest log, so grafting it onto an
        older run's progress would show the wrong total.
        """
        from unittest.mock import patch

        from tests.conftest import make_workflow_progress

        ds = WorkflowDataSource(workflow_dir=tmp_path)
        ds._estimator = self._mock_estimator({"align": 5, "sort": 3})
        ds._current_log_index = 1  # browsing a historical run, not the live one
        progress = make_workflow_progress(total_jobs=0, completed_jobs=0, running_jobs=[])

        with patch.object(ds, "read_new_events", return_value=[]):
            with patch("snakesee.tui.data_source.parse_workflow_state", return_value=progress):
                result, _ = ds.poll_state()

        assert result.total_jobs == 0
