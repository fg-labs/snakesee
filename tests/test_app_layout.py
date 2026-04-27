"""Tests for layout mode cycling in SnakeseeApp."""

from pathlib import Path

from snakesee.tui.app import LayoutMode
from snakesee.tui.app import SnakeseeApp


async def test_tab_cycles_layout(snakemake_dir: Path, tmp_path: Path) -> None:
    """Pressing Tab cycles to the next LayoutMode and updates the CSS class."""
    app = SnakeseeApp(workflow_dir=tmp_path)
    async with app.run_test() as pilot:
        modes = list(LayoutMode)
        initial_idx = modes.index(app.layout_mode)
        await pilot.press("tab")
        await pilot.pause()
        new_idx = modes.index(app.layout_mode)
        assert new_idx == (initial_idx + 1) % len(modes)
        assert app.has_class(f"-{app.layout_mode.value}")
        await pilot.press("q")


async def test_layout_initial_class(snakemake_dir: Path, tmp_path: Path) -> None:
    """The CSS class for the initial layout mode is set on mount."""
    app = SnakeseeApp(workflow_dir=tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        assert app.has_class(f"-{app.layout_mode.value}")
        await pilot.press("q")


async def test_tab_full_cycle(snakemake_dir: Path, tmp_path: Path) -> None:
    """N tab presses cycle back to the original mode where N = len(LayoutMode)."""
    app = SnakeseeApp(workflow_dir=tmp_path)
    async with app.run_test() as pilot:
        original = app.layout_mode
        for _ in range(len(LayoutMode)):
            await pilot.press("tab")
            await pilot.pause()
        assert app.layout_mode == original
        await pilot.press("q")
