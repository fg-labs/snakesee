"""Tests for the Rich renderables (progress bar, legend, header, help, ETA).

These cover behavior ported from the Rich TUI into the Textual renderables:
the segmented progress bar with running/pending counts (#57) and the header /
help / ETA quality-of-life touches (#60).
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime
from io import StringIO

import pytest
from rich.console import Console

from snakesee import __version__
from snakesee.models import JobInfo
from snakesee.models import WorkflowStatus
from snakesee.state.clock import FrozenClock
from snakesee.state.clock import reset_clock
from snakesee.state.clock import set_clock
from snakesee.tui.accessibility import ACCESSIBLE_CONFIG
from snakesee.tui.accessibility import DEFAULT_CONFIG
from snakesee.tui.renderables import _truncate_path
from snakesee.tui.renderables import make_header
from snakesee.tui.renderables import make_help
from snakesee.tui.renderables import make_progress_bar
from snakesee.tui.renderables import make_progress_panel
from tests.conftest import make_job_info
from tests.conftest import make_time_estimate
from tests.conftest import make_workflow_progress


def _render(panel: object, width: int = 120) -> str:
    """Render a Rich renderable to plain text for substring assertions."""
    buf = StringIO()
    Console(file=buf, width=width, force_terminal=True).print(panel)
    return buf.getvalue()


# =============================================================================
# #57 — segmented progress bar
# =============================================================================


class TestSegmentedProgressBar:
    def test_bar_fills_full_width(self) -> None:
        """The bar always renders exactly ``width`` characters."""
        progress = make_workflow_progress(total_jobs=100, completed_jobs=50, failed_jobs=10)
        bar = make_progress_bar(progress, width=40, accessibility=DEFAULT_CONFIG)
        assert len(bar.plain) == 40

    def test_all_succeeded_fills_width(self) -> None:
        progress = make_workflow_progress(total_jobs=100, completed_jobs=100, failed_jobs=0)
        bar = make_progress_bar(progress, width=40, accessibility=DEFAULT_CONFIG)
        assert len(bar.plain) == 40

    def test_accessible_bar_uses_distinct_chars(self) -> None:
        """Accessible mode renders distinct glyphs per segment and no block chars."""
        progress = make_workflow_progress(total_jobs=100, completed_jobs=50, failed_jobs=10)
        plain = make_progress_bar(progress, width=40, accessibility=ACCESSIBLE_CONFIG).plain
        assert "=" in plain  # succeeded
        assert "X" in plain  # failed
        assert "·" in plain  # remaining
        assert "█" not in plain
        assert "░" not in plain

    def test_default_bar_uses_block_chars(self) -> None:
        progress = make_workflow_progress(total_jobs=100, completed_jobs=50, failed_jobs=10)
        plain = make_progress_bar(progress, width=40, accessibility=DEFAULT_CONFIG).plain
        assert "█" in plain
        assert "░" in plain

    def test_clamps_when_counts_exceed_total(self) -> None:
        """A transient counter skew (counts > total) never under-renders the bar."""
        progress = make_workflow_progress(total_jobs=10, completed_jobs=12, failed_jobs=0)
        bar = make_progress_bar(progress, width=20, accessibility=DEFAULT_CONFIG)
        assert len(bar.plain) == 20

    def test_incomplete_bar_splits_interrupted_from_pending(self) -> None:
        """An INCOMPLETE run shows interrupted jobs (in-flight) separately from pending."""
        incomplete_jobs = [JobInfo(rule=f"rule_{i}") for i in range(5)]
        progress = make_workflow_progress(
            status=WorkflowStatus.INCOMPLETE,
            total_jobs=100,
            completed_jobs=50,
            failed_jobs=0,
            incomplete_jobs_list=incomplete_jobs,
        )
        plain = make_progress_bar(progress, width=100, accessibility=ACCESSIBLE_CONFIG).plain
        assert plain.count("?") == 5  # 5/100 interrupted
        assert plain.count("·") == 45  # 45/100 pending

    def test_running_bar_uses_in_flight_segment_on_live_run(self) -> None:
        """A live RUNNING run renders its running jobs in the in-flight (yellow) segment."""
        running = [make_job_info(rule="align"), make_job_info(rule="sort")]
        progress = make_workflow_progress(
            status=WorkflowStatus.RUNNING,
            total_jobs=100,
            completed_jobs=50,
            failed_jobs=0,
            running_jobs=running,
        )
        plain = make_progress_bar(progress, width=100, accessibility=ACCESSIBLE_CONFIG).plain
        assert plain.count(">") == 2  # 2/100 running
        assert plain.count("·") == 48  # 100 - 50 - 2


# =============================================================================
# #57 — legend always shows non-zero segments
# =============================================================================


class TestProgressLegend:
    def test_legend_shows_segments_in_default_mode(self) -> None:
        """Default mode lists every non-zero segment (and omits zero ones)."""
        progress = make_workflow_progress(
            total_jobs=100,
            completed_jobs=50,
            failed_jobs=0,
            running_jobs=[make_job_info(rule="align"), make_job_info(rule="sort")],
        )
        panel = make_progress_panel(progress, None, True, DEFAULT_CONFIG)
        out = _render(panel)
        assert "succeeded" in out
        assert "running" in out
        assert "remaining" in out
        assert "failed" not in out

    def test_legend_shown_before_first_completion(self) -> None:
        """With nothing completed yet, the legend still shows running + pending."""
        progress = make_workflow_progress(
            total_jobs=8,
            completed_jobs=0,
            failed_jobs=0,
            running_jobs=[make_job_info(rule="align")],
        )
        out = _render(make_progress_panel(progress, None, True, DEFAULT_CONFIG))
        assert "1 running" in out
        assert "7 remaining" in out
        assert "succeeded" not in out

    def test_incomplete_legend_splits_interrupted_from_pending(self) -> None:
        incomplete_jobs = [JobInfo(rule=f"rule_{i}") for i in range(5)]
        progress = make_workflow_progress(
            status=WorkflowStatus.INCOMPLETE,
            total_jobs=100,
            completed_jobs=50,
            failed_jobs=0,
            incomplete_jobs_list=incomplete_jobs,
        )
        out = _render(make_progress_panel(progress, None, True, ACCESSIBLE_CONFIG), width=200)
        assert "5 incomplete" in out
        assert "45 remaining" in out

    def test_failed_segment_shown_in_legend(self) -> None:
        progress = make_workflow_progress(total_jobs=100, completed_jobs=80, failed_jobs=20)
        out = _render(make_progress_panel(progress, None, True, DEFAULT_CONFIG))
        assert "80 succeeded" in out
        assert "20 failed" in out


# =============================================================================
# #60 — header path truncation
# =============================================================================


class TestHeaderPath:
    def test_short_path_unchanged(self) -> None:
        assert _truncate_path("/a/b/c", 60) == "/a/b/c"

    def test_long_path_middle_truncated(self) -> None:
        path = "/very/long/" + "x" * 200 + "/workflow"
        out = _truncate_path(path, 40)
        assert len(out) == 40
        assert "…" in out  # ellipsis
        assert out.startswith("/very/long")
        assert out.endswith("workflow")

    def test_make_header_returns_panel(self) -> None:
        progress = make_workflow_progress(status=WorkflowStatus.RUNNING)
        panel = make_header(progress, "/abs/workflow", paused=False, event_reader=None)
        out = _render(panel)
        assert "RUNNING" in out
        assert "workflow" in out


# =============================================================================
# #60 — help subtitle carries the snakesee version
# =============================================================================


class TestHelpSubtitle:
    def test_help_subtitle_includes_version(self) -> None:
        panel = make_help()
        assert __version__ in str(panel.subtitle)
        assert "snakesee v" in str(panel.subtitle)


# =============================================================================
# #60 — ETA completion time shows date + timezone when crossing midnight
# =============================================================================


class TestEtaCompletionTime:
    @pytest.fixture
    def _noon_clock(self) -> Iterator[None]:
        """Freeze the clock at local noon today and reset afterwards."""
        noon = datetime.now().astimezone().replace(hour=12, minute=0, second=0, microsecond=0)
        set_clock(FrozenClock(noon.timestamp()))
        yield
        reset_clock()

    def test_same_day_eta_omits_date(self, _noon_clock: None) -> None:
        """An ETA later the same day shows HH:MM:SS with a timezone but no date."""
        progress = make_workflow_progress(status=WorkflowStatus.RUNNING)
        estimate = make_time_estimate(seconds_remaining=3600)  # +1h, still today
        out = _render(make_progress_panel(progress, estimate, True, DEFAULT_CONFIG), width=200)
        assert "13:00:00" in out
        import re

        assert re.search(r"\d{4}-\d{2}-\d{2}", out) is None

    def test_overnight_eta_includes_date(self, _noon_clock: None) -> None:
        """An ETA crossing midnight includes the YYYY-MM-DD date so it isn't ambiguous."""
        progress = make_workflow_progress(status=WorkflowStatus.RUNNING)
        estimate = make_time_estimate(seconds_remaining=18 * 3600)  # +18h, tomorrow 06:00
        out = _render(make_progress_panel(progress, estimate, True, DEFAULT_CONFIG), width=200)
        import re

        assert re.search(r"\d{4}-\d{2}-\d{2}", out) is not None
