"""Tests for the TUI module."""

from pathlib import Path
from unittest.mock import MagicMock
from unittest.mock import patch

import pytest

from snakesee.events import EventType
from snakesee.models import JobInfo
from snakesee.models import TimeEstimate
from snakesee.models import WorkflowProgress
from snakesee.models import WorkflowStatus
from snakesee.tui import DEFAULT_REFRESH_RATE
from snakesee.tui import FG_BLUE
from snakesee.tui import FG_GREEN
from snakesee.tui import MAX_REFRESH_RATE
from snakesee.tui import MIN_REFRESH_RATE
from snakesee.tui import LayoutMode
from snakesee.tui import WorkflowMonitorTUI
from snakesee.tui.monitor import _is_event_file_current
from tests.conftest import make_job_info
from tests.conftest import make_snakesee_event
from tests.conftest import make_time_estimate
from tests.conftest import make_workflow_progress


class TestLayoutMode:
    """Tests for LayoutMode enum."""

    def test_layout_modes_exist(self) -> None:
        """Test that all layout modes are defined."""
        assert LayoutMode.FULL.value == "full"
        assert LayoutMode.COMPACT.value == "compact"
        assert LayoutMode.MINIMAL.value == "minimal"


class TestBrandingColors:
    """Tests for branding colors."""

    def test_fg_blue_defined(self) -> None:
        """Test FG_BLUE color is defined."""
        assert FG_BLUE == "#26a8e0"

    def test_fg_green_defined(self) -> None:
        """Test FG_GREEN color is defined."""
        assert FG_GREEN == "#38b44a"


class TestWorkflowMonitorTUI:
    """Tests for WorkflowMonitorTUI class."""

    @pytest.fixture
    def mock_console(self) -> MagicMock:
        """Create a mock console."""
        console = MagicMock()
        console.width = 120
        console.height = 40
        console.is_terminal = True
        return console

    @pytest.fixture
    def tui(
        self, snakemake_dir: Path, tmp_path: Path, mock_console: MagicMock
    ) -> WorkflowMonitorTUI:
        """Create a TUI instance for testing."""
        with patch("snakesee.tui.monitor.Console", return_value=mock_console):
            return WorkflowMonitorTUI(
                workflow_dir=tmp_path,
                refresh_rate=2.0,
                use_estimation=True,
            )

    def test_init_default_values(self, snakemake_dir: Path, tmp_path: Path) -> None:
        """Test TUI initialization with default values."""
        tui = WorkflowMonitorTUI(workflow_dir=tmp_path)
        assert tui.workflow_dir == tmp_path
        assert tui.refresh_rate == DEFAULT_REFRESH_RATE
        assert tui.use_estimation is True

    def test_init_custom_values(self, snakemake_dir: Path, tmp_path: Path) -> None:
        """Test TUI initialization with custom values."""
        tui = WorkflowMonitorTUI(
            workflow_dir=tmp_path,
            refresh_rate=5.0,
            use_estimation=False,
        )
        assert tui.refresh_rate == 5.0
        assert tui.use_estimation is False

    def test_handle_key_quit(self, tui: WorkflowMonitorTUI) -> None:
        """Test quit key handler."""
        assert tui._handle_key("q") is True
        assert tui._handle_key("Q") is True

    def test_handle_key_toggle_pause(self, tui: WorkflowMonitorTUI) -> None:
        """Test pause toggle key handler."""
        assert not tui._paused
        tui._handle_key("p")
        assert tui._paused
        tui._handle_key("p")  # type: ignore[unreachable]
        assert not tui._paused

    def test_handle_key_toggle_estimation(self, tui: WorkflowMonitorTUI) -> None:
        """Test estimation toggle key handler."""
        initial = tui.use_estimation
        tui._handle_key("e")
        assert tui.use_estimation != initial

    def test_handle_key_toggle_wildcard_conditioning(self, tui: WorkflowMonitorTUI) -> None:
        """Test wildcard conditioning toggle key handler."""
        assert tui._use_wildcard_conditioning is True  # Now enabled by default
        tui._handle_key("w")
        assert tui._use_wildcard_conditioning is False

    def test_handle_key_toggle_help(self, tui: WorkflowMonitorTUI) -> None:
        """Test help toggle key handler."""
        assert tui._show_help is False
        tui._handle_key("?")
        assert tui._show_help is True

    def test_handle_key_refresh_rate_decrease(self, tui: WorkflowMonitorTUI) -> None:
        """Test refresh rate decrease keys (- for fine, < for coarse)."""
        initial = tui.refresh_rate
        tui._handle_key("-")
        assert tui.refresh_rate == max(MIN_REFRESH_RATE, initial - 0.5)

    def test_handle_key_refresh_rate_increase(self, tui: WorkflowMonitorTUI) -> None:
        """Test refresh rate increase keys (+ for fine, > for coarse)."""
        initial = tui.refresh_rate
        tui._handle_key("+")
        assert tui.refresh_rate == min(MAX_REFRESH_RATE, initial + 0.5)

    def test_handle_key_refresh_rate_reset(self, tui: WorkflowMonitorTUI) -> None:
        """Test refresh rate reset key."""
        tui.refresh_rate = 10.0
        tui._handle_key("0")
        assert tui.refresh_rate == DEFAULT_REFRESH_RATE

    def test_handle_key_refresh_rate_coarse(self, tui: WorkflowMonitorTUI) -> None:
        """Test coarse refresh rate adjustment with < and >."""
        tui.refresh_rate = 10.0
        tui._handle_key("<")
        assert tui.refresh_rate == 5.0  # -5s
        tui._handle_key(">")
        assert tui.refresh_rate == 10.0  # +5s

    def test_handle_key_layout_cycle(self, tui: WorkflowMonitorTUI) -> None:
        """Test layout cycle key."""
        assert tui._layout_mode == LayoutMode.FULL
        tui._handle_key("\t")
        assert tui._layout_mode == LayoutMode.COMPACT
        tui._handle_key("\t")  # type: ignore[unreachable]
        assert tui._layout_mode == LayoutMode.MINIMAL
        tui._handle_key("\t")
        assert tui._layout_mode == LayoutMode.FULL

    def test_handle_key_filter_mode(self, tui: WorkflowMonitorTUI) -> None:
        """Test filter mode key."""
        assert tui._filter_mode is False
        tui._handle_key("/")
        assert tui._filter_mode is True

    def test_handle_filter_key_escape(self, tui: WorkflowMonitorTUI) -> None:
        """Test escape in filter mode."""
        tui._filter_mode = True
        tui._filter_input = "test"
        tui._handle_filter_key("\x1b")
        assert tui._filter_mode is False
        assert tui._filter_input == ""

    def test_handle_filter_key_enter(self, tui: WorkflowMonitorTUI) -> None:
        """Test enter in filter mode."""
        tui._filter_mode = True
        tui._filter_input = "align"
        tui._handle_filter_key("\r")
        assert tui._filter_mode is False
        assert tui._filter_text == "align"

    def test_handle_filter_key_typing(self, tui: WorkflowMonitorTUI) -> None:
        """Test typing in filter mode."""
        tui._filter_mode = True
        tui._filter_input = ""
        tui._handle_filter_key("a")
        assert tui._filter_input == "a"
        tui._handle_filter_key("b")
        assert tui._filter_input == "ab"

    def test_handle_filter_key_backspace(self, tui: WorkflowMonitorTUI) -> None:
        """Test backspace in filter mode."""
        tui._filter_mode = True
        tui._filter_input = "abc"
        tui._handle_filter_key("\x7f")
        assert tui._filter_input == "ab"

    def test_filter_jobs(self, tui: WorkflowMonitorTUI) -> None:
        """Test job filtering."""
        jobs = [
            JobInfo(rule="align_reads"),
            JobInfo(rule="sort_bam"),
            JobInfo(rule="align_contigs"),
        ]
        tui._filter_text = "align"
        filtered = tui._filter_jobs(jobs)
        assert len(filtered) == 2
        assert all("align" in j.rule for j in filtered)

    def test_filter_jobs_no_filter(self, tui: WorkflowMonitorTUI) -> None:
        """Test job filtering with no filter."""
        jobs = [JobInfo(rule="align"), JobInfo(rule="sort")]
        tui._filter_text = None
        filtered = tui._filter_jobs(jobs)
        assert len(filtered) == 2

    def test_make_progress_bar(self, tui: WorkflowMonitorTUI) -> None:
        """Test progress bar creation."""
        progress = WorkflowProgress(
            workflow_dir=Path("."),
            status=WorkflowStatus.RUNNING,
            total_jobs=10,
            completed_jobs=5,
            failed_jobs=1,
        )
        bar = tui._make_progress_bar(progress, width=20)
        assert len(bar.plain) == 20

    def test_make_progress_panel(self, tui: WorkflowMonitorTUI) -> None:
        """Test progress panel creation."""
        progress = WorkflowProgress(
            workflow_dir=Path("."),
            status=WorkflowStatus.RUNNING,
            total_jobs=10,
            completed_jobs=5,
        )
        estimate = TimeEstimate(
            seconds_remaining=300,
            lower_bound=200,
            upper_bound=400,
            confidence=0.8,
            method="weighted",
        )
        panel = tui._make_progress_panel(progress, estimate)
        assert panel.title == "Progress"

    def test_make_running_table(self, tui: WorkflowMonitorTUI) -> None:
        """Test running jobs table creation."""
        progress = WorkflowProgress(
            workflow_dir=Path("."),
            status=WorkflowStatus.RUNNING,
            total_jobs=10,
            completed_jobs=5,
            running_jobs=[JobInfo(rule="align", job_id="1")],
        )
        panel = tui._make_running_table(progress)
        assert "Running" in panel.title

    def test_make_completions_table(self, tui: WorkflowMonitorTUI) -> None:
        """Test completions table creation."""
        progress = WorkflowProgress(
            workflow_dir=Path("."),
            status=WorkflowStatus.RUNNING,
            total_jobs=10,
            completed_jobs=5,
            recent_completions=[
                JobInfo(rule="align", start_time=100.0, end_time=200.0),
            ],
        )
        panel = tui._make_completions_table(progress)
        assert panel.title == "Recent Completions"

    def test_get_completions_sorted_uses_heap_for_limit(self, tui: WorkflowMonitorTUI) -> None:
        """Test that _get_completions_sorted with limit returns top-N by end_time."""
        jobs = [
            JobInfo(rule=f"rule_{i}", start_time=float(i), end_time=float(i * 10))
            for i in range(20)
        ]
        progress = make_workflow_progress(recent_completions=jobs)

        result, failed_ids = tui._get_completions_sorted(progress, limit=5)

        assert len(result) == 5
        assert len(failed_ids) == 0
        # Default sort is by end_time descending — top 5 should be the last 5 jobs
        expected_rules = [f"rule_{i}" for i in range(19, 14, -1)]
        assert [j.rule for j in result] == expected_rules

    def test_get_completions_sorted_full_list_without_limit(self, tui: WorkflowMonitorTUI) -> None:
        """Test that _get_completions_sorted without limit returns all jobs sorted."""
        jobs = [
            JobInfo(rule=f"rule_{i}", start_time=float(i), end_time=float(i * 10))
            for i in range(20)
        ]
        progress = make_workflow_progress(recent_completions=jobs)

        result, _ = tui._get_completions_sorted(progress)

        assert len(result) == 20
        # All jobs sorted by end_time descending
        assert result[0].rule == "rule_19"
        assert result[-1].rule == "rule_0"

    def test_get_completions_sorted_custom_sort_column(self, tui: WorkflowMonitorTUI) -> None:
        """Test heap selection with a custom sort column (rule name ascending)."""
        jobs = [
            JobInfo(rule="zebra", start_time=1.0, end_time=10.0),
            JobInfo(rule="alpha", start_time=2.0, end_time=20.0),
            JobInfo(rule="middle", start_time=3.0, end_time=30.0),
        ]
        progress = make_workflow_progress(recent_completions=jobs)

        tui._sort_table = "completions"
        tui._sort_column = 0  # rule name
        tui._sort_ascending = True

        result, _ = tui._get_completions_sorted(progress, limit=2)

        assert len(result) == 2
        assert result[0].rule == "alpha"
        assert result[1].rule == "middle"

    def test_get_completions_sorted_includes_failed_jobs(self, tui: WorkflowMonitorTUI) -> None:
        """Test that failed jobs are merged and their IDs tracked."""
        completed = [JobInfo(rule="align", start_time=1.0, end_time=10.0)]
        failed = [JobInfo(rule="sort", start_time=2.0, end_time=20.0)]
        progress = make_workflow_progress(recent_completions=completed, failed_jobs_list=failed)

        result, failed_ids = tui._get_completions_sorted(progress)

        assert len(result) == 2
        assert id(failed[0]) in failed_ids
        assert id(completed[0]) not in failed_ids

    def test_get_completions_sorted_with_filter(self, tui: WorkflowMonitorTUI) -> None:
        """Test that filtering is applied before heap selection."""
        jobs = [
            JobInfo(rule="align_reads", start_time=1.0, end_time=10.0),
            JobInfo(rule="sort_bam", start_time=2.0, end_time=20.0),
            JobInfo(rule="align_index", start_time=3.0, end_time=30.0),
        ]
        progress = make_workflow_progress(recent_completions=jobs)

        tui._filter_text = "align"
        result, _ = tui._get_completions_sorted(progress, limit=5)

        assert len(result) == 2
        assert all("align" in j.rule for j in result)

    def test_get_completions_sorted_empty(self, tui: WorkflowMonitorTUI) -> None:
        """Test with no completions or failures."""
        progress = make_workflow_progress()

        result, failed_ids = tui._get_completions_sorted(progress, limit=8)

        assert result == []
        assert failed_ids == set()

    def test_get_completions_sorted_fewer_than_limit(self, tui: WorkflowMonitorTUI) -> None:
        """Test when there are fewer jobs than the limit."""
        jobs = [
            JobInfo(rule="align", start_time=1.0, end_time=10.0),
            JobInfo(rule="sort", start_time=2.0, end_time=20.0),
        ]
        progress = make_workflow_progress(recent_completions=jobs)

        result, _ = tui._get_completions_sorted(progress, limit=8)

        # Falls back to full sort when len(jobs) <= limit
        assert len(result) == 2
        assert result[0].rule == "sort"  # end_time=20, descending
        assert result[1].rule == "align"

    def test_make_failed_jobs_panel_empty(self, tui: WorkflowMonitorTUI) -> None:
        """Test failed jobs panel with no failures."""
        progress = WorkflowProgress(
            workflow_dir=Path("."),
            status=WorkflowStatus.RUNNING,
            total_jobs=10,
            completed_jobs=5,
        )
        panel = tui._make_failed_jobs_panel(progress)
        assert "Failed" in panel.title

    def test_make_failed_jobs_panel_with_failures(self, tui: WorkflowMonitorTUI) -> None:
        """Test failed jobs panel with failures."""
        progress = WorkflowProgress(
            workflow_dir=Path("."),
            status=WorkflowStatus.FAILED,
            total_jobs=10,
            completed_jobs=5,
            failed_jobs=2,
            failed_jobs_list=[
                JobInfo(rule="align", job_id="1"),
                JobInfo(rule="sort", job_id="2"),
            ],
        )
        panel = tui._make_failed_jobs_panel(progress)
        assert "(2)" in panel.title

    def test_make_summary_footer(self, tui: WorkflowMonitorTUI) -> None:
        """Test summary footer creation."""
        progress = WorkflowProgress(
            workflow_dir=Path("."),
            status=WorkflowStatus.RUNNING,
            total_jobs=20,
            completed_jobs=10,
            failed_jobs=2,
            running_jobs=[JobInfo(rule="test")] * 3,
        )
        panel = tui._make_summary_footer(progress)
        assert panel is not None

    def test_sort_indicator_active(self, tui: WorkflowMonitorTUI) -> None:
        """Test sort indicator when sorting is active."""
        tui._sort_table = "running"
        tui._sort_column = 0
        tui._sort_ascending = True
        indicator = tui._sort_indicator("running", 0)
        assert "▲" in indicator

    def test_sort_indicator_descending(self, tui: WorkflowMonitorTUI) -> None:
        """Test sort indicator for descending sort."""
        tui._sort_table = "running"
        tui._sort_column = 0
        tui._sort_ascending = False
        indicator = tui._sort_indicator("running", 0)
        assert "▼" in indicator

    def test_sort_indicator_inactive(self, tui: WorkflowMonitorTUI) -> None:
        """Test sort indicator when not sorting this table."""
        tui._sort_table = "stats"
        indicator = tui._sort_indicator("running", 0)
        assert indicator == ""

    def test_handle_sort_key_cycle(self, tui: WorkflowMonitorTUI) -> None:
        """Test sort table cycling forward with 's'."""
        assert tui._sort_table is None
        tui._handle_key("s")
        assert tui._sort_table == "running"
        tui._handle_key("s")
        assert tui._sort_table == "completions"
        tui._handle_key("s")
        assert tui._sort_table == "pending"
        tui._handle_key("s")
        assert tui._sort_table == "stats"
        tui._handle_key("s")
        assert tui._sort_table is None

    def test_handle_sort_key_cycle_backward(self, tui: WorkflowMonitorTUI) -> None:
        """Test sort table cycling backward with 'S' (shift+s)."""
        assert tui._sort_table is None
        tui._handle_key("S")
        assert tui._sort_table == "stats"
        tui._handle_key("S")
        assert tui._sort_table == "pending"
        tui._handle_key("S")
        assert tui._sort_table == "completions"
        tui._handle_key("S")
        assert tui._sort_table == "running"
        tui._handle_key("S")
        assert tui._sort_table is None

    def test_handle_log_navigation_older(self, tui: WorkflowMonitorTUI) -> None:
        """Test log navigation to older log."""
        tui._available_logs = [Path("log1"), Path("log2"), Path("log3")]
        tui._current_log_index = 0
        with patch.object(tui, "_refresh_log_list"):
            tui._handle_key("[")
        assert tui._current_log_index == 1

    def test_handle_log_navigation_newer(self, tui: WorkflowMonitorTUI) -> None:
        """Test log navigation to newer log."""
        tui._available_logs = [Path("log1"), Path("log2"), Path("log3")]
        tui._current_log_index = 2
        tui._handle_key("]")
        assert tui._current_log_index == 1


