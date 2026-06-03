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
