from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from pathlib import Path

from textual.pilot import Pilot
from textual.widgets import DataTable
from textual.widgets import Log
from textual.widgets import TabbedContent

from snakesee.monitor import Update
from snakesee.tui.app import SnakeseeApp
from snakesee.tui.log_screen import LogScreen


async def wait_for(pilot: Pilot[None], cond: Callable[[], bool], timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not cond():
        if time.monotonic() > deadline:
            raise AssertionError("condition not met in time")
        await pilot.pause(0.02)


def rows(app: SnakeseeApp, table_id: str) -> int:
    return app.query_one(f"#{table_id}", DataTable).row_count


async def test_tables_populate(workflow: Path) -> None:
    app = SnakeseeApp(workflow)
    async with app.run_test(size=(160, 40)) as pilot:
        await wait_for(pilot, lambda: rows(app, "done") == 1)
        assert rows(app, "active") == 2
        assert rows(app, "failed") == 2
        await pilot.press("2")
        assert app.query_one(TabbedContent).active == "tab-failed"


async def test_open_stderr_of_failed_job(workflow: Path) -> None:
    app = SnakeseeApp(workflow)
    async with app.run_test(size=(160, 40)) as pilot:
        await wait_for(pilot, lambda: rows(app, "failed") == 2)
        await pilot.press("2")
        job = app._selected_job()
        assert job is not None and job.cluster_log is not None
        err = Path(job.cluster_log).with_suffix(".err")
        err.parent.mkdir(parents=True)
        err.write_text("Traceback (most recent call last):\nMemoryError\n")
        await pilot.press("e")
        assert isinstance(app.screen, LogScreen)
        log = app.screen.query_one(Log)
        await wait_for(pilot, lambda: "MemoryError" in log.lines)
        await pilot.press("escape")
        assert not isinstance(app.screen, LogScreen)


async def test_enter_on_active_job_uses_bpeek(workflow: Path, fake_bpeek: Path) -> None:
    app = SnakeseeApp(workflow)
    async with app.run_test(size=(160, 40)) as pilot:
        await wait_for(pilot, lambda: rows(app, "active") == 2)
        await pilot.press("enter")
        assert isinstance(app.screen, LogScreen)
        log = app.screen.query_one(Log)
        await wait_for(pilot, lambda: any("peeking at job 1018677" in ln for ln in log.lines))


async def test_ctrl_z_is_bound_to_suspend(workflow: Path) -> None:
    app = SnakeseeApp(workflow)
    called: list[bool] = []
    app.action_suspend_process = lambda: called.append(True)  # type: ignore[method-assign]
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.press("ctrl+z")
        assert called == [True]
        await wait_for(pilot, lambda: rows(app, "active") == 2)
        await pilot.press("enter")
        assert isinstance(app.screen, LogScreen)
        await pilot.press("ctrl+z")  # must also work from the log viewer
        assert called == [True, True]


async def test_ui_stays_responsive_when_filesystem_is_slow(workflow: Path) -> None:
    """A poll that blocks for seconds must not delay key handling."""
    app = SnakeseeApp(workflow)
    real_poll = app.monitor.poll

    def slow_poll() -> Update:
        time.sleep(3.0)
        return real_poll()

    app.monitor.poll = slow_poll  # type: ignore[method-assign]
    handled: list[float] = []
    show_tab = app.action_show_tab

    def record(table_id: str) -> None:
        handled.append(time.monotonic())
        show_tab(table_id)

    app.action_show_tab = record  # type: ignore[method-assign]
    async with app.run_test(size=(160, 40)) as pilot:
        await asyncio.sleep(0.1)  # worker is now stuck inside slow_poll
        start = time.monotonic()
        await pilot.press("3")
        assert app.query_one(TabbedContent).active == "tab-done"
        assert handled and handled[0] - start < 0.1


async def test_less_keys_in_job_table(tmp_path: Path) -> None:
    log_dir = tmp_path / ".snakemake" / "log"
    log_dir.mkdir(parents=True)
    blocks = "".join(f"rule r:\n    jobid: {i}\n" for i in range(200))
    (log_dir / "2026-01-01T000000.000000.snakemake.log").write_text(blocks + "\n")
    app = SnakeseeApp(tmp_path)
    async with app.run_test(size=(120, 30)) as pilot:
        await wait_for(pilot, lambda: rows(app, "active") == 200)
        table = app.query_one("#active", DataTable)
        await pilot.press("j", "j")
        assert table.cursor_row == 2
        await pilot.press("k")
        assert table.cursor_row == 1
        await pilot.press("ctrl+d")
        half = table.cursor_row - 1
        assert half >= 5
        await pilot.press("ctrl+u")
        assert table.cursor_row == 1
        await pilot.press("G")
        assert table.cursor_row == 199
        await pilot.press("g")
        assert table.cursor_row == 0


async def open_log_with(pilot: Pilot[None], app: SnakeseeApp, lines: list[str]) -> Log:
    await wait_for(pilot, lambda: rows(app, "failed") == 2)
    await pilot.press("2")
    job = app._selected_job()
    assert job is not None and job.cluster_log is not None
    out = Path(job.cluster_log)
    out.parent.mkdir(parents=True)
    out.write_text("\n".join(lines) + "\n")
    await pilot.press("o")
    log = app.screen.query_one(Log)
    await wait_for(pilot, lambda: lines[-1] in log.lines)
    return log


async def test_less_keys_in_log_view(workflow: Path) -> None:
    app = SnakeseeApp(workflow)
    async with app.run_test(size=(120, 30)) as pilot:
        log = await open_log_with(pilot, app, [f"line {i}" for i in range(500)])
        await pilot.press("g")
        assert log.scroll_y == 0
        await pilot.press("j", "j", "j")
        assert log.scroll_y == 3
        await pilot.press("k")
        assert log.scroll_y == 2
        await pilot.press("ctrl+d")
        assert log.scroll_y > 6
        await pilot.press("ctrl+u")
        assert log.scroll_y == 2
        await pilot.press("G")
        assert log.scroll_y == log.max_scroll_y


async def test_mouse_select_and_ctrl_c_copies(workflow: Path) -> None:
    app = SnakeseeApp(workflow)
    copied: list[str] = []
    app.copy_to_clipboard = copied.append  # type: ignore[method-assign,assignment]
    async with app.run_test(size=(120, 30)) as pilot:
        log = await open_log_with(pilot, app, ["MemoryError: oops", "second line"])
        await pilot.press("g")
        await pilot.mouse_down(log, offset=(0, 0))
        await pilot.hover(log, offset=(11, 0))
        await pilot.mouse_up(log, offset=(11, 0))
        await pilot.press("ctrl+c")
        assert copied and copied[0].startswith("MemoryError")
        assert app.is_running  # ctrl+c must not quit


async def test_ctrl_c_does_not_quit(workflow: Path) -> None:
    app = SnakeseeApp(workflow)
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.press("ctrl+c")
        await pilot.pause()
        assert app.is_running