class TestEventHandling:
    """Tests for event processing methods."""

    def test_apply_events_empty_list(self, tui_with_mocks: WorkflowMonitorTUI) -> None:
        """Test applying empty event list returns unchanged progress."""
        progress = make_workflow_progress()
        result = tui_with_mocks._apply_events_to_progress(progress, [])
        assert result.completed_jobs == progress.completed_jobs
        assert result.total_jobs == progress.total_jobs

    def test_apply_events_progress_event(self, tui_with_mocks: WorkflowMonitorTUI) -> None:
        """Test that progress events update total and completed jobs."""
        progress = make_workflow_progress(total_jobs=100, completed_jobs=50)
        events = [make_snakesee_event(EventType.PROGRESS, total_jobs=100, completed_jobs=60)]
        result = tui_with_mocks._apply_events_to_progress(progress, events)
        assert result.completed_jobs == 60

    def test_apply_events_job_error(self, tui_with_mocks: WorkflowMonitorTUI) -> None:
        """Test that job error events update failed count."""
        progress = make_workflow_progress(failed_jobs=0, failed_jobs_list=[])
        events = [make_snakesee_event(EventType.JOB_ERROR, rule_name="align", job_id=123)]
        result = tui_with_mocks._apply_events_to_progress(progress, events)
        assert result.failed_jobs == 1
        assert len(result.failed_jobs_list) == 1

    def test_handle_job_submitted_tracks_threads(self, tui_with_mocks: WorkflowMonitorTUI) -> None:
        """Test that submitted events track thread info in JobRegistry."""
        event = make_snakesee_event(
            EventType.JOB_SUBMITTED, rule_name="align", job_id=123, threads=8
        )
        # In production, apply_event is called first to create the job
        tui_with_mocks._workflow_state.jobs.apply_event(event)
        # Pass empty running_jobs list as required by the method
        running_jobs: list[JobInfo] = []
        tui_with_mocks._handle_job_submitted_event(event, running_jobs)
        # Threads should be stored in JobRegistry
        job = tui_with_mocks._workflow_state.jobs.get_by_job_id("123")
        assert job is not None
        assert job.threads == 8


class TestPanelCreation:
    """Tests for panel creation methods."""

    def test_make_header_running(self, tui_with_mocks: WorkflowMonitorTUI) -> None:
        """Test header shows RUNNING status."""
        progress = make_workflow_progress(status=WorkflowStatus.RUNNING)
        panel = tui_with_mocks._make_header(progress)
        # Panel should contain RUNNING text
        assert panel is not None

    def test_make_header_completed(self, tui_with_mocks: WorkflowMonitorTUI) -> None:
        """Test header shows COMPLETED status."""
        progress = make_workflow_progress(status=WorkflowStatus.COMPLETED)
        panel = tui_with_mocks._make_header(progress)
        assert panel is not None

    def test_make_header_failed(self, tui_with_mocks: WorkflowMonitorTUI) -> None:
        """Test header shows FAILED status."""
        progress = make_workflow_progress(status=WorkflowStatus.FAILED, failed_jobs=1)
        panel = tui_with_mocks._make_header(progress)
        assert panel is not None

    def test_make_header_incomplete(self, tui_with_mocks: WorkflowMonitorTUI) -> None:
        """Test header shows INCOMPLETE status."""
        progress = make_workflow_progress(
            status=WorkflowStatus.INCOMPLETE,
            incomplete_jobs_list=[make_job_info(rule="interrupted")],
        )
        panel = tui_with_mocks._make_header(progress)
        assert panel is not None

    def test_make_progress_bar_all_succeeded(self, tui_with_mocks: WorkflowMonitorTUI) -> None:
        """Test progress bar with all jobs succeeded."""
        progress = make_workflow_progress(total_jobs=100, completed_jobs=100, failed_jobs=0)
        bar = tui_with_mocks._make_progress_bar(progress, width=40)
        assert len(bar.plain) == 40

    def test_make_progress_bar_with_failures(self, tui_with_mocks: WorkflowMonitorTUI) -> None:
        """Test progress bar shows failures in different color."""
        progress = make_workflow_progress(total_jobs=100, completed_jobs=80, failed_jobs=10)
        bar = tui_with_mocks._make_progress_bar(progress, width=40)
        assert len(bar.plain) == 40

    def test_make_progress_panel_with_estimate(self, tui_with_mocks: WorkflowMonitorTUI) -> None:
        """Test progress panel includes ETA when estimate provided."""
        progress = make_workflow_progress()
        estimate = make_time_estimate(seconds_remaining=600)
        panel = tui_with_mocks._make_progress_panel(progress, estimate)
        assert panel.title == "Progress"

    def test_make_progress_panel_without_estimate(self, tui_with_mocks: WorkflowMonitorTUI) -> None:
        """Test progress panel without ETA."""
        progress = make_workflow_progress()
        panel = tui_with_mocks._make_progress_panel(progress, None)
        assert panel.title == "Progress"

    def test_make_progress_panel_completed_workflow(
        self, tui_with_mocks: WorkflowMonitorTUI
    ) -> None:
        """Test progress panel for completed workflow."""
        progress = make_workflow_progress(
            status=WorkflowStatus.COMPLETED,
            total_jobs=100,
            completed_jobs=100,
        )
        panel = tui_with_mocks._make_progress_panel(progress, None)
        assert panel is not None

    def test_make_incomplete_jobs_panel_empty(self, tui_with_mocks: WorkflowMonitorTUI) -> None:
        """Test incomplete jobs panel with no incomplete jobs."""
        progress = make_workflow_progress(incomplete_jobs_list=[])
        panel = tui_with_mocks._make_incomplete_jobs_panel(progress)
        assert "Incomplete" in panel.title

    def test_make_incomplete_jobs_panel_with_jobs(self, tui_with_mocks: WorkflowMonitorTUI) -> None:
        """Test incomplete jobs panel with interrupted jobs."""
        progress = make_workflow_progress(
            status=WorkflowStatus.INCOMPLETE,
            incomplete_jobs_list=[
                make_job_info(rule="align", output_file=Path("output.bam")),
                make_job_info(rule="sort"),
            ],
        )
        panel = tui_with_mocks._make_incomplete_jobs_panel(progress)
        assert "(2)" in panel.title

    def test_make_pending_jobs_panel(
        self, tui_with_mocks: WorkflowMonitorTUI, mock_estimator: MagicMock
    ) -> None:
        """Test pending jobs panel creation."""
        tui_with_mocks._estimator = mock_estimator
        progress = make_workflow_progress(total_jobs=100, completed_jobs=50)
        panel = tui_with_mocks._make_pending_jobs_panel(progress)
        assert "Pending" in panel.title

    def test_make_help_panel(self, tui_with_mocks: WorkflowMonitorTUI) -> None:
        """Test help panel creation."""
        panel = tui_with_mocks._make_help_panel()
        assert "Keyboard" in panel.title

    def test_make_footer(self, tui_with_mocks: WorkflowMonitorTUI) -> None:
        """Test footer creation."""
        footer = tui_with_mocks._make_footer()
        assert footer is not None


