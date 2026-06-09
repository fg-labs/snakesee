"""Tests for DataTables in SnakeseeApp."""

from pathlib import Path
from unittest.mock import patch

from textual.widgets import DataTable

from snakesee.tui.app import SnakeseeApp
from tests.conftest import make_job_info
from tests.conftest import make_workflow_progress


async def test_running_table_populates(snakemake_dir: Path, tmp_path: Path) -> None:
    """Running DataTable shows one row per running JobInfo."""
    progress = make_workflow_progress(
        running_jobs=[make_job_info(job_id="1", rule="map_reads", start_time=1000.0)],
    )
    app = SnakeseeApp(workflow_dir=tmp_path)
    with patch.object(app._data, "poll_state", return_value=(progress, None)):
        async with app.run_test() as pilot:
            await pilot.pause()
            table = app.query_one("#running", DataTable)
            assert table.row_count == 1
            await pilot.press("q")


async def test_completions_table_populates(snakemake_dir: Path, tmp_path: Path) -> None:
    """Completions DataTable shows one row per completed JobInfo."""
    progress = make_workflow_progress(
        recent_completions=[
            make_job_info(job_id="2", rule="trim_reads", start_time=900.0, end_time=930.0),
        ],
    )
    app = SnakeseeApp(workflow_dir=tmp_path)
    with patch.object(app._data, "poll_state", return_value=(progress, None)):
        async with app.run_test() as pilot:
            await pilot.pause()
            table = app.query_one("#completions", DataTable)
            assert table.row_count == 1
            await pilot.press("q")


async def test_failed_table_populates(snakemake_dir: Path, tmp_path: Path) -> None:
    """Failed DataTable shows one row per failed JobInfo."""
    progress = make_workflow_progress(
        failed_jobs=1,
        failed_jobs_list=[make_job_info(job_id="3", rule="align")],
    )
    app = SnakeseeApp(workflow_dir=tmp_path)
    with patch.object(app._data, "poll_state", return_value=(progress, None)):
        async with app.run_test() as pilot:
            await pilot.pause()
            table = app.query_one("#failed", DataTable)
            assert table.row_count == 1
            await pilot.press("q")


async def test_incomplete_table_populates(snakemake_dir: Path, tmp_path: Path) -> None:
    """Incomplete DataTable shows one row per incomplete JobInfo."""
    progress = make_workflow_progress(
        incomplete_jobs_list=[
            make_job_info(
                job_id="4",
                rule="dedup",
                output_file=Path("results/dedup.bam"),
            )
        ],
    )
    app = SnakeseeApp(workflow_dir=tmp_path)
    with patch.object(app._data, "poll_state", return_value=(progress, None)):
        async with app.run_test() as pilot:
            await pilot.pause()
            table = app.query_one("#incomplete", DataTable)
            assert table.row_count == 1
            await pilot.press("q")


async def test_tables_present_when_no_data(snakemake_dir: Path, tmp_path: Path) -> None:
    """All six DataTables exist and start empty when there is no workflow data."""
    progress = make_workflow_progress(total_jobs=0, completed_jobs=0)
    app = SnakeseeApp(workflow_dir=tmp_path)
    with patch.object(app._data, "poll_state", return_value=(progress, None)):
        async with app.run_test() as pilot:
            await pilot.pause()
            for table_id in ("running", "completions", "pending", "failed", "incomplete", "stats"):
                table = app.query_one(f"#{table_id}", DataTable)
                assert table.row_count == 0
            await pilot.press("q")


async def test_completions_cost_column_appears_with_cost(
    snakemake_dir: Path, tmp_path: Path
) -> None:
    """The completions table gains a Cost column + cell when cost data is present."""
    from dataclasses import replace

    from snakesee.models import JobInfo

    job = JobInfo(rule="align", job_id="2", start_time=900.0, end_time=930.0, cost_estimate=0.0123)
    progress = replace(make_workflow_progress(recent_completions=[job]), total_cost_estimate=0.0123)
    app = SnakeseeApp(workflow_dir=tmp_path)
    with patch.object(app._data, "poll_state", return_value=(progress, None)):
        async with app.run_test() as pilot:
            await pilot.pause()
            table = app.query_one("#completions", DataTable)
            assert len(table.columns) == 6  # #, Rule, Thr, Duration, Completed, Cost
            row = table.get_row_at(0)
            assert "$0.0123" in row
            await pilot.press("q")


