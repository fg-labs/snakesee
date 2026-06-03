"""Capture a product screenshot of the snakesee TUI mid-run.

Runs the bundled demo workflow under Snakemake in the background, drives the
Textual app headlessly via the test Pilot, waits until the run has a healthy
mix of completed / running / pending (and the intentional sample-E failure),
then exports an SVG screenshot.

Usage:
    uv run python docs/scripts/capture_screenshot.py docs/assets/screenshot.svg
"""

from __future__ import annotations

import asyncio
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from snakesee.demo.runner import build_snakemake_argv
from snakesee.demo.runner import copy_workflow
from snakesee.demo.runner import logger_plugin_available
from snakesee.tui.app import SnakeseeApp

# Leaf directory name for the demo run; surfaces in the TUI header so we want
# something that reads like a real project rather than a temp path.
WORKFLOW_NAME = "rna-seq-workflow"

# Terminal geometry for the export: wide enough for the full layout, tall
# enough to show several jobs per panel without scrolling.
COLUMNS = 120
ROWS = 40

# The TUI header middle-truncates the workflow path beyond ~`COLUMNS - 80`
# characters; keep the resolved demo path within this budget so the header in
# the screenshot reads cleanly instead of showing an elided temp path.
PATH_BUDGET = COLUMNS - 80

# Per-job sleep range (seconds). "medium"-ish so the run lingers in a
# partially-complete state long enough to capture a representative frame.
SLEEP_MIN = 4
SLEEP_MAX = 12


def _demo_dir() -> Path:
    """Pick a short-pathed directory for the demo workflow.

    Prefers the platform tempdir; falls back to ``/tmp`` when the platform
    tempdir would blow the header's truncation budget (e.g. macOS, where
    ``tempfile.gettempdir()`` is a long ``/var/folders/...`` path while
    ``/tmp`` resolves to a 29-character ``/private/tmp``).

    Returns:
        Directory (not yet created) to copy the demo workflow into.
    """
    candidates = [Path(tempfile.gettempdir()), Path("/tmp")]
    for base in candidates:
        demo_dir = base / WORKFLOW_NAME
        if base.is_dir() and len(str(demo_dir.resolve())) <= PATH_BUDGET:
            return demo_dir
    # No candidate fits; accept a truncated header rather than failing.
    return candidates[0] / WORKFLOW_NAME


async def _capture(proc: subprocess.Popen[bytes], workflow_dir: Path, out_path: Path) -> int:
    """Drive the TUI headlessly against a live run and export an SVG frame.

    Polls both the app state and the background Snakemake process. If the
    process exits before a representative frame is reached, returns its exit
    code immediately instead of waiting out the full timeout — otherwise a
    workflow that dies early would be masked as a generic "unrepresentative"
    failure. The SVG is written only when the target frame is reached, so a
    failed or timed-out run never leaves an unrepresentative artifact on disk.

    Args:
        proc: The running background Snakemake process to monitor.
        workflow_dir: Workflow directory the (already started) Snakemake run
            is executing in; the app polls its ``.snakemake/`` state.
        out_path: Path the SVG screenshot is written to (on success only).

    Returns:
        0 if the target job mix was reached and the SVG was written; the
        Snakemake exit code (or 1 if that code was 0) if the process exited
        before a representative frame; 1 if the wait cap elapsed first.
    """
    app = SnakeseeApp(workflow_dir=workflow_dir)
    async with app.run_test(size=(COLUMNS, ROWS)) as pilot:
        # Let the TUI poll the live .snakemake/ dir until the run reaches a
        # frame that exercises every panel: several completed, some running,
        # plenty pending, and the intentional sample-E failure. Cap the wait so
        # the capture can't hang if the workflow stalls.
        for _ in range(120):
            await pilot.pause()
            await asyncio.sleep(0.5)
            # Fail fast if the background workflow exited: with --keep-going it
            # ends non-zero once it has run everything, so reaching this point
            # means we were too slow to catch the mid-run frame (or the run
            # died outright). Either way, surface the real exit code rather
            # than waiting out the timeout and reporting a generic failure.
            returncode = proc.poll()
            if returncode is not None:
                print(
                    f"snakemake exited (code {returncode}) before a "
                    "representative frame was reached",
                    file=sys.stderr,
                )
                return returncode if returncode != 0 else 1
            poll = app.last_poll
            if poll is None:
                continue
            progress = poll[0]
            if progress.failed_jobs >= 1 and progress.completed_jobs >= 8:
                await pilot.pause()
                app.save_screenshot(str(out_path))
                return 0
    # Timed out before a representative frame; leave no artifact behind.
    print(
        f"warning: target job mix not reached before timeout; no screenshot "
        f"written to {out_path} — re-run, or inspect the workflow",
        file=sys.stderr,
    )
    return 1


def main() -> int:
    """Run the demo workflow, capture the screenshot, and clean up.

    Returns:
        0 on success; 2 on usage error; 127 if snakemake is missing; 1 if the
        capture timed out before reaching a representative frame; otherwise the
        Snakemake exit code if the run exited before a representative frame.
    """
    if len(sys.argv) < 2:
        print("usage: capture_screenshot.py OUTPUT.svg", file=sys.stderr)
        return 2
    out_path = Path(sys.argv[1]).resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)

    snakemake = shutil.which("snakemake")
    if snakemake is None:
        print("snakemake not on PATH", file=sys.stderr)
        return 127

    demo_dir = _demo_dir()
    if demo_dir.exists():
        shutil.rmtree(demo_dir)
    try:
        copy_workflow(demo_dir)
        argv = build_snakemake_argv(
            cores=4,
            sleep_min=SLEEP_MIN,
            sleep_max=SLEEP_MAX,
            use_logger_plugin=logger_plugin_available(),
        )
        argv[0] = snakemake
        proc = subprocess.Popen(
            argv, cwd=demo_dir, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
        try:
            # Give Snakemake a head start so the first jobs are already running.
            time.sleep(3)
            exit_code = asyncio.run(_capture(proc, demo_dir, out_path))
        finally:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
    finally:
        shutil.rmtree(demo_dir, ignore_errors=True)

    if exit_code == 0:
        print(f"wrote {out_path}")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