class TestLayout:
    """Tests for layout building."""

    def test_make_layout_full_mode(self, tui_with_mocks: WorkflowMonitorTUI) -> None:
        """Test full layout mode includes all panels."""
        tui_with_mocks._layout_mode = LayoutMode.FULL
        progress = make_workflow_progress()
        estimate = make_time_estimate()
        layout = tui_with_mocks._make_layout(progress, estimate)
        assert layout is not None

    def test_make_layout_compact_mode(self, tui_with_mocks: WorkflowMonitorTUI) -> None:
        """Test compact layout mode."""
        tui_with_mocks._layout_mode = LayoutMode.COMPACT
        progress = make_workflow_progress()
        layout = tui_with_mocks._make_layout(progress, None)
        assert layout is not None

    def test_make_layout_minimal_mode(self, tui_with_mocks: WorkflowMonitorTUI) -> None:
        """Test minimal layout mode."""
        tui_with_mocks._layout_mode = LayoutMode.MINIMAL
        progress = make_workflow_progress()
        layout = tui_with_mocks._make_layout(progress, None)
        assert layout is not None

    def test_make_layout_with_failed_jobs(self, tui_with_mocks: WorkflowMonitorTUI) -> None:
        """Test layout includes failed jobs panel when applicable."""
        tui_with_mocks._layout_mode = LayoutMode.FULL
        progress = make_workflow_progress(
            status=WorkflowStatus.FAILED,
            failed_jobs=2,
            failed_jobs_list=[
                make_job_info(rule="align", job_id="1"),
                make_job_info(rule="sort", job_id="2"),
            ],
        )
        layout = tui_with_mocks._make_layout(progress, None)
        assert layout is not None

    def test_make_layout_with_incomplete_jobs(self, tui_with_mocks: WorkflowMonitorTUI) -> None:
        """Test layout includes incomplete panel when applicable."""
        tui_with_mocks._layout_mode = LayoutMode.FULL
        progress = make_workflow_progress(
            status=WorkflowStatus.INCOMPLETE,
            incomplete_jobs_list=[make_job_info(rule="interrupted")],
        )
        layout = tui_with_mocks._make_layout(progress, None)
        assert layout is not None

    def test_make_layout_with_help_overlay(self, tui_with_mocks: WorkflowMonitorTUI) -> None:
        """Test layout with help overlay shown."""
        tui_with_mocks._show_help = True
        progress = make_workflow_progress()
        layout = tui_with_mocks._make_layout(progress, None)
        assert layout is not None


class TestDataBuilding:
    """Tests for data building and transformation methods."""

    def test_build_running_job_data_empty(self, tui_with_mocks: WorkflowMonitorTUI) -> None:
        """Test building job data with no running jobs."""
        result = tui_with_mocks._build_running_job_data([])
        assert result == []

    def test_build_running_job_data_with_jobs(self, tui_with_mocks: WorkflowMonitorTUI) -> None:
        """Test building job data with running jobs."""
        import time

        jobs = [
            make_job_info(rule="align", job_id="1", start_time=time.time() - 100),
            make_job_info(rule="sort", job_id="2", start_time=time.time() - 50),
        ]
        result = tui_with_mocks._build_running_job_data(jobs)
        assert len(result) == 2
        # Each result should be a tuple of (job, elapsed, remaining, start_time, tool_progress)
        assert result[0][0].rule == "align"

    def test_sort_running_job_data_by_rule(self, tui_with_mocks: WorkflowMonitorTUI) -> None:
        """Test sorting job data by rule name."""
        import time

        jobs = [
            make_job_info(rule="zebra", job_id="1", start_time=time.time() - 100),
            make_job_info(rule="alpha", job_id="2", start_time=time.time() - 50),
        ]
        job_data = tui_with_mocks._build_running_job_data(jobs)
        tui_with_mocks._sort_table = "running"
        tui_with_mocks._sort_column = 0  # Rule column
        tui_with_mocks._sort_ascending = True
        sorted_data = tui_with_mocks._sort_running_job_data(job_data)
        assert sorted_data[0][0].rule == "alpha"

    def test_get_running_jobs_list(self, tui_with_mocks: WorkflowMonitorTUI) -> None:
        """Test getting running jobs list with filtering."""
        import time

        progress = make_workflow_progress(
            running_jobs=[
                make_job_info(rule="align", job_id="1", start_time=time.time() - 100),
                make_job_info(rule="sort", job_id="2", start_time=time.time() - 50),
            ]
        )
        result = tui_with_mocks._get_running_jobs_list(progress)
        assert len(result) == 2

    def test_get_completions_list(self, tui_with_mocks: WorkflowMonitorTUI) -> None:
        """Test getting completions list."""
        import time

        progress = make_workflow_progress(
            recent_completions=[
                make_job_info(
                    rule="align", start_time=time.time() - 200, end_time=time.time() - 100
                ),
            ],
            failed_jobs_list=[
                make_job_info(rule="sort", job_id="failed"),
            ],
        )
        jobs, failed_ids = tui_with_mocks._get_completions_list(progress)
        # Should include both completed and failed
        assert len(jobs) == 2
        assert len(failed_ids) == 1


class TestRuleStatsUpdate:
    """Tests for _update_rule_stats_from_completions method."""

    def test_update_stats_no_estimator(self, tui_with_mocks: WorkflowMonitorTUI) -> None:
        """Test that no update happens without estimator."""
        tui_with_mocks._estimator = None
        job = make_job_info(rule="align", job_id="1", start_time=100.0, end_time=200.0)
        # Add job to registry (function now reads from registry, not recent_completions)
        tui_with_mocks._workflow_state.jobs.apply_job_info(job, key=job.job_id)
        progress = make_workflow_progress()
        # Should not raise
        tui_with_mocks._update_rule_stats_from_completions(progress)

    def test_update_stats_with_job_id(
        self, tui_with_mocks: WorkflowMonitorTUI, mock_estimator: MagicMock
    ) -> None:
        """Test stats update with job_id deduplication."""
        tui_with_mocks._estimator = mock_estimator

        job = make_job_info(rule="align", job_id="1", start_time=100.0, end_time=200.0)
        # Add job to registry (function now reads from registry, not recent_completions)
        tui_with_mocks._workflow_state.jobs.apply_job_info(job, key=job.job_id)
        progress = make_workflow_progress()
        tui_with_mocks._update_rule_stats_from_completions(progress)

        # Should have added stats to RuleRegistry
        rule_stats = tui_with_mocks._workflow_state.rules.get("align")
        assert rule_stats is not None
        assert rule_stats.aggregate.count == 1

    def test_update_stats_deduplication(
        self, tui_with_mocks: WorkflowMonitorTUI, mock_estimator: MagicMock
    ) -> None:
        """Test that duplicate jobs are not counted twice."""
        tui_with_mocks._estimator = mock_estimator

        job = make_job_info(rule="align", job_id="1", start_time=100.0, end_time=200.0)
        # Add job to registry (function now reads from registry, not recent_completions)
        tui_with_mocks._workflow_state.jobs.apply_job_info(job, key=job.job_id)
        progress = make_workflow_progress()

        # Call twice with same job
        tui_with_mocks._update_rule_stats_from_completions(progress)
        tui_with_mocks._update_rule_stats_from_completions(progress)

        # Should only count once (deduplication via stats_recorded flag)
        rule_stats = tui_with_mocks._workflow_state.rules.get("align")
        assert rule_stats is not None
        assert rule_stats.aggregate.count == 1

    def test_update_stats_with_threads(
        self, tui_with_mocks: WorkflowMonitorTUI, mock_estimator: MagicMock
    ) -> None:
        """Test that thread stats are updated when job has threads."""
        tui_with_mocks._estimator = mock_estimator

        job = make_job_info(rule="align", job_id="1", start_time=100.0, end_time=200.0, threads=4)
        # Add job to registry (function now reads from registry, not recent_completions)
        tui_with_mocks._workflow_state.jobs.apply_job_info(job, key=job.job_id)
        progress = make_workflow_progress()
        tui_with_mocks._update_rule_stats_from_completions(progress)

        # Should have thread stats in RuleRegistry
        thread_stats_dict = tui_with_mocks._workflow_state.rules.to_thread_stats_dict()
        assert "align" in thread_stats_dict
        assert 4 in thread_stats_dict["align"].stats_by_threads

    def test_update_stats_skips_no_duration(
        self, tui_with_mocks: WorkflowMonitorTUI, mock_estimator: MagicMock
    ) -> None:
        """Test that jobs without duration are skipped."""
        tui_with_mocks._estimator = mock_estimator

        # Job with no end_time means no duration
        job = make_job_info(rule="align", job_id="1", start_time=100.0, end_time=None)
        # Add job to registry (function now reads from registry, not recent_completions)
        tui_with_mocks._workflow_state.jobs.apply_job_info(job, key=job.job_id)
        progress = make_workflow_progress()
        tui_with_mocks._update_rule_stats_from_completions(progress)

        # Should not have added stats (job has no duration)
        assert tui_with_mocks._workflow_state.rules.get("align") is None


class TestStatsPanel:
    """Tests for _make_stats_panel method."""

    def test_stats_panel_estimation_disabled(self, tui_with_mocks: WorkflowMonitorTUI) -> None:
        """Test stats panel when estimation is disabled."""
        tui_with_mocks.use_estimation = False
        panel = tui_with_mocks._make_stats_panel()
        assert "disabled" in str(panel.renderable).lower()

    def test_stats_panel_no_data(
        self, tui_with_mocks: WorkflowMonitorTUI, mock_estimator: MagicMock
    ) -> None:
        """Test stats panel with no historical data."""
        tui_with_mocks._estimator = mock_estimator
        tui_with_mocks._estimator.rule_stats = {}
        panel = tui_with_mocks._make_stats_panel()
        assert "No historical data" in str(panel.renderable)

    def test_stats_panel_with_data(
        self, tui_with_mocks: WorkflowMonitorTUI, mock_estimator: MagicMock
    ) -> None:
        """Test stats panel with rule statistics."""
        from snakesee.models import RuleTimingStats

        tui_with_mocks._estimator = mock_estimator
        tui_with_mocks._estimator.rule_stats = {
            "align": RuleTimingStats(rule="align", durations=[100.0, 110.0, 90.0]),
            "sort": RuleTimingStats(rule="sort", durations=[50.0, 55.0]),
        }
        panel = tui_with_mocks._make_stats_panel()
        assert "Rule Statistics" in panel.title

    def test_stats_panel_with_thread_stats(
        self, tui_with_mocks: WorkflowMonitorTUI, mock_estimator: MagicMock
    ) -> None:
        """Test stats panel displays thread-specific statistics."""
        tui_with_mocks._estimator = mock_estimator

        # Populate RuleRegistry with thread-specific data
        tui_with_mocks._workflow_state.rules.record_completion(
            rule="align", duration=100.0, timestamp=1000.0, threads=4
        )
        tui_with_mocks._workflow_state.rules.record_completion(
            rule="align", duration=60.0, timestamp=2000.0, threads=8
        )

        panel = tui_with_mocks._make_stats_panel()
        assert panel is not None

    def test_stats_panel_sorting_active(
        self, tui_with_mocks: WorkflowMonitorTUI, mock_estimator: MagicMock
    ) -> None:
        """Test stats panel when sorting is active."""
        from snakesee.models import RuleTimingStats

        tui_with_mocks._estimator = mock_estimator
        tui_with_mocks._estimator.rule_stats = {
            "align": RuleTimingStats(rule="align", durations=[100.0]),
        }
        tui_with_mocks._sort_table = "stats"
        tui_with_mocks._sort_column = 0
        tui_with_mocks._sort_ascending = True

        panel = tui_with_mocks._make_stats_panel()
        assert "sorting" in panel.title