async def test_completions_no_cost_column_without_cost(snakemake_dir: Path, tmp_path: Path) -> None:
    """No Cost column for a run without cost estimates."""
    progress = make_workflow_progress(
        recent_completions=[
            make_job_info(job_id="2", rule="trim", start_time=900.0, end_time=930.0)
        ],
    )
    app = SnakeseeApp(workflow_dir=tmp_path)
    with patch.object(app._data, "poll_state", return_value=(progress, None)):
        async with app.run_test() as pilot:
            await pilot.pause()
            assert len(app.query_one("#completions", DataTable).columns) == 5
            await pilot.press("q")


async def test_completions_cost_column_persists_then_blank(
    snakemake_dir: Path, tmp_path: Path
) -> None:
    """Once added, the Cost column persists; a later cost-free frame renders '-' (no crash)."""
    from dataclasses import replace

    from snakesee.models import JobInfo

    cost_job = JobInfo(
        rule="align", job_id="2", start_time=900.0, end_time=930.0, cost_estimate=0.5
    )
    with_cost = replace(
        make_workflow_progress(recent_completions=[cost_job]), total_cost_estimate=0.5
    )
    plain_job = make_job_info(job_id="3", rule="sort", start_time=900.0, end_time=930.0)
    no_cost = make_workflow_progress(recent_completions=[plain_job])  # total_cost_estimate None

    app = SnakeseeApp(workflow_dir=tmp_path)
    with patch.object(app._data, "poll_state", return_value=(with_cost, None)):
        async with app.run_test() as pilot:
            await pilot.pause()
            table = app.query_one("#completions", DataTable)
            assert len(table.columns) == 6
            # Second frame has no cost: the column persists and the cell is "-".
            app._populate_completions(no_cost)
            assert len(table.columns) == 6  # column did not disappear
            assert table.row_count == 1
            assert "-" in table.get_row_at(0)  # cost cell rendered as "-", no mismatch
            await pilot.press("q")


async def test_completions_mixed_cost_and_blank_cells(snakemake_dir: Path, tmp_path: Path) -> None:
    """A job with cost and one without render their cells correctly in the same table."""
    from dataclasses import replace

    from snakesee.models import JobInfo

    priced = JobInfo(
        rule="align", job_id="2", start_time=900.0, end_time=930.0, cost_estimate=0.0123
    )
    free = JobInfo(rule="sort", job_id="3", start_time=900.0, end_time=930.0)
    progress = replace(
        make_workflow_progress(recent_completions=[priced, free]), total_cost_estimate=0.0123
    )
    app = SnakeseeApp(workflow_dir=tmp_path)
    with patch.object(app._data, "poll_state", return_value=(progress, None)):
        async with app.run_test() as pilot:
            await pilot.pause()
            table = app.query_one("#completions", DataTable)
            costs = {table.get_row_at(i)[1]: table.get_row_at(i)[5] for i in range(table.row_count)}
            assert costs["align"] == "$0.0123"
            assert costs["sort"] == "-"
            await pilot.press("q")


async def test_stats_cost_column_appears_with_cost(snakemake_dir: Path, tmp_path: Path) -> None:
    """The Rule Statistics table gains a per-rule Cost column when cost data exists."""
    from snakesee.events import EventType
    from snakesee.events import SnakeseeEvent

    progress = make_workflow_progress()
    app = SnakeseeApp(workflow_dir=tmp_path)
    with patch.object(app._data, "poll_state", return_value=(progress, None)):
        async with app.run_test() as pilot:
            await pilot.pause()
            # Inject a rule stat + per-rule cost after mount (the initial refresh
            # re-inits the estimator, which would clear pre-mount injections), then
            # populate the stats table directly.
            if app._data._estimator is not None:
                app._data._estimator.current_rules = None
                app._data._estimator._rule_registry.record_completion(
                    rule="align", duration=100.0, timestamp=1.0
                )
            app._data._workflow_state.jobs.apply_event(
                SnakeseeEvent(
                    event_type=EventType.JOB_FINISHED,
                    timestamp=1.0,
                    job_id=7,
                    rule_name="align",
                    cost_estimate=0.5,
                    stopped_at=1.0,
                )
            )
            app._populate_stats()
            table = app.query_one("#stats", DataTable)
            assert len(table.columns) == 6  # Rule, Thr, Count, Avg, Std Dev, Cost
            await pilot.press("q")
