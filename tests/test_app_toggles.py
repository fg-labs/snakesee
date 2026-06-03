"""Tests for toggle bindings (p/e/w/a/r) in SnakeseeApp."""

import dataclasses
from pathlib import Path

from snakesee.models import TimeEstimate
from snakesee.models import WorkflowProgress
from snakesee.tui.accessibility import ACCESSIBLE_CONFIG
from snakesee.tui.accessibility import DEFAULT_CONFIG
from snakesee.tui.app import SnakeseeApp


async def test_p_toggles_paused(snakemake_dir: Path, tmp_path: Path) -> None:
    """Pressing 'p' toggles the paused reactive between False and True."""
    app = SnakeseeApp(workflow_dir=tmp_path)
    async with app.run_test() as pilot:
        assert app.paused == False  # noqa: E712
        await pilot.press("p")
        assert app.paused == True  # noqa: E712
        await pilot.press("p")
        assert app.paused == False  # noqa: E712
        await pilot.press("q")


async def test_a_toggles_accessibility(snakemake_dir: Path, tmp_path: Path) -> None:
    """Pressing 'a' cycles between DEFAULT_CONFIG and ACCESSIBLE_CONFIG."""
    app = SnakeseeApp(workflow_dir=tmp_path)
    async with app.run_test() as pilot:
        assert app._accessibility_config is DEFAULT_CONFIG
        await pilot.press("a")
        assert app._accessibility_config is ACCESSIBLE_CONFIG
        await pilot.press("a")
        assert app._accessibility_config is DEFAULT_CONFIG
        await pilot.press("q")


async def test_a_restores_constructor_accessibility_config(
    snakemake_dir: Path, tmp_path: Path
) -> None:
    """Toggling accessibility off restores a constructor-supplied config, not DEFAULT_CONFIG."""
    custom = dataclasses.replace(DEFAULT_CONFIG, show_legend=True)
    app = SnakeseeApp(workflow_dir=tmp_path, accessibility_config=custom)
    async with app.run_test() as pilot:
        assert app._accessibility_config is custom
        await pilot.press("a")
        assert app._accessibility_config is ACCESSIBLE_CONFIG
        await pilot.press("a")
        assert app._accessibility_config is custom
        await pilot.press("q")


async def test_e_toggles_estimation(snakemake_dir: Path, tmp_path: Path) -> None:
    """Pressing 'e' toggles use_estimation on the data source."""
    app = SnakeseeApp(workflow_dir=tmp_path)
    async with app.run_test() as pilot:
        initial = app._data.use_estimation
        await pilot.press("e")
        await pilot.pause()
        assert app._data.use_estimation == (not initial)
        await pilot.press("q")


async def test_w_toggles_wildcard(snakemake_dir: Path, tmp_path: Path) -> None:
    """Pressing 'w' toggles _use_wildcard_conditioning on the data source."""
    app = SnakeseeApp(workflow_dir=tmp_path)
    async with app.run_test() as pilot:
        initial = app._data._use_wildcard_conditioning
        await pilot.press("w")
        await pilot.pause()
        assert app._data._use_wildcard_conditioning == (not initial)
        await pilot.press("q")


async def test_r_does_not_crash(snakemake_dir: Path, tmp_path: Path) -> None:
    """Pressing 'r' triggers a force refresh without crashing."""
    app = SnakeseeApp(workflow_dir=tmp_path)
    async with app.run_test() as pilot:
        await pilot.press("r")
        await pilot.pause()
        await pilot.press("q")


async def test_force_refresh_runs_while_paused(snakemake_dir: Path, tmp_path: Path) -> None:
    """Pressing 'r' while paused still polls the data source (force refresh ignores paused)."""
    app = SnakeseeApp(workflow_dir=tmp_path)
    async with app.run_test() as pilot:
        await pilot.press("p")
        await pilot.pause()
        assert app.paused
        call_count = 0
        original = app._refresh_panels

        def counting_refresh(ignore_pause: bool = False) -> None:
            nonlocal call_count
            call_count += 1
            original(ignore_pause=ignore_pause)

        app._refresh_panels = counting_refresh  # type: ignore[method-assign]
        await pilot.press("r")
        await pilot.pause()
        assert call_count >= 1, "force refresh did not invoke _refresh_panels while paused"
        await pilot.press("q")


async def test_toggle_pause_repaints_immediately(snakemake_dir: Path, tmp_path: Path) -> None:
    """Pressing 'p' repaints right away so the PAUSED indicator shows without waiting.

    A long refresh_rate keeps the interval timer from polling during the test, so
    any poll observed after the patch must come from the toggle itself.
    """
    app = SnakeseeApp(workflow_dir=tmp_path, refresh_rate=60.0)
    async with app.run_test() as pilot:
        await pilot.pause()
        poll_count = 0
        original = app._data.poll_state

        def counting_poll() -> tuple[WorkflowProgress, TimeEstimate | None]:
            nonlocal poll_count
            poll_count += 1
            return original()

        app._data.poll_state = counting_poll  # type: ignore[method-assign]
        await pilot.press("p")  # pause: header must repaint to show PAUSED
        await pilot.pause()
        assert app.paused
        assert poll_count >= 1, "pausing did not repaint the panels"
        poll_count = 0
        await pilot.press("p")  # unpause: repaint immediately, not at the next tick
        await pilot.pause()
        assert not app.paused
        assert poll_count >= 1, "unpausing did not repaint the panels"
        await pilot.press("q")


async def test_hard_refresh_repaints_while_paused(snakemake_dir: Path, tmp_path: Path) -> None:
    """Ctrl+R re-inits the estimator and repaints even while auto-refresh is paused."""
    app = SnakeseeApp(workflow_dir=tmp_path, refresh_rate=60.0)
    async with app.run_test() as pilot:
        await pilot.press("p")
        await pilot.pause()
        assert app.paused
        poll_count = 0
        original = app._data.poll_state

        def counting_poll() -> tuple[WorkflowProgress, TimeEstimate | None]:
            nonlocal poll_count
            poll_count += 1
            return original()

        app._data.poll_state = counting_poll  # type: ignore[method-assign]
        await pilot.press("ctrl+r")
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert poll_count >= 1, "hard refresh did not repaint the panels while paused"
        await pilot.press("q")


async def test_user_actions_repaint_while_paused(snakemake_dir: Path, tmp_path: Path) -> None:
    """Explicit user actions (accessibility toggle, sort cycle) repaint even while paused.

    Pause gates only the auto-refresh timer; every keystroke-driven redraw should
    still render immediately.
    """
    app = SnakeseeApp(workflow_dir=tmp_path, refresh_rate=60.0)
    async with app.run_test() as pilot:
        await pilot.press("p")
        await pilot.pause()
        assert app.paused
        poll_count = 0
        original = app._data.poll_state

        def counting_poll() -> tuple[WorkflowProgress, TimeEstimate | None]:
            nonlocal poll_count
            poll_count += 1
            return original()

        app._data.poll_state = counting_poll  # type: ignore[method-assign]
        await pilot.press("a")  # accessibility toggle
        await pilot.pause()
        assert poll_count >= 1, "accessibility toggle did not repaint while paused"
        poll_count = 0
        await pilot.press("s")  # sort cycle
        await pilot.pause()
        assert poll_count >= 1, "sort cycle did not repaint while paused"
        await pilot.press("q")