class TestJobLogPanel:
    """Tests for _make_job_log_panel method."""

    def test_job_log_panel_no_running_jobs(self, tui_with_mocks: WorkflowMonitorTUI) -> None:
        """Test job log panel with no running jobs."""
        tui_with_mocks._job_selection_mode = True
        tui_with_mocks._log_source = "running"
        progress = make_workflow_progress(running_jobs=[])
        panel = tui_with_mocks._make_job_log_panel(progress)
        assert "No running jobs" in str(panel.renderable)

    def test_job_log_panel_no_completed_jobs(self, tui_with_mocks: WorkflowMonitorTUI) -> None:
        """Test job log panel with no completed jobs."""
        tui_with_mocks._job_selection_mode = True
        tui_with_mocks._log_source = "completions"
        progress = make_workflow_progress(recent_completions=[], failed_jobs_list=[])
        panel = tui_with_mocks._make_job_log_panel(progress)
        assert "No completed jobs" in str(panel.renderable)

    def test_job_log_panel_no_log_file(self, tui_with_mocks: WorkflowMonitorTUI) -> None:
        """Test job log panel when log file doesn't exist."""
        import time

        tui_with_mocks._job_selection_mode = True
        tui_with_mocks._log_source = "running"
        progress = make_workflow_progress(
            running_jobs=[
                make_job_info(rule="align", job_id="1", start_time=time.time() - 100),
            ]
        )
        panel = tui_with_mocks._make_job_log_panel(progress)
        assert "No log file for" in str(panel.renderable)


class TestInitMethods:
    """Tests for TUI initialization methods."""

    def test_init_event_reader_disabled(self, tui_with_mocks: WorkflowMonitorTUI) -> None:
        """Test event reader init when events disabled."""
        tui_with_mocks._events_enabled = False
        tui_with_mocks._event_reader = "placeholder"  # type: ignore[assignment]
        tui_with_mocks._init_event_reader()
        # Should not modify event_reader when disabled
        assert tui_with_mocks._event_reader == "placeholder"

    def test_init_event_reader_no_file(self, tui_with_mocks: WorkflowMonitorTUI) -> None:
        """Test event reader init when event file doesn't exist."""
        tui_with_mocks._events_enabled = True
        tui_with_mocks._init_event_reader()
        assert tui_with_mocks._event_reader is None

    def test_init_log_reader(self, tui_with_mocks: WorkflowMonitorTUI) -> None:
        """Test log reader initialization."""
        tui_with_mocks._init_log_reader()
        # Should have created a log reader (even with placeholder)
        assert tui_with_mocks._log_reader is not None

    def test_init_validation_no_event_file(self, tui_with_mocks: WorkflowMonitorTUI) -> None:
        """Test validation init when event file doesn't exist."""
        tui_with_mocks._init_validation()
        # Should not create validator without event file
        assert tui_with_mocks._event_accumulator is None


class TestTerminalHandling:
    """Tests for TUI terminal handling and run loop."""

    @pytest.fixture
    def mock_console(self) -> MagicMock:
        """Create a mock console."""
        console = MagicMock()
        console.width = 120
        console.height = 40
        console.is_terminal = True
        return console

    @pytest.fixture
    def tui(
        self, snakemake_dir: Path, tmp_path: Path, mock_console: MagicMock
    ) -> WorkflowMonitorTUI:
        """Create a TUI instance for testing."""
        with patch("snakesee.tui.monitor.Console", return_value=mock_console):
            return WorkflowMonitorTUI(
                workflow_dir=tmp_path,
                refresh_rate=2.0,
            )

    def test_run_non_terminal_prints_warning(
        self, tui: WorkflowMonitorTUI, mock_console: MagicMock
    ) -> None:
        """Test that run() prints warning when not in terminal."""
        mock_console.is_terminal = False

        tui.run()

        # Should have printed warning
        mock_console.print.assert_called()
        call_args = str(mock_console.print.call_args)
        assert "Warning" in call_args
        assert "interactive terminal" in call_args

    def test_run_non_terminal_returns_early(
        self, tui: WorkflowMonitorTUI, mock_console: MagicMock
    ) -> None:
        """Test that run() returns early when not in terminal."""
        mock_console.is_terminal = False

        # Should not raise any errors and return quickly
        tui.run()

        # Live should not have been created (no screen=True call)
        # Just verify it completed without errors

    def test_run_simple_is_fallback(self, tui: WorkflowMonitorTUI, mock_console: MagicMock) -> None:
        """Test that _run_simple exists as a fallback mode."""
        # Verify _run_simple can be called directly (fallback path)
        tui._running = False  # Exit immediately
        tui._run_simple()
        # Should have printed the simple mode message
        calls = [str(c) for c in mock_console.print.call_args_list]
        assert any("simple mode" in c.lower() for c in calls)

    def test_run_simple_prints_mode_message(
        self, tui: WorkflowMonitorTUI, mock_console: MagicMock
    ) -> None:
        """Test that _run_simple prints the simple mode message."""
        # Make workflow appear complete immediately to exit loop
        tui._running = False

        tui._run_simple()

        # Check that simple mode message was printed
        calls = [str(c) for c in mock_console.print.call_args_list]
        assert any("simple mode" in c.lower() for c in calls)

    def test_run_simple_keyboard_interrupt(
        self, tui: WorkflowMonitorTUI, mock_console: MagicMock
    ) -> None:
        """Test that _run_simple handles KeyboardInterrupt gracefully."""
        with patch.object(tui, "_poll_state", side_effect=KeyboardInterrupt):
            # Should not raise
            tui._run_simple()

    def test_run_simple_exits_on_completed(
        self, tui: WorkflowMonitorTUI, mock_console: MagicMock
    ) -> None:
        """Test that _run_simple exits when workflow completes."""
        progress = make_workflow_progress(status=WorkflowStatus.COMPLETED)

        call_count = 0

        def mock_poll() -> tuple[WorkflowProgress, TimeEstimate | None]:
            nonlocal call_count
            call_count += 1
            return progress, None

        with patch.object(tui, "_poll_state", side_effect=mock_poll):
            with patch("time.sleep"):  # Don't actually sleep
                tui._run_simple()

        # Should have polled once and exited
        assert call_count == 1

    def test_run_simple_exits_on_failed(
        self, tui: WorkflowMonitorTUI, mock_console: MagicMock
    ) -> None:
        """Test that _run_simple exits when workflow fails."""
        progress = make_workflow_progress(status=WorkflowStatus.FAILED)

        with patch.object(tui, "_poll_state", return_value=(progress, None)):
            with patch("time.sleep"):
                tui._run_simple()

        # Verify it printed the finished message
        calls = [str(c) for c in mock_console.print.call_args_list]
        assert any("finished" in c.lower() for c in calls)


class TestKeyboardInput:
    """Tests for keyboard input handling in the TUI."""

    @pytest.fixture
    def mock_console(self) -> MagicMock:
        """Create a mock console."""
        console = MagicMock()
        console.width = 120
        console.height = 40
        console.is_terminal = True
        return console

    @pytest.fixture
    def tui(
        self, snakemake_dir: Path, tmp_path: Path, mock_console: MagicMock
    ) -> WorkflowMonitorTUI:
        """Create a TUI instance for testing."""
        with patch("snakesee.tui.monitor.Console", return_value=mock_console):
            return WorkflowMonitorTUI(
                workflow_dir=tmp_path,
                refresh_rate=2.0,
            )

    def test_handle_key_quit_lowercase(self, tui: WorkflowMonitorTUI) -> None:
        """Test that 'q' quits."""
        assert tui._handle_key("q") is True

    def test_handle_key_quit_uppercase(self, tui: WorkflowMonitorTUI) -> None:
        """Test that 'Q' quits."""
        assert tui._handle_key("Q") is True

    def test_handle_key_pause_toggle(self, tui: WorkflowMonitorTUI) -> None:
        """Test pause toggle with 'p'."""
        assert tui._paused is False
        result = tui._handle_key("p")
        assert result is False
        assert tui._paused is True

    def test_handle_key_pause_toggle_back(self, tui: WorkflowMonitorTUI) -> None:
        """Test pause toggle back with 'p'."""
        tui._paused = True
        tui._handle_key("p")
        assert tui._paused is False

    def test_handle_key_estimation_toggle(self, tui: WorkflowMonitorTUI) -> None:
        """Test estimation toggle with 'e'."""
        original = tui.use_estimation
        tui._handle_key("e")
        assert tui.use_estimation is not original

    def test_handle_key_refresh_rate_increase(self, tui: WorkflowMonitorTUI) -> None:
        """Test refresh rate increase with '+' (slower refresh)."""
        original_rate = tui.refresh_rate
        tui._handle_key("+")
        assert tui.refresh_rate > original_rate

    def test_handle_key_refresh_rate_decrease(self, tui: WorkflowMonitorTUI) -> None:
        """Test refresh rate decrease with '-' (faster refresh)."""
        tui.refresh_rate = 5.0  # Set to middle value
        original_rate = tui.refresh_rate
        tui._handle_key("-")
        assert tui.refresh_rate < original_rate

    def test_handle_key_help_toggle(self, tui: WorkflowMonitorTUI) -> None:
        """Test help overlay toggle with '?'."""
        assert tui._show_help is False
        tui._handle_key("?")
        assert tui._show_help is True

    def test_handle_key_help_close(self, tui: WorkflowMonitorTUI) -> None:
        """Test that any key closes help overlay."""
        tui._show_help = True
        tui._handle_key("x")
        assert tui._show_help is False

    def test_handle_key_force_refresh(self, tui: WorkflowMonitorTUI) -> None:
        """Test force refresh with 'r'."""
        tui._force_refresh = False
        tui._handle_key("r")
        assert tui._force_refresh is True

    def test_handle_key_ctrl_r_hard_refresh(self, tui: WorkflowMonitorTUI) -> None:
        """Test hard refresh with Ctrl+r."""
        tui._force_refresh = False
        tui._handle_key("\x12")  # Ctrl+r
        assert tui._force_refresh is True

    def test_handle_key_wildcard_conditioning_toggle(self, tui: WorkflowMonitorTUI) -> None:
        """Test wildcard conditioning toggle with 'w'."""
        original = tui._use_wildcard_conditioning
        tui._handle_key("w")
        assert tui._use_wildcard_conditioning is not original

    def test_handle_key_unknown_returns_false(self, tui: WorkflowMonitorTUI) -> None:
        """Test that unknown keys return False."""
        assert tui._handle_key("z") is False
        assert tui._handle_key("@") is False

    def test_handle_key_navigation_up(self, tui: WorkflowMonitorTUI) -> None:
        """Test navigation up with k (vim-style)."""
        # k is mapped from up arrow - just verify no error
        result = tui._handle_key("k")
        assert result is False  # Navigation doesn't quit

    def test_handle_key_navigation_down(self, tui: WorkflowMonitorTUI) -> None:
        """Test navigation down with j (vim-style)."""
        # j is mapped from down arrow
        tui._handle_key("j")
        # Just verify no error

    def test_handle_key_layout_cycle(self, tui: WorkflowMonitorTUI) -> None:
        """Test layout mode cycling with Tab."""
        original_mode = tui._layout_mode
        tui._handle_key("\t")  # Tab cycles layout
        # Should cycle to next mode
        assert tui._layout_mode != original_mode


