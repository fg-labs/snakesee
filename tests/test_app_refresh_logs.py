"""Tests for refresh-rate and log-navigation bindings in SnakeseeApp."""

from pathlib import Path

import pytest

from snakesee.constants import DEFAULT_REFRESH_RATE
from snakesee.constants import MAX_REFRESH_RATE
from snakesee.constants import MIN_REFRESH_RATE
from snakesee.tui.app import SnakeseeApp


@pytest.mark.parametrize(
    "key,delta",
    [
        ("plus", 0.5),
        ("equal", 0.5),
        ("minus", -0.5),
        ("greater_than_sign", 5.0),
        ("full_stop", 5.0),
        ("less_than_sign", -5.0),
        ("comma", -5.0),
    ],
)
async def test_refresh_rate_keys(
    snakemake_dir: Path, tmp_path: Path, key: str, delta: float
) -> None:
    """Each rate key nudges refresh_rate by the expected delta."""
    app = SnakeseeApp(workflow_dir=tmp_path, refresh_rate=10.0)
    async with app.run_test() as pilot:
        await pilot.press(key)
        await pilot.pause()
        assert app.refresh_rate == pytest.approx(10.0 + delta)
        await pilot.press("q")


async def test_refresh_rate_clamped_min(snakemake_dir: Path, tmp_path: Path) -> None:
    """minus at MIN_REFRESH_RATE does not go below the minimum."""
    app = SnakeseeApp(workflow_dir=tmp_path, refresh_rate=MIN_REFRESH_RATE)
    async with app.run_test() as pilot:
        await pilot.press("minus")
        await pilot.pause()
        assert app.refresh_rate == MIN_REFRESH_RATE
        await pilot.press("q")


async def test_refresh_rate_clamped_max(snakemake_dir: Path, tmp_path: Path) -> None:
    """plus at MAX_REFRESH_RATE does not exceed the maximum."""
    app = SnakeseeApp(workflow_dir=tmp_path, refresh_rate=MAX_REFRESH_RATE)
    async with app.run_test() as pilot:
        await pilot.press("plus")
        await pilot.pause()
        assert app.refresh_rate == MAX_REFRESH_RATE
        await pilot.press("q")


def test_construct_with_non_default_refresh_rate_no_event_loop(
    snakemake_dir: Path, tmp_path: Path
) -> None:
    """Constructing the app with a non-default refresh rate must not require a running loop.

    The CLI builds ``SnakeseeApp`` synchronously (before ``run()`` starts the event
    loop) and passes ``refresh`` defaulting to 2.0, which differs from the
    ``refresh_rate`` reactive's default. A premature reactive watcher that called
    ``set_interval`` here would raise ``RuntimeError: no running event loop``. This is
    deliberately a synchronous (non-async) test so no event loop is running, matching
    the real CLI path.
    """
    assert DEFAULT_REFRESH_RATE != 2.0, "test assumes 2.0 differs from the reactive default"
    app = SnakeseeApp(workflow_dir=tmp_path, refresh_rate=2.0)
    assert app.refresh_rate == pytest.approx(2.0)


async def test_refresh_rate_reset(snakemake_dir: Path, tmp_path: Path) -> None:
    """Pressing '0' resets refresh_rate to DEFAULT_REFRESH_RATE."""
    app = SnakeseeApp(workflow_dir=tmp_path, refresh_rate=15.0)
    async with app.run_test() as pilot:
        await pilot.press("0")
        await pilot.pause()
        assert app.refresh_rate == DEFAULT_REFRESH_RATE
        await pilot.press("q")


async def test_refresh_rate_min_via_G(snakemake_dir: Path, tmp_path: Path) -> None:
    """Pressing 'G' sets refresh_rate to MIN_REFRESH_RATE."""
    app = SnakeseeApp(workflow_dir=tmp_path, refresh_rate=10.0)
    async with app.run_test() as pilot:
        await pilot.press("G")
        await pilot.pause()
        assert app.refresh_rate == MIN_REFRESH_RATE
        await pilot.press("q")


async def test_log_navigation(snakemake_dir: Path, tmp_path: Path) -> None:
    """[ moves to older log; ] moves back to newer."""
    log_dir = tmp_path / ".snakemake" / "log"
    log_dir.mkdir(parents=True, exist_ok=True)
    (log_dir / "2024-01-01T000000.snakemake.log").touch()
    (log_dir / "2024-01-02T000000.snakemake.log").touch()

    app = SnakeseeApp(workflow_dir=tmp_path)
    async with app.run_test() as pilot:
        assert app.current_log_index == 0
        await pilot.press("left_square_bracket")
        await pilot.pause()
        assert app.current_log_index == 1
        await pilot.press("right_square_bracket")
        await pilot.pause()
        assert app.current_log_index == 0
        await pilot.press("q")


async def test_log_navigation_with_no_logs_stays_at_zero(
    snakemake_dir: Path, tmp_path: Path
) -> None:
    """Pressing [ on an empty workflow keeps current_log_index at 0 (no negative index)."""
    app = SnakeseeApp(workflow_dir=tmp_path)
    async with app.run_test() as pilot:
        assert app.current_log_index == 0
        await pilot.press("left_square_bracket")
        await pilot.pause()
        assert app.current_log_index == 0
        await pilot.press("left_curly_bracket")
        await pilot.pause()
        assert app.current_log_index == 0
        await pilot.press("q")


async def test_log_navigation_curly_steps_5(snakemake_dir: Path, tmp_path: Path) -> None:
    """{ moves 5 logs older; } moves 5 logs newer."""
    log_dir = tmp_path / ".snakemake" / "log"
    log_dir.mkdir(parents=True, exist_ok=True)
    for d in range(1, 8):  # 7 logs
        (log_dir / f"2024-01-{d:02d}T000000.snakemake.log").touch()

    app = SnakeseeApp(workflow_dir=tmp_path)
    async with app.run_test() as pilot:
        assert app.current_log_index == 0
        await pilot.press("left_curly_bracket")
        await pilot.pause()
        assert app.current_log_index == 5
        await pilot.press("right_curly_bracket")
        await pilot.pause()
        assert app.current_log_index == 0
        await pilot.press("q")
