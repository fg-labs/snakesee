"""Tests for sort cycling in SnakeseeApp."""

from pathlib import Path

from snakesee.tui.app import SnakeseeApp


async def test_s_cycles_sort_forward(snakemake_dir: Path, tmp_path: Path) -> None:
    """Pressing 's' cycles the sort target forward through all tables and back to None."""
    app = SnakeseeApp(workflow_dir=tmp_path)
    async with app.run_test() as pilot:
        assert app.sort_table is None
        await pilot.press("s")
        assert app.sort_table == "running"
        await pilot.press("s")
        assert app.sort_table == "completions"
        await pilot.press("s")
        assert app.sort_table == "pending"
        await pilot.press("s")
        assert app.sort_table == "stats"
        await pilot.press("s")
        assert app.sort_table is None
        await pilot.press("q")


async def test_S_cycles_sort_backward(snakemake_dir: Path, tmp_path: Path) -> None:
    """Pressing 'S' cycles the sort target backward (None → stats)."""
    app = SnakeseeApp(workflow_dir=tmp_path)
    async with app.run_test() as pilot:
        assert app.sort_table is None
        await pilot.press("S")
        assert app.sort_table == "stats"
        await pilot.press("q")


async def test_sort_cycle_resets_column_and_direction(snakemake_dir: Path, tmp_path: Path) -> None:
    """Cycling the sort target resets sort_column to 0 and sort_ascending to True."""
    app = SnakeseeApp(workflow_dir=tmp_path)
    async with app.run_test() as pilot:
        await pilot.press("s")  # sort_table = "running"
        assert app.sort_table == "running"
        await pilot.press("2")  # sort_column = 1
        assert app.sort_column == 1
        await pilot.press("2")  # same column — toggle direction
        asc: bool = app.sort_ascending
        assert not asc
        await pilot.press("s")  # advance to "completions" — resets column and direction
        assert app.sort_table == "completions"
        assert app.sort_column == 0
        asc = app.sort_ascending
        assert asc
        await pilot.press("q")


async def test_sort_column_keys(snakemake_dir: Path, tmp_path: Path) -> None:
    """Pressing digit keys sets the sort column (0-indexed) and toggles direction on repeat."""
    app = SnakeseeApp(workflow_dir=tmp_path)
    async with app.run_test() as pilot:
        # Set a sort target first.
        await pilot.press("s")  # sort_table = "running"
        assert app.sort_table == "running"
        assert app.sort_column == 0

        await pilot.press("2")
        assert app.sort_column == 1  # key "2" → 0-indexed column 1
        col_asc: bool = app.sort_ascending
        assert col_asc

        await pilot.press("2")  # same column → toggle direction
        assert app.sort_column == 1
        col_asc = app.sort_ascending
        assert not col_asc

        await pilot.press("1")  # different column → reset to ascending
        assert app.sort_column == 0
        col_asc = app.sort_ascending
        assert col_asc

        await pilot.press("q")


async def test_sort_column_ignored_when_no_target(snakemake_dir: Path, tmp_path: Path) -> None:
    """Digit keys have no effect when no sort target is selected."""
    app = SnakeseeApp(workflow_dir=tmp_path)
    async with app.run_test() as pilot:
        assert app.sort_table is None
        await pilot.press("2")
        assert app.sort_column == 0  # unchanged
        await pilot.press("q")


async def test_sort_column_capped_for_completions(snakemake_dir: Path, tmp_path: Path) -> None:
    """Pressing '4' when sorting completions (max col 3) has no effect."""
    app = SnakeseeApp(workflow_dir=tmp_path)
    async with app.run_test() as pilot:
        await pilot.press("s")  # running
        await pilot.press("s")  # completions
        assert app.sort_table == "completions"
        assert app.sort_column == 0
        await pilot.press("4")  # col 3 >= max_col 3 → rejected
        assert app.sort_column == 0
        await pilot.press("3")  # col 2 < max_col 3 → accepted
        assert app.sort_column == 2
        await pilot.press("q")


async def test_sort_column_capped_for_pending(snakemake_dir: Path, tmp_path: Path) -> None:
    """Pressing '3' or '4' when sorting pending (max col 2) has no effect."""
    app = SnakeseeApp(workflow_dir=tmp_path)
    async with app.run_test() as pilot:
        await pilot.press("s")  # running
        await pilot.press("s")  # completions
        await pilot.press("s")  # pending
        assert app.sort_table == "pending"
        assert app.sort_column == 0
        await pilot.press("3")  # col 2 >= max_col 2 → rejected
        assert app.sort_column == 0
        await pilot.press("4")  # col 3 >= max_col 2 → rejected
        assert app.sort_column == 0
        await pilot.press("2")  # col 1 < max_col 2 → accepted
        assert app.sort_column == 1
        await pilot.press("q")


async def test_backward_cycle_full_loop(snakemake_dir: Path, tmp_path: Path) -> None:
    """N 'S' presses cycle back to None from None, where N = number of sort targets."""
    from snakesee.tui.app import _SORT_CYCLE

    app = SnakeseeApp(workflow_dir=tmp_path)
    async with app.run_test() as pilot:
        assert app.sort_table is None
        for _ in range(len(_SORT_CYCLE)):
            await pilot.press("S")
        assert app.sort_table is None
        await pilot.press("q")