class TestTerminalSettingsRestore:
    """Tests for terminal settings save/restore behavior."""

    @pytest.fixture
    def mock_console(self) -> MagicMock:
        """Create a mock console for terminal tests."""
        console = MagicMock()
        console.width = 120
        console.height = 40
        console.is_terminal = True
        return console

    @pytest.fixture
    def tui(
        self, snakemake_dir: Path, tmp_path: Path, mock_console: MagicMock
    ) -> WorkflowMonitorTUI:
        """Create a TUI instance for testing."""
        with patch("snakesee.tui.monitor.Console", return_value=mock_console):
            return WorkflowMonitorTUI(
                workflow_dir=tmp_path,
                refresh_rate=0.1,  # Fast for testing
            )

    def test_terminal_settings_restored_on_normal_exit(
        self, tui: WorkflowMonitorTUI, mock_console: MagicMock
    ) -> None:
        """Test that terminal settings are restored on normal exit."""
        mock_termios = MagicMock()
        mock_tty = MagicMock()
        mock_old_settings = ["mock", "settings"]
        mock_termios.tcgetattr.return_value = mock_old_settings

        # Make stdin look like a valid file descriptor
        mock_stdin = MagicMock()
        mock_stdin.fileno.return_value = 0

        def quit_immediately(*args: object) -> tuple[list[MagicMock], list[object], list[object]]:
            # First call returns stdin ready, second returns empty
            return ([mock_stdin], [], [])

        call_count = 0

        def mock_select(*args: object) -> tuple[list[object], list[object], list[object]]:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return ([mock_stdin], [], [])
            return ([], [], [])

        with patch.dict(
            "sys.modules",
            {"termios": mock_termios, "tty": mock_tty},
        ):
            with patch("sys.stdin", mock_stdin):
                with patch("select.select", side_effect=mock_select):
                    with patch("os.read", return_value=b"q"):  # Quit key
                        with patch("fcntl.fcntl"):
                            with patch("snakesee.tui.monitor.Live"):
                                tui.run()

        # Verify tcsetattr was called to restore settings
        mock_termios.tcsetattr.assert_called()

    def test_terminal_settings_restored_on_keyboard_interrupt(
        self, tui: WorkflowMonitorTUI, mock_console: MagicMock
    ) -> None:
        """Test terminal settings are restored after KeyboardInterrupt."""
        mock_termios = MagicMock()
        mock_tty = MagicMock()
        mock_old_settings = ["mock", "settings"]
        mock_termios.tcgetattr.return_value = mock_old_settings

        mock_stdin = MagicMock()
        mock_stdin.fileno.return_value = 0

        with patch.dict(
            "sys.modules",
            {"termios": mock_termios, "tty": mock_tty},
        ):
            with patch("sys.stdin", mock_stdin):
                with patch("select.select", side_effect=KeyboardInterrupt):
                    with patch("snakesee.tui.monitor.Live"):
                        tui.run()

        # Verify settings were restored even after interrupt
        mock_termios.tcsetattr.assert_called()


class TestEscapeSequenceParsing:
    """Tests for escape sequence parsing in keyboard input."""

    @pytest.fixture
    def mock_console(self) -> MagicMock:
        """Create a mock console."""
        console = MagicMock()
        console.width = 120
        console.height = 40
        console.is_terminal = True
        return console

    @pytest.fixture
    def tui(
        self, snakemake_dir: Path, tmp_path: Path, mock_console: MagicMock
    ) -> WorkflowMonitorTUI:
        """Create a TUI instance for testing."""
        with patch("snakesee.tui.monitor.Console", return_value=mock_console):
            return WorkflowMonitorTUI(
                workflow_dir=tmp_path,
                refresh_rate=2.0,
            )

    def test_up_arrow_mapped_to_k(self, tui: WorkflowMonitorTUI) -> None:
        """Test that up arrow escape sequence is recognized."""
        # The run loop parses \x1b[A or \x1bOA as up arrow
        # and maps to 'k' (vim up) before calling _handle_key
        # We test the _handle_key with the mapped value
        result = tui._handle_key("k")  # k (mapped from up arrow)
        assert result is False  # Navigation doesn't quit

    def test_down_arrow_mapped_to_j(self, tui: WorkflowMonitorTUI) -> None:
        """Test that down arrow escape sequence is recognized."""
        result = tui._handle_key("j")  # j (mapped from down arrow)
        assert result is False

    def test_escape_alone_does_not_quit(self, tui: WorkflowMonitorTUI) -> None:
        """Test that pressing Escape alone doesn't quit."""
        result = tui._handle_key("\x1b")  # Escape
        assert result is False


class TestKeyboardEdgeCases:
    """Tests for keyboard input edge cases."""

    @pytest.fixture
    def mock_console(self) -> MagicMock:
        """Create a mock console."""
        console = MagicMock()
        console.width = 120
        console.height = 40
        console.is_terminal = True
        return console

    @pytest.fixture
    def tui(
        self, snakemake_dir: Path, tmp_path: Path, mock_console: MagicMock
    ) -> WorkflowMonitorTUI:
        """Create a TUI instance for testing."""
        with patch("snakesee.tui.monitor.Console", return_value=mock_console):
            return WorkflowMonitorTUI(
                workflow_dir=tmp_path,
                refresh_rate=2.0,
            )

    def test_rapid_key_presses(self, tui: WorkflowMonitorTUI) -> None:
        """Test handling of rapid key presses."""
        initial_rate = tui.refresh_rate
        # Simulate rapid + key presses
        for _ in range(10):
            tui._handle_key("+")
        # Rate should increase but be capped at max
        assert tui.refresh_rate <= MAX_REFRESH_RATE
        assert tui.refresh_rate >= initial_rate

    def test_refresh_rate_bounds(self, tui: WorkflowMonitorTUI) -> None:
        """Test refresh rate stays within bounds."""
        # Decrease to minimum
        for _ in range(100):
            tui._handle_key("-")
        assert tui.refresh_rate >= MIN_REFRESH_RATE

        # Increase to maximum
        for _ in range(100):
            tui._handle_key("+")
        assert tui.refresh_rate <= MAX_REFRESH_RATE

    def test_filter_special_characters(self, tui: WorkflowMonitorTUI) -> None:
        """Test filter mode handles special characters."""
        tui._handle_key("/")  # Enter filter mode
        assert tui._filter_mode is True

        # Type special characters (stored in _filter_input during filter mode)
        tui._handle_key("*")
        tui._handle_key(".")
        tui._handle_key("?")
        assert "*" in tui._filter_input
        assert "." in tui._filter_input
        assert "?" in tui._filter_input

    def test_filter_unicode_characters(self, tui: WorkflowMonitorTUI) -> None:
        """Test filter mode handles unicode characters."""
        tui._handle_key("/")
        # Unicode characters should be accepted (stored in _filter_input)
        tui._handle_key("α")
        tui._handle_key("β")
        assert "α" in tui._filter_input
        assert "β" in tui._filter_input

    def test_filter_backspace_empty(self, tui: WorkflowMonitorTUI) -> None:
        """Test backspace on empty filter is safe."""
        tui._handle_key("/")
        assert tui._filter_input == ""
        # Backspace on empty filter should be safe
        tui._handle_key("\x7f")
        assert tui._filter_input == ""

    def test_filter_clear_on_escape(self, tui: WorkflowMonitorTUI) -> None:
        """Test escape clears filter and exits filter mode."""
        tui._handle_key("/")
        tui._handle_key("t")
        tui._handle_key("e")
        tui._handle_key("s")
        tui._handle_key("t")
        assert tui._filter_input == "test"

        tui._handle_key("\x1b")  # Escape
        assert tui._filter_mode is False
        assert tui._filter_input == ""

    def test_job_selection_boundaries(self, tui: WorkflowMonitorTUI) -> None:
        """Test job selection stays within bounds."""
        # Navigate up at top should be safe (index stays >= 0)
        for _ in range(10):
            tui._handle_key("k")  # k (up)
        assert tui._selected_job_index >= 0
        assert tui._selected_completion_index >= 0

        # Navigate down should work
        tui._handle_key("j")  # j (down)
        assert tui._selected_job_index >= 0
        assert tui._selected_completion_index >= 0

    def test_layout_cycle_wraps(self, tui: WorkflowMonitorTUI) -> None:
        """Test layout cycling wraps around correctly."""
        initial_layout = tui._layout_mode
        layouts_seen = [initial_layout]

        # Cycle through all layouts (Tab key cycles layouts)
        for _ in range(10):
            tui._handle_key("\t")
            layouts_seen.append(tui._layout_mode)

        # Should have seen all layout modes
        assert LayoutMode.FULL in layouts_seen
        assert LayoutMode.COMPACT in layouts_seen
        assert LayoutMode.MINIMAL in layouts_seen

    def test_sort_cycle_wraps(self, tui: WorkflowMonitorTUI) -> None:
        """Test sort table cycling wraps around."""
        # s and S cycle through sort tables (running, completions, pending, stats)
        tables_seen = [tui._sort_table]

        # Cycle through sort options multiple times
        for _ in range(10):
            tui._handle_key("s")
            tables_seen.append(tui._sort_table)

        # Should have seen all sort tables
        assert "running" in tables_seen
        assert "completions" in tables_seen
        assert "pending" in tables_seen
        assert "stats" in tables_seen

    def test_toggle_states_are_boolean(self, tui: WorkflowMonitorTUI) -> None:
        """Test toggle states toggle properly."""
        # Test pause toggle (p key)
        initial_paused = tui._paused
        tui._handle_key("p")
        assert tui._paused != initial_paused
        tui._handle_key("p")
        assert tui._paused == initial_paused

        # Test help toggle
        initial_help = tui._show_help
        tui._handle_key("?")
        assert tui._show_help != initial_help
        tui._handle_key("?")
        assert tui._show_help == initial_help

    def test_null_character_ignored(self, tui: WorkflowMonitorTUI) -> None:
        """Test null character is ignored."""
        result = tui._handle_key("\x00")
        assert result is False

    def test_unknown_control_char_ignored(self, tui: WorkflowMonitorTUI) -> None:
        """Test unknown control characters are ignored."""
        # Ctrl+C (ETX) is handled by KeyboardInterrupt, not _handle_key
        # Test other control chars are safely ignored
        result = tui._handle_key("\x01")  # Ctrl+A
        assert result is False

    def test_log_navigation_bounds(self, tui: WorkflowMonitorTUI) -> None:
        """Test log navigation stays within bounds."""
        # Navigate to older logs (should be safe even with no logs)
        for _ in range(10):
            tui._handle_key("[")

        # Navigate to newer logs
        for _ in range(10):
            tui._handle_key("]")

        # Should not crash and offset should be valid
        assert tui._log_scroll_offset >= 0


