"""Tests for filter Input widget in SnakeseeApp."""

from pathlib import Path

from textual.widgets import Input

from snakesee.tui.app import SnakeseeApp


async def test_slash_focuses_filter_input(snakemake_dir: Path, tmp_path: Path) -> None:
    """Pressing / focuses the filter Input and adds the -active class; escape blurs it."""
    app = SnakeseeApp(workflow_dir=tmp_path)
    async with app.run_test() as pilot:
        f = app.query_one("#filter", Input)
        assert app.focused is not f
        await pilot.press("slash")
        await pilot.pause()
        assert app.focused is f
        assert f.has_class("-active")
        await pilot.press("escape")
        await pilot.pause()
        assert app.focused is not f
        await pilot.press("q")


async def test_filter_submit_sets_filter_text(snakemake_dir: Path, tmp_path: Path) -> None:
    """Typing into the filter Input and pressing enter sets app.filter_text."""
    app = SnakeseeApp(workflow_dir=tmp_path)
    async with app.run_test() as pilot:
        await pilot.press("slash")
        await pilot.pause()
        for ch in "trim":
            await pilot.press(ch)
        await pilot.press("enter")
        await pilot.pause()
        assert app.filter_text == "trim"
        await pilot.press("q")


async def test_filter_clear(snakemake_dir: Path, tmp_path: Path) -> None:
    """Escape (with no Input focused) clears the active filter_text."""
    app = SnakeseeApp(workflow_dir=tmp_path)
    async with app.run_test() as pilot:
        await pilot.press("slash")
        await pilot.pause()
        for ch in "abc":
            await pilot.press(ch)
        await pilot.press("enter")
        await pilot.pause()
        assert app.filter_text == "abc"
        await pilot.press("escape")
        await pilot.pause()
        assert app.filter_text is None
        await pilot.press("q")


async def test_filter_empty_submit_clears(snakemake_dir: Path, tmp_path: Path) -> None:
    """Submitting an empty filter Input leaves filter_text as None."""
    app = SnakeseeApp(workflow_dir=tmp_path)
    async with app.run_test() as pilot:
        await pilot.press("slash")
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        assert app.filter_text is None
        await pilot.press("q")


async def test_escape_returns_to_latest_log(snakemake_dir: Path, tmp_path: Path) -> None:
    """Escape (with no Input focused) clears filter AND returns to the latest log."""
    log_dir = tmp_path / ".snakemake" / "log"
    log_dir.mkdir(parents=True, exist_ok=True)
    (log_dir / "2024-01-01T000000.snakemake.log").touch()
    (log_dir / "2024-01-02T000000.snakemake.log").touch()

    app = SnakeseeApp(workflow_dir=tmp_path)
    async with app.run_test() as pilot:
        # Step into a historical log and apply a filter.
        await pilot.press("left_square_bracket")
        await pilot.pause()
        assert app.current_log_index == 1
        await pilot.press("slash")
        await pilot.pause()
        for ch in "abc":
            await pilot.press(ch)
        await pilot.press("enter")
        await pilot.pause()
        assert app.filter_text == "abc"
        # Escape clears both the filter and the historical-log offset.
        await pilot.press("escape")
        await pilot.pause()
        assert app.filter_text is None
        assert app.current_log_index == 0
        await pilot.press("q")


async def test_next_match_on_incomplete_table_is_noop(snakemake_dir: Path, tmp_path: Path) -> None:
    """n/N on the #incomplete table (single 'Output File' column) must not crash.

    #incomplete has no Rule column, so a global n/N press while it holds focus
    previously indexed a non-existent column and raised IndexError. It must now be
    a safe no-op (regression test for the _jump_match bounds bug).
    """
    from textual.widgets import DataTable

    app = SnakeseeApp(workflow_dir=tmp_path)
    async with app.run_test() as pilot:
        app.paused = True  # keep auto-refresh from wiping the manually added row
        app.filter_text = "anything"
        await pilot.pause()
        table = app.query_one("#incomplete", DataTable)
        table.add_row("results/E.txt")
        table.focus()
        await pilot.pause()
        # Should not raise; cursor stays put because there is no Rule column to match.
        app.action_next_match()
        app.action_prev_match()
        await pilot.pause()
        assert table.cursor_row == 0
        await pilot.press("q")


async def test_next_match_uses_rule_column_zero_for_pending(
    snakemake_dir: Path, tmp_path: Path
) -> None:
    """On #pending (Rule at column 0), n jumps the cursor to the matching rule row."""
    from textual.widgets import DataTable

    app = SnakeseeApp(workflow_dir=tmp_path)
    async with app.run_test() as pilot:
        app.paused = True
        app.filter_text = "sort"
        await pilot.pause()
        table = app.query_one("#pending", DataTable)
        table.add_row("align", "5")
        table.add_row("sort", "3")
        table.focus()
        await pilot.pause()
        app.action_next_match()
        await pilot.pause()
        assert table.cursor_row == 1  # matched the "sort" row at column 0
        await pilot.press("q")
