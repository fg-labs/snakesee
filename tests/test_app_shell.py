"""Tests for the Textual SnakeseeApp shell (header/progress/summary + quit)."""

from pathlib import Path

from snakesee.tui.app import SnakeseeApp


async def test_app_boots_and_quits(snakemake_dir: Path, tmp_path: Path) -> None:
    """SnakeseeApp boots, exposes header/progress/summary widgets, and quits on q."""
    app = SnakeseeApp(workflow_dir=tmp_path)
    async with app.run_test() as pilot:
        assert app.query_one("#header") is not None
        assert app.query_one("#progress") is not None
        assert app.query_one("#summary") is not None
        await pilot.press("q")


async def test_app_init_without_estimation(snakemake_dir: Path, tmp_path: Path) -> None:
    """SnakeseeApp accepts use_estimation=False without errors."""
    app = SnakeseeApp(workflow_dir=tmp_path, use_estimation=False)
    async with app.run_test() as pilot:
        await pilot.press("q")