class TestJobSelectionModeKeyHandling:
    """Tests for key handling in job selection mode."""

    @pytest.fixture
    def mock_console(self) -> MagicMock:
        """Create a mock console."""
        console = MagicMock()
        console.width = 120
        console.height = 40
        console.is_terminal = True
        return console

    @pytest.fixture
    def tui(
        self, snakemake_dir: Path, tmp_path: Path, mock_console: MagicMock
    ) -> WorkflowMonitorTUI:
        """Create a TUI instance for testing."""
        with patch("snakesee.tui.monitor.Console", return_value=mock_console):
            return WorkflowMonitorTUI(
                workflow_dir=tmp_path,
                refresh_rate=2.0,
            )

    def test_help_key_works_in_job_selection_mode(self, tui: WorkflowMonitorTUI) -> None:
        """Test that '?' shows help in job selection mode."""
        tui._job_selection_mode = True
        tui._log_source = "running"
        assert tui._show_help is False

        result = tui._handle_key("?")

        assert result is False  # Should not quit
        assert tui._show_help is True

    def test_sort_cycle_forward_in_job_selection_mode(self, tui: WorkflowMonitorTUI) -> None:
        """Test that 's' cycles sort table in job selection mode."""
        tui._job_selection_mode = True
        tui._log_source = "running"
        assert tui._sort_table is None

        tui._handle_key("s")
        assert tui._sort_table == "running"

        tui._handle_key("s")
        assert tui._sort_table == "completions"

    def test_sort_cycle_backward_in_job_selection_mode(self, tui: WorkflowMonitorTUI) -> None:
        """Test that 'S' cycles sort table backward in job selection mode."""
        tui._job_selection_mode = True
        tui._log_source = "running"
        assert tui._sort_table is None

        tui._handle_key("S")
        assert tui._sort_table == "stats"

        tui._handle_key("S")
        assert tui._sort_table == "pending"

    def test_column_sort_keys_in_job_selection_mode(self, tui: WorkflowMonitorTUI) -> None:
        """Test that 1-4 keys work for column sorting in job selection mode."""
        tui._job_selection_mode = True
        tui._log_source = "running"
        tui._sort_table = "running"  # Must select a table first
        tui._sort_column = 0
        tui._sort_ascending = True

        # Press '2' to sort by column 2
        tui._handle_key("2")
        assert tui._sort_column == 1  # 0-indexed

        # Press '2' again to reverse sort
        tui._handle_key("2")
        assert tui._sort_column == 1
        assert tui._sort_ascending is False

    def test_column_sort_key_3_in_job_selection_mode(self, tui: WorkflowMonitorTUI) -> None:
        """Test that '3' key works in job selection mode."""
        tui._job_selection_mode = True
        tui._log_source = "running"
        tui._sort_table = "running"

        tui._handle_key("3")
        assert tui._sort_column == 2

    def test_column_sort_key_4_in_job_selection_mode(self, tui: WorkflowMonitorTUI) -> None:
        """Test that '4' key works for running/stats tables in job selection mode."""
        tui._job_selection_mode = True
        tui._log_source = "running"
        tui._sort_table = "running"

        tui._handle_key("4")
        assert tui._sort_column == 3

    def test_job_selection_mode_still_handles_navigation(self, tui: WorkflowMonitorTUI) -> None:
        """Test that job navigation still works after sort keys added."""
        tui._job_selection_mode = True
        tui._log_source = "running"
        tui._selected_job_index = 5

        # k should navigate up
        tui._handle_key("k")
        assert tui._selected_job_index == 4

    def test_job_selection_mode_escape_still_exits(self, tui: WorkflowMonitorTUI) -> None:
        """Test that Escape still exits job selection mode."""
        tui._job_selection_mode = True
        tui._log_source = "running"

        tui._handle_key("\x1b")

        assert tui._job_selection_mode is False
        assert tui._log_source is None

    def test_help_closes_with_any_key_in_job_selection_mode(self, tui: WorkflowMonitorTUI) -> None:
        """Test that help overlay closes with any key while in job selection mode."""
        tui._job_selection_mode = True
        tui._log_source = "running"
        tui._show_help = True

        # Any key should close help, even in job selection mode
        tui._handle_key("x")

        assert tui._show_help is False
        # Should still be in job selection mode
        assert tui._job_selection_mode is True

    def test_help_closes_before_job_selection_handles_key(self, tui: WorkflowMonitorTUI) -> None:
        """Test that help closes before job selection mode processes the key."""
        tui._job_selection_mode = True
        tui._log_source = "running"
        tui._show_help = True
        tui._selected_job_index = 5

        # k would normally navigate, but should just close help
        tui._handle_key("k")

        assert tui._show_help is False
        # Job index should NOT have changed (key was consumed by help close)
        assert tui._selected_job_index == 5

    def test_toggle_keys_work_in_table_mode(self, tui: WorkflowMonitorTUI) -> None:
        """Test that toggle keys (p, e, w, a, r, Ctrl+r) work in table navigation mode."""
        tui._job_selection_mode = True
        tui._log_source = "running"

        # p toggles pause
        assert not tui._paused
        tui._handle_key("p")
        assert tui._paused
        assert tui._job_selection_mode is True  # type: ignore[unreachable]  # stays in table mode

        # e toggles estimation
        initial_est = tui.use_estimation
        tui._handle_key("e")
        assert tui.use_estimation != initial_est
        assert tui._job_selection_mode is True

        # w toggles wildcard conditioning
        initial_wc = tui._use_wildcard_conditioning
        tui._handle_key("w")
        assert tui._use_wildcard_conditioning != initial_wc
        assert tui._job_selection_mode is True

        # a toggles accessibility
        from snakesee.tui.accessibility import ACCESSIBLE_CONFIG
        from snakesee.tui.accessibility import DEFAULT_CONFIG

        assert tui._accessibility_config == DEFAULT_CONFIG
        tui._handle_key("a")
        assert tui._accessibility_config == ACCESSIBLE_CONFIG
        assert tui._job_selection_mode is True

        # r triggers force refresh without leaving table mode
        tui._force_refresh = False
        tui._handle_key("r")
        assert tui._force_refresh is True
        assert tui._job_selection_mode is True

        # Ctrl+r triggers hard refresh without leaving table mode
        tui._force_refresh = False
        tui._handle_key("\x12")
        assert tui._force_refresh is True
        assert tui._job_selection_mode is True

    def test_toggle_keys_work_in_log_viewing_mode(self, tui: WorkflowMonitorTUI) -> None:
        """Test that toggle keys (p, e, w, a, r, Ctrl+r) work in log viewing mode."""
        tui._job_selection_mode = True
        tui._log_viewing_mode = True
        tui._log_source = "running"

        # p toggles pause
        assert not tui._paused
        tui._handle_key("p")
        assert tui._paused
        assert tui._log_viewing_mode is True  # type: ignore[unreachable]  # stays in log mode

        # w toggles wildcard conditioning
        initial_wc = tui._use_wildcard_conditioning
        tui._handle_key("w")
        assert tui._use_wildcard_conditioning != initial_wc
        assert tui._log_viewing_mode is True

        # e toggles estimation
        initial_est = tui.use_estimation
        tui._handle_key("e")
        assert tui.use_estimation != initial_est
        assert tui._log_viewing_mode is True

        # a toggles accessibility
        from snakesee.tui.accessibility import ACCESSIBLE_CONFIG
        from snakesee.tui.accessibility import DEFAULT_CONFIG

        assert tui._accessibility_config == DEFAULT_CONFIG
        tui._handle_key("a")
        assert tui._accessibility_config == ACCESSIBLE_CONFIG
        assert tui._log_viewing_mode is True

        # r triggers force refresh without leaving log mode
        tui._force_refresh = False
        tui._handle_key("r")
        assert tui._force_refresh is True
        assert tui._log_viewing_mode is True

        # Ctrl+r triggers hard refresh without leaving log mode
        tui._force_refresh = False
        tui._handle_key("\x12")
        assert tui._force_refresh is True
        assert tui._log_viewing_mode is True

    def test_g_jumps_to_first_job_in_running(self, tui: WorkflowMonitorTUI) -> None:
        """Test that 'g' jumps to first job in running table."""
        tui._job_selection_mode = True
        tui._log_source = "running"
        tui._selected_job_index = 5

        result = tui._handle_table_navigation_key("g", num_jobs=10)

        assert result is False
        assert tui._selected_job_index == 0

    def test_g_jumps_to_first_job_in_completions(self, tui: WorkflowMonitorTUI) -> None:
        """Test that 'g' jumps to first job in completions table."""
        tui._job_selection_mode = True
        tui._log_source = "completions"
        tui._selected_completion_index = 5

        result = tui._handle_table_navigation_key("g", num_jobs=10)

        assert result is False
        assert tui._selected_completion_index == 0

    def test_G_jumps_to_last_job_in_running(self, tui: WorkflowMonitorTUI) -> None:
        """Test that 'G' jumps to last job in running table."""
        tui._job_selection_mode = True
        tui._log_source = "running"
        tui._selected_job_index = 0

        result = tui._handle_table_navigation_key("G", num_jobs=10)

        assert result is False
        assert tui._selected_job_index == 9  # num_jobs - 1

    def test_G_jumps_to_last_job_in_completions(self, tui: WorkflowMonitorTUI) -> None:
        """Test that 'G' jumps to last job in completions table."""
        tui._job_selection_mode = True
        tui._log_source = "completions"
        tui._selected_completion_index = 0

        result = tui._handle_table_navigation_key("G", num_jobs=10)

        assert result is False
        assert tui._selected_completion_index == 9

    def test_shift_tab_switches_from_running_to_stats(self, tui: WorkflowMonitorTUI) -> None:
        """Test that shift-tab cycles backward from running to stats."""
        tui._job_selection_mode = True
        tui._log_source = "running"

        result = tui._handle_table_navigation_key("\x1b[Z", num_jobs=10)

        assert result is False
        assert tui._log_source == "stats"

    def test_shift_tab_switches_from_completions_to_running(self, tui: WorkflowMonitorTUI) -> None:
        """Test that shift-tab cycles backward from completions to running."""
        tui._job_selection_mode = True
        tui._log_source = "completions"

        result = tui._handle_table_navigation_key("\x1b[Z", num_jobs=10)

        assert result is False
        assert tui._log_source == "running"

    def test_tab_cycles_through_all_tables(self, tui: WorkflowMonitorTUI) -> None:
        """Test that tab cycles forward through all 4 tables."""
        tui._job_selection_mode = True
        tui._log_source = "running"

        # running -> completions
        tui._handle_table_navigation_key("\t", num_jobs=10)
        assert tui._log_source == "completions"

        # completions -> pending
        tui._handle_table_navigation_key("\t", num_jobs=10)
        assert tui._log_source == "pending"

        # pending -> stats
        tui._handle_table_navigation_key("\t", num_jobs=10)
        assert tui._log_source == "stats"

        # stats -> running (wrap around)
        tui._handle_table_navigation_key("\t", num_jobs=10)
        assert tui._log_source == "running"

    def test_h_switches_to_left_column(self, tui: WorkflowMonitorTUI) -> None:
        """Test that h switches to the left column table."""
        tui._job_selection_mode = True

        # completions -> running
        tui._log_source = "completions"
        tui._handle_table_navigation_key("h", num_jobs=10)
        assert tui._log_source == "running"

        # stats -> pending
        tui._log_source = "stats"
        tui._handle_table_navigation_key("h", num_jobs=10)
        assert tui._log_source == "pending"

    def test_l_switches_to_right_column(self, tui: WorkflowMonitorTUI) -> None:
        """Test that l switches to the right column table."""
        tui._job_selection_mode = True

        # running -> completions
        tui._log_source = "running"
        tui._handle_table_navigation_key("l", num_jobs=10)
        assert tui._log_source == "completions"

        # pending -> stats
        tui._log_source = "pending"
        tui._handle_table_navigation_key("l", num_jobs=10)
        assert tui._log_source == "stats"

    def test_enter_no_op_for_pending_stats(self, tui: WorkflowMonitorTUI) -> None:
        """Test that enter does not activate log mode for pending/stats tables."""
        tui._job_selection_mode = True

        # pending - enter should not activate log viewing
        tui._log_source = "pending"
        tui._log_viewing_mode = False
        tui._handle_table_navigation_key("\r", num_jobs=10)
        assert tui._log_viewing_mode is False

        # stats - enter should not activate log viewing
        tui._log_source = "stats"
        tui._log_viewing_mode = False
        tui._handle_table_navigation_key("\r", num_jobs=10)
        assert tui._log_viewing_mode is False

    def test_j_k_navigation_for_pending(self, tui: WorkflowMonitorTUI) -> None:
        """Test j/k navigation works for pending table."""
        tui._job_selection_mode = True
        tui._log_source = "pending"
        tui._selected_pending_index = 0

        # j - move down
        tui._handle_table_navigation_key("j", num_jobs=10)
        assert tui._selected_pending_index == 1

        # k - move up
        tui._handle_table_navigation_key("k", num_jobs=10)
        assert tui._selected_pending_index == 0

    def test_j_k_navigation_for_stats(self, tui: WorkflowMonitorTUI) -> None:
        """Test j/k navigation works for stats table."""
        tui._job_selection_mode = True
        tui._log_source = "stats"
        tui._selected_stats_index = 0

        # j - move down
        tui._handle_table_navigation_key("j", num_jobs=10)
        assert tui._selected_stats_index == 1

        # k - move up
        tui._handle_table_navigation_key("k", num_jobs=10)
        assert tui._selected_stats_index == 0

    def test_g_jumps_to_first_in_pending(self, tui: WorkflowMonitorTUI) -> None:
        """Test that 'g' jumps to first item in pending table."""
        tui._job_selection_mode = True
        tui._log_source = "pending"
        tui._selected_pending_index = 5

        result = tui._handle_table_navigation_key("g", num_jobs=10)

        assert result is False
        assert tui._selected_pending_index == 0

    def test_g_jumps_to_first_in_stats(self, tui: WorkflowMonitorTUI) -> None:
        """Test that 'g' jumps to first item in stats table."""
        tui._job_selection_mode = True
        tui._log_source = "stats"
        tui._selected_stats_index = 5

        result = tui._handle_table_navigation_key("g", num_jobs=10)

        assert result is False
        assert tui._selected_stats_index == 0

    def test_G_jumps_to_last_in_pending(self, tui: WorkflowMonitorTUI) -> None:
        """Test that 'G' jumps to last item in pending table."""
        tui._job_selection_mode = True
        tui._log_source = "pending"
        tui._selected_pending_index = 0

        result = tui._handle_table_navigation_key("G", num_jobs=10)

        assert result is False
        assert tui._selected_pending_index == 9

    def test_G_jumps_to_last_in_stats(self, tui: WorkflowMonitorTUI) -> None:
        """Test that 'G' jumps to last item in stats table."""
        tui._job_selection_mode = True
        tui._log_source = "stats"
        tui._selected_stats_index = 0

        result = tui._handle_table_navigation_key("G", num_jobs=10)

        assert result is False
        assert tui._selected_stats_index == 9

    def test_ctrl_d_half_page_down_pending(self, tui: WorkflowMonitorTUI) -> None:
        """Test Ctrl+d moves half page down in pending table."""
        tui._job_selection_mode = True
        tui._log_source = "pending"
        tui._selected_pending_index = 0

        result = tui._handle_table_navigation_key("\x04", num_jobs=20)

        assert result is False
        assert tui._selected_pending_index == 5  # half_page = 5

    def test_ctrl_d_half_page_down_stats(self, tui: WorkflowMonitorTUI) -> None:
        """Test Ctrl+d moves half page down in stats table."""
        tui._job_selection_mode = True
        tui._log_source = "stats"
        tui._selected_stats_index = 0

        result = tui._handle_table_navigation_key("\x04", num_jobs=20)

        assert result is False
        assert tui._selected_stats_index == 5

    def test_ctrl_u_half_page_up_pending(self, tui: WorkflowMonitorTUI) -> None:
        """Test Ctrl+u moves half page up in pending table."""
        tui._job_selection_mode = True
        tui._log_source = "pending"
        tui._selected_pending_index = 10

        result = tui._handle_table_navigation_key("\x15", num_jobs=20)

        assert result is False
        assert tui._selected_pending_index == 5

    def test_ctrl_u_half_page_up_stats(self, tui: WorkflowMonitorTUI) -> None:
        """Test Ctrl+u moves half page up in stats table."""
        tui._job_selection_mode = True
        tui._log_source = "stats"
        tui._selected_stats_index = 10

        result = tui._handle_table_navigation_key("\x15", num_jobs=20)

        assert result is False
        assert tui._selected_stats_index == 5

    def test_ctrl_f_full_page_down_pending(self, tui: WorkflowMonitorTUI) -> None:
        """Test Ctrl+f moves full page down in pending table."""
        tui._job_selection_mode = True
        tui._log_source = "pending"
        tui._selected_pending_index = 0

        result = tui._handle_table_navigation_key("\x06", num_jobs=20)

        assert result is False
        assert tui._selected_pending_index == 10  # full_page = 10

    def test_ctrl_f_full_page_down_stats(self, tui: WorkflowMonitorTUI) -> None:
        """Test Ctrl+f moves full page down in stats table."""
        tui._job_selection_mode = True
        tui._log_source = "stats"
        tui._selected_stats_index = 0

        result = tui._handle_table_navigation_key("\x06", num_jobs=20)

        assert result is False
        assert tui._selected_stats_index == 10

    def test_ctrl_b_full_page_up_pending(self, tui: WorkflowMonitorTUI) -> None:
        """Test Ctrl+b moves full page up in pending table."""
        tui._job_selection_mode = True
        tui._log_source = "pending"
        tui._selected_pending_index = 15

        result = tui._handle_table_navigation_key("\x02", num_jobs=20)

        assert result is False
        assert tui._selected_pending_index == 5

    def test_ctrl_b_full_page_up_stats(self, tui: WorkflowMonitorTUI) -> None:
        """Test Ctrl+b moves full page up in stats table."""
        tui._job_selection_mode = True
        tui._log_source = "stats"
        tui._selected_stats_index = 15

        result = tui._handle_table_navigation_key("\x02", num_jobs=20)

        assert result is False
        assert tui._selected_stats_index == 5

    def test_page_navigation_clamps_to_bounds_pending(self, tui: WorkflowMonitorTUI) -> None:
        """Test page navigation clamps to valid bounds in pending table."""
        tui._job_selection_mode = True
        tui._log_source = "pending"

        # Ctrl+d at end should stay at last item
        tui._selected_pending_index = 8
        tui._handle_table_navigation_key("\x04", num_jobs=10)
        assert tui._selected_pending_index == 9

        # Ctrl+u at start should stay at first item
        tui._selected_pending_index = 2
        tui._handle_table_navigation_key("\x15", num_jobs=10)
        assert tui._selected_pending_index == 0

    def test_page_navigation_clamps_to_bounds_stats(self, tui: WorkflowMonitorTUI) -> None:
        """Test page navigation clamps to valid bounds in stats table."""
        tui._job_selection_mode = True
        tui._log_source = "stats"

        # Ctrl+f at end should stay at last item
        tui._selected_stats_index = 8
        tui._handle_table_navigation_key("\x06", num_jobs=10)
        assert tui._selected_stats_index == 9

        # Ctrl+b at start should stay at first item
        tui._selected_stats_index = 5
        tui._handle_table_navigation_key("\x02", num_jobs=10)
        assert tui._selected_stats_index == 0


class TestJobIdColumnWidth:
    """Tests for dynamic job ID column width calculation."""

    @pytest.fixture
    def mock_console(self) -> MagicMock:
        """Create a mock console."""
        console = MagicMock()
        console.width = 120
        console.height = 40
        console.is_terminal = True
        return console

    @pytest.fixture
    def tui(
        self, snakemake_dir: Path, tmp_path: Path, mock_console: MagicMock
    ) -> WorkflowMonitorTUI:
        """Create a TUI instance for testing."""
        with patch("snakesee.tui.monitor.Console", return_value=mock_console):
            return WorkflowMonitorTUI(
                workflow_dir=tmp_path,
                refresh_rate=2.0,
            )

    def test_empty_jobs_returns_minimum_width(self, tui: WorkflowMonitorTUI) -> None:
        """Test that empty job list returns minimum width of 2."""
        width = tui._get_job_id_column_width([])
        assert width == 2

    def test_single_digit_job_ids(self, tui: WorkflowMonitorTUI) -> None:
        """Test width for single-digit job IDs."""
        jobs = [
            JobInfo(rule="test", job_id="1"),
            JobInfo(rule="test", job_id="5"),
            JobInfo(rule="test", job_id="9"),
        ]
        width = tui._get_job_id_column_width(jobs)
        assert width == 2  # Minimum is 2

    def test_double_digit_job_ids(self, tui: WorkflowMonitorTUI) -> None:
        """Test width for double-digit job IDs."""
        jobs = [
            JobInfo(rule="test", job_id="10"),
            JobInfo(rule="test", job_id="50"),
            JobInfo(rule="test", job_id="99"),
        ]
        width = tui._get_job_id_column_width(jobs)
        assert width == 2

    def test_triple_digit_job_ids(self, tui: WorkflowMonitorTUI) -> None:
        """Test width for triple-digit job IDs."""
        jobs = [
            JobInfo(rule="test", job_id="100"),
            JobInfo(rule="test", job_id="500"),
        ]
        width = tui._get_job_id_column_width(jobs)
        assert width == 3

    def test_large_job_ids(self, tui: WorkflowMonitorTUI) -> None:
        """Test width for large job IDs."""
        jobs = [
            JobInfo(rule="test", job_id="12345"),
        ]
        width = tui._get_job_id_column_width(jobs)
        assert width == 5

    def test_mixed_job_ids(self, tui: WorkflowMonitorTUI) -> None:
        """Test width uses maximum job ID."""
        jobs = [
            JobInfo(rule="test", job_id="5"),
            JobInfo(rule="test", job_id="1000"),
            JobInfo(rule="test", job_id="50"),
        ]
        width = tui._get_job_id_column_width(jobs)
        assert width == 4  # 1000 requires 4 digits

    def test_jobs_without_job_ids(self, tui: WorkflowMonitorTUI) -> None:
        """Test width based on job count when no job IDs."""
        jobs = [
            JobInfo(rule="test"),
            JobInfo(rule="test"),
            JobInfo(rule="test"),
        ]
        width = tui._get_job_id_column_width(jobs)
        assert width == 2  # 3 jobs, single digit, minimum 2

    def test_many_jobs_without_job_ids(self, tui: WorkflowMonitorTUI) -> None:
        """Test width scales with job count when no job IDs."""
        jobs = [JobInfo(rule="test") for _ in range(150)]
        width = tui._get_job_id_column_width(jobs)
        assert width == 3  # 150 requires 3 digits

    def test_non_numeric_job_ids(self, tui: WorkflowMonitorTUI) -> None:
        """Test handling of non-numeric job IDs."""
        jobs = [
            JobInfo(rule="test", job_id="abc"),
            JobInfo(rule="test", job_id="xyz123"),
        ]
        width = tui._get_job_id_column_width(jobs)
        # Should handle gracefully (uses string length heuristic)
        assert width >= 2


class TestEventFileCurrentValidation:
    """Tests for _is_event_file_current function (stale event file detection)."""

    def test_current_events_file(self, tmp_path: Path) -> None:
        """Test that a current events file is accepted."""
        import json

        event_file = tmp_path / ".snakesee_events.jsonl"
        log_start_time = 1000.0

        # Write a workflow_started event at the same time as log start
        event_file.write_text(
            json.dumps({"event_type": "workflow_started", "timestamp": 1000.0}) + "\n"
        )

        assert _is_event_file_current(event_file, log_start_time) is True

    def test_stale_events_file(self, tmp_path: Path) -> None:
        """Test that a stale events file from a previous run is rejected."""
        import json

        event_file = tmp_path / ".snakesee_events.jsonl"
        log_start_time = 2000.0  # Current log started at 2000

        # Write a workflow_started event from an older run
        event_file.write_text(
            json.dumps({"event_type": "workflow_started", "timestamp": 1000.0}) + "\n"
        )

        # Events file is 1000 seconds older than log - should be rejected
        assert _is_event_file_current(event_file, log_start_time) is False

    def test_events_file_within_tolerance(self, tmp_path: Path) -> None:
        """Test that events file within 60s tolerance is accepted."""
        import json

        event_file = tmp_path / ".snakesee_events.jsonl"
        log_start_time = 1050.0

        # Write a workflow_started event 50 seconds before log start
        event_file.write_text(
            json.dumps({"event_type": "workflow_started", "timestamp": 1000.0}) + "\n"
        )

        # Events file is 50 seconds older, within 60s tolerance
        assert _is_event_file_current(event_file, log_start_time) is True

    def test_events_file_no_log_start_time(self, tmp_path: Path) -> None:
        """Test that events file is accepted when log start time is unknown."""
        import json

        event_file = tmp_path / ".snakesee_events.jsonl"
        event_file.write_text(
            json.dumps({"event_type": "workflow_started", "timestamp": 1000.0}) + "\n"
        )

        # No log start time - assume events are current
        assert _is_event_file_current(event_file, None) is True

    def test_events_file_missing_workflow_started(self, tmp_path: Path) -> None:
        """Test that events file without workflow_started is rejected."""
        import json

        event_file = tmp_path / ".snakesee_events.jsonl"
        log_start_time = 1000.0

        # Write a different event type as first line
        event_file.write_text(json.dumps({"event_type": "progress", "timestamp": 1000.0}) + "\n")

        # First event isn't workflow_started - should be rejected
        assert _is_event_file_current(event_file, log_start_time) is False

    def test_events_file_empty(self, tmp_path: Path) -> None:
        """Test that empty events file is rejected."""
        event_file = tmp_path / ".snakesee_events.jsonl"
        event_file.write_text("")

        assert _is_event_file_current(event_file, 1000.0) is False

    def test_events_file_nonexistent(self, tmp_path: Path) -> None:
        """Test that nonexistent events file is rejected."""
        event_file = tmp_path / ".snakesee_events.jsonl"

        # Should return False (not raise exception)
        assert _is_event_file_current(event_file, 1000.0) is False

    def test_events_file_newer_than_log(self, tmp_path: Path) -> None:
        """Test that events file newer than log is accepted."""
        import json

        event_file = tmp_path / ".snakesee_events.jsonl"
        log_start_time = 1000.0

        # Write a workflow_started event 100 seconds after log start
        # (shouldn't happen in practice but should be accepted)
        event_file.write_text(
            json.dumps({"event_type": "workflow_started", "timestamp": 1100.0}) + "\n"
        )

        assert _is_event_file_current(event_file, log_start_time) is True


class TestAccessibility:
    """Tests for colorblind-accessible mode."""

    def test_default_config_uses_block_chars(self) -> None:
        """Default config uses filled/light block Unicode characters."""
        from snakesee.tui.accessibility import DEFAULT_CONFIG

        assert DEFAULT_CONFIG.succeeded.char == "\u2588"
        assert DEFAULT_CONFIG.failed.char == "\u2588"
        assert DEFAULT_CONFIG.remaining.char == "\u2591"
        assert DEFAULT_CONFIG.incomplete.char == "\u2591"
        assert DEFAULT_CONFIG.show_legend is False

    def test_accessible_config_uses_distinct_ascii_chars(self) -> None:
        """Accessible config uses visually distinct characters."""
        from snakesee.tui.accessibility import ACCESSIBLE_CONFIG

        # All characters must be distinct from each other
        chars = {
            ACCESSIBLE_CONFIG.succeeded.char,
            ACCESSIBLE_CONFIG.failed.char,
            ACCESSIBLE_CONFIG.remaining.char,
            ACCESSIBLE_CONFIG.incomplete.char,
        }
        assert len(chars) == 4, "All accessible characters must be distinct"
        assert ACCESSIBLE_CONFIG.show_legend is True

    def test_accessible_progress_bar_uses_distinct_chars(
        self, tui_with_mocks: WorkflowMonitorTUI
    ) -> None:
        """In accessible mode, progress bar uses distinct characters per segment."""
        from snakesee.tui.accessibility import ACCESSIBLE_CONFIG

        tui_with_mocks._accessibility_config = ACCESSIBLE_CONFIG
        progress = make_workflow_progress(total_jobs=100, completed_jobs=50, failed_jobs=10)
        bar = tui_with_mocks._make_progress_bar(progress, width=40)
        plain = bar.plain

        assert len(plain) == 40
        # Succeeded segment uses '='
        assert "=" in plain
        # Failed segment uses 'X'
        assert "X" in plain
        # Remaining segment uses middle dot
        assert "\u00b7" in plain
        # No block characters from default mode
        assert "\u2588" not in plain
        assert "\u2591" not in plain

    def test_default_progress_bar_uses_block_chars(
        self, tui_with_mocks: WorkflowMonitorTUI
    ) -> None:
        """Default mode still uses block characters (regression test)."""
        from snakesee.tui.accessibility import DEFAULT_CONFIG

        tui_with_mocks._accessibility_config = DEFAULT_CONFIG
        progress = make_workflow_progress(total_jobs=100, completed_jobs=50, failed_jobs=10)
        bar = tui_with_mocks._make_progress_bar(progress, width=40)
        plain = bar.plain

        assert "\u2588" in plain
        assert "\u2591" in plain

    def test_accessible_mode_always_shows_legend(self, tui_with_mocks: WorkflowMonitorTUI) -> None:
        """In accessible mode, legend is shown even with zero failures."""
        from snakesee.tui.accessibility import ACCESSIBLE_CONFIG

        tui_with_mocks._accessibility_config = ACCESSIBLE_CONFIG
        progress = make_workflow_progress(total_jobs=100, completed_jobs=50, failed_jobs=0)
        panel = tui_with_mocks._make_progress_panel(progress, None)
        # Render the panel to text and check for legend content
        from io import StringIO

        from rich.console import Console

        buf = StringIO()
        console = Console(file=buf, width=120, force_terminal=True)
        console.print(panel)
        output = buf.getvalue()
        assert "succeeded" in output
        assert "remaining" in output

    def test_default_mode_no_legend_without_failures(
        self, tui_with_mocks: WorkflowMonitorTUI
    ) -> None:
        """In default mode, legend is not shown when there are no failures."""
        from snakesee.tui.accessibility import DEFAULT_CONFIG

        tui_with_mocks._accessibility_config = DEFAULT_CONFIG
        progress = make_workflow_progress(total_jobs=100, completed_jobs=50, failed_jobs=0)
        panel = tui_with_mocks._make_progress_panel(progress, None)
        from io import StringIO

        from rich.console import Console

        buf = StringIO()
        console = Console(file=buf, width=120, force_terminal=True)
        console.print(panel)
        output = buf.getvalue()
        assert "succeeded" not in output

    def test_toggle_accessible_mode_with_key(self, tui_with_mocks: WorkflowMonitorTUI) -> None:
        """Pressing 'a' toggles between default and accessible mode."""
        from snakesee.tui.accessibility import ACCESSIBLE_CONFIG
        from snakesee.tui.accessibility import DEFAULT_CONFIG

        assert tui_with_mocks._accessibility_config == DEFAULT_CONFIG
        tui_with_mocks._handle_key("a")
        assert tui_with_mocks._accessibility_config == ACCESSIBLE_CONFIG
        tui_with_mocks._handle_key("a")
        assert tui_with_mocks._accessibility_config == DEFAULT_CONFIG

    def test_incomplete_bar_uses_accessible_char(self, tui_with_mocks: WorkflowMonitorTUI) -> None:
        """Incomplete workflow uses '?' character in accessible mode."""
        from snakesee.tui.accessibility import ACCESSIBLE_CONFIG

        tui_with_mocks._accessibility_config = ACCESSIBLE_CONFIG
        incomplete_jobs = [JobInfo(rule=f"rule_{i}") for i in range(10)]
        progress = make_workflow_progress(
            status=WorkflowStatus.INCOMPLETE,
            total_jobs=100,
            completed_jobs=50,
            failed_jobs=0,
            incomplete_jobs_list=incomplete_jobs,
        )
        bar = tui_with_mocks._make_progress_bar(progress, width=40)
        assert "?" in bar.plain

    def test_incomplete_bar_splits_interrupted_from_pending(
        self, tui_with_mocks: WorkflowMonitorTUI
    ) -> None:
        """Incomplete workflow bar shows interrupted jobs separately from pending jobs."""
        from snakesee.tui.accessibility import ACCESSIBLE_CONFIG

        tui_with_mocks._accessibility_config = ACCESSIBLE_CONFIG
        # 100 total, 50 completed, 0 failed => 50 unfinished
        # But only 5 were actually in progress (interrupted)
        incomplete_jobs = [JobInfo(rule=f"rule_{i}") for i in range(5)]
        progress = make_workflow_progress(
            status=WorkflowStatus.INCOMPLETE,
            total_jobs=100,
            completed_jobs=50,
            failed_jobs=0,
            incomplete_jobs_list=incomplete_jobs,
        )
        bar = tui_with_mocks._make_progress_bar(progress, width=100)
        plain = bar.plain

        # Should have both '?' (incomplete) and middle dot (remaining)
        assert "?" in plain
        assert "\u00b7" in plain
        # The number of '?' chars should be proportional to 5/100 = 5 chars in 100-width bar
        assert plain.count("?") == 5
        # The number of middle dot chars should be proportional to 45/100 = 45 chars
        assert plain.count("\u00b7") == 45

    def test_incomplete_legend_splits_interrupted_from_pending(
        self, tui_with_mocks: WorkflowMonitorTUI
    ) -> None:
        """Incomplete workflow legend shows both incomplete and remaining counts."""
        from io import StringIO

        from rich.console import Console

        from snakesee.tui.accessibility import ACCESSIBLE_CONFIG

        tui_with_mocks._accessibility_config = ACCESSIBLE_CONFIG
        incomplete_jobs = [JobInfo(rule=f"rule_{i}") for i in range(5)]
        progress = make_workflow_progress(
            status=WorkflowStatus.INCOMPLETE,
            total_jobs=100,
            completed_jobs=50,
            failed_jobs=0,
            incomplete_jobs_list=incomplete_jobs,
        )
        panel = tui_with_mocks._make_progress_panel(progress, None)
        buf = StringIO()
        console = Console(file=buf, width=200, force_terminal=True)
        console.print(panel)
        output = buf.getvalue()

        # Should show both incomplete and remaining counts
        assert "5 incomplete" in output
        assert "45 remaining" in output


class TestMaxVisibleRows:
    """Tests for _max_visible_rows dynamic row calculation."""

    @pytest.fixture
    def tui(self, tmp_path: Path) -> WorkflowMonitorTUI:
        """Create a TUI with a mocked console of known dimensions."""
        snakemake_dir = tmp_path / ".snakemake"
        snakemake_dir.mkdir()
        (snakemake_dir / "log").mkdir()
        mock_console = MagicMock()
        mock_console.width = 120
        mock_console.height = 50
        mock_console.is_terminal = True
        with patch("snakesee.tui.monitor.Console", return_value=mock_console):
            return WorkflowMonitorTUI(workflow_dir=tmp_path)

    def test_full_layout_two_panels(self, tui: WorkflowMonitorTUI) -> None:
        """Standard terminal, FULL layout, 2 panels per column."""
        from snakesee.tui.monitor import LayoutMode

        tui._layout_mode = LayoutMode.FULL
        # height=50, chrome=15, body=35, per panel=17, minus overhead=14
        result = tui._max_visible_rows(2)
        assert result == 14

    def test_full_layout_three_panels(self, tui: WorkflowMonitorTUI) -> None:
        """Standard terminal, FULL layout, 3 panels per column."""
        from snakesee.tui.monitor import LayoutMode

        tui._layout_mode = LayoutMode.FULL
        # height=50, chrome=15, body=35, per panel=11, minus overhead=8
        result = tui._max_visible_rows(3)
        assert result == 8

    def test_compact_layout_one_panel(self, tui: WorkflowMonitorTUI) -> None:
        """COMPACT layout uses less chrome (no summary_footer), giving more rows."""
        from snakesee.tui.monitor import LayoutMode

        tui._layout_mode = LayoutMode.COMPACT
        # height=50, chrome=12, body=38, per panel=38, minus overhead=35
        result = tui._max_visible_rows(1)
        assert result == 35

    def test_compact_vs_full_gives_more_rows(self, tui: WorkflowMonitorTUI) -> None:
        """COMPACT layout yields more rows than FULL for the same panel count."""
        from snakesee.tui.monitor import LayoutMode

        tui._layout_mode = LayoutMode.COMPACT
        compact_rows = tui._max_visible_rows(2)
        tui._layout_mode = LayoutMode.FULL
        full_rows = tui._max_visible_rows(2)
        assert compact_rows > full_rows

    def test_small_terminal_floor(self, tui: WorkflowMonitorTUI) -> None:
        """Very small terminal returns minimum of 3 rows."""
        from snakesee.tui.monitor import LayoutMode

        tui.console.height = 10
        tui._layout_mode = LayoutMode.FULL
        # height=10, chrome=15, body=-5 → floor of 3
        result = tui._max_visible_rows(2)
        assert result == 3

    def test_none_height_uses_default(self, tui: WorkflowMonitorTUI) -> None:
        """None console height falls back to 24."""
        from snakesee.tui.monitor import LayoutMode

        tui.console.height = None
        tui._layout_mode = LayoutMode.FULL
        # height=24, chrome=15, body=9, per panel=4, minus overhead=1 → floor 3
        result = tui._max_visible_rows(2)
        assert result == 3

    def test_minimal_layout_matches_compact_chrome(self, tui: WorkflowMonitorTUI) -> None:
        """MINIMAL layout uses the same chrome math as COMPACT."""
        from snakesee.tui.monitor import LayoutMode

        tui._layout_mode = LayoutMode.MINIMAL
        minimal_rows = tui._max_visible_rows(2)
        tui._layout_mode = LayoutMode.COMPACT
        compact_rows = tui._max_visible_rows(2)
        assert minimal_rows == compact_rows
