"""Demo runner: spawn Snakemake on a bundled workflow and launch the TUI."""

from __future__ import annotations

import importlib.resources
import logging
import os
import shutil
import signal
import subprocess
import sys
import time
from datetime import datetime
from datetime import timezone
from importlib.util import find_spec
from pathlib import Path
from typing import Literal

from snakesee.tui.app import SnakeseeApp

logger = logging.getLogger(__name__)

Duration = Literal["short", "medium", "long"]

# (sleep_min, sleep_max) per duration, in seconds.
_DURATION_RANGES: dict[Duration, tuple[int, int]] = {
    "short": (1, 5),
    "medium": (5, 15),
    "long": (15, 45),
}

_LOGGER_PLUGIN_MODULE = "snakemake_logger_plugin_snakesee"


def _cache_root() -> Path:
    """Return ${XDG_CACHE_HOME:-~/.cache}/snakesee/demo/."""
    xdg = os.environ.get("XDG_CACHE_HOME")
    base = Path(xdg) if xdg else Path.home() / ".cache"
    return base / "snakesee" / "demo"


def _utc_timestamp() -> str:
    """Return UTC timestamp in YYYYMMDDTHHMMSS_ffffffZ form (filesystem-safe).

    Includes microseconds so two demo runs started within the same second get
    distinct directories instead of silently mixing outputs into one run.
    """
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S_%fZ")


def _prune_old_runs(cache_root: Path, keep: int) -> None:
    """Delete all but the `keep` most recent demo dirs."""
    if not cache_root.is_dir():
        return
    runs = sorted(
        (p for p in cache_root.iterdir() if p.is_dir()),
        key=lambda p: p.name,
        reverse=True,
    )
    for old in runs[keep:]:
        shutil.rmtree(old, ignore_errors=True)


def _copy_workflow(target: Path) -> None:
    """Copy the bundled Snakefile and inputs/ into target."""
    target.mkdir(parents=True, exist_ok=True)
    (target / "inputs").mkdir(exist_ok=True)
    snakefile = importlib.resources.files("snakesee.demo") / "Snakefile"
    target.joinpath("Snakefile").write_text(snakefile.read_text())
    inputs_pkg = importlib.resources.files("snakesee.demo.inputs")
    for sample in ("A", "B", "C", "D", "E"):
        src = inputs_pkg / f"{sample}.txt"
        target.joinpath("inputs", f"{sample}.txt").write_text(src.read_text())


def _logger_plugin_available() -> bool:
    return find_spec(_LOGGER_PLUGIN_MODULE) is not None


def _build_snakemake_argv(
    cores: int,
    sleep_min: int,
    sleep_max: int,
    use_logger_plugin: bool,
) -> list[str]:
    argv = ["snakemake", "all", "--cores", str(cores), "--keep-going"]
    if use_logger_plugin:
        argv += ["--logger", "snakesee"]
    argv += [
        "--config",
        f"sleep_min={sleep_min}",
        f"sleep_max={sleep_max}",
    ]
    return argv


def _terminate(proc: subprocess.Popen[bytes]) -> None:
    """SIGTERM with 3s grace; SIGKILL on timeout. Best-effort — never re-raises."""
    if proc.poll() is not None:
        return
    proc.send_signal(signal.SIGTERM)
    try:
        proc.wait(timeout=3)
        return
    except subprocess.TimeoutExpired:
        logger.warning("snakemake did not exit within 3s of SIGTERM; sending SIGKILL")
    proc.kill()
    try:
        proc.wait(timeout=3)
    except subprocess.TimeoutExpired:
        logger.warning("snakemake did not exit within 3s of SIGKILL; giving up")


def run_demo(
    cores: int = 4,
    duration: Duration = "short",
    keep_runs: int = 5,
    no_tui: bool = False,
    clean: bool = False,
) -> int:
    """Run the bundled demo workflow with snakesee.

    Args:
        cores: Number of cores for `snakemake --cores`.
        duration: Sleep-range preset. Scales the workflow's per-job duration.
        keep_runs: Maximum demo dirs to keep in the cache; older are pruned at startup.
        no_tui: If True, run snakemake to completion and skip launching the TUI.
                Used by tests and CI; not advertised to end users.
        clean: If True, delete every demo dir in the cache and exit without
               running anything.

    Returns:
        0 on clean exit, non-zero if snakemake errored.
    """
    cache_root = _cache_root()

    if clean:
        if cache_root.is_dir():
            shutil.rmtree(cache_root)
            print(f"Removed {cache_root}", file=sys.stderr)
        return 0

    cache_root.mkdir(parents=True, exist_ok=True)
    _prune_old_runs(cache_root, keep_runs)

    demo_dir = cache_root / _utc_timestamp()
    _copy_workflow(demo_dir)

    sleep_min, sleep_max = _DURATION_RANGES[duration]
    use_plugin = _logger_plugin_available()
    if not use_plugin:
        print(
            "[snakesee demo] snakemake-logger-plugin-snakesee not installed; "
            "falling back to log-file polling. Install the plugin for realtime events.",
            file=sys.stderr,
        )

    argv = _build_snakemake_argv(cores, sleep_min, sleep_max, use_plugin)

    snakemake = shutil.which(argv[0])
    if snakemake is None:
        print(
            "[snakesee demo] could not find `snakemake` on PATH; "
            "install snakemake (e.g. `pixi add snakemake` or `pip install snakemake`).",
            file=sys.stderr,
        )
        return 127
    argv[0] = snakemake

    proc: subprocess.Popen[bytes] = subprocess.Popen(
        argv,
        cwd=demo_dir,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    if no_tui:
        # Wait synchronously and report Snakemake's exit code.
        try:
            return proc.wait()
        except KeyboardInterrupt:
            _terminate(proc)
            return 130

    # Closing the terminal sends SIGHUP, which by default kills Python without
    # running `finally` blocks — leaving the snakemake child as an orphan.
    # Install a handler so the subprocess goes down with the parent.  SIGHUP
    # is Unix-only; on Windows, terminal close raises CTRL_CLOSE_EVENT instead
    # and there is nothing to install here.
    has_sighup = hasattr(signal, "SIGHUP")
    prev_sighup = signal.getsignal(signal.SIGHUP) if has_sighup else None

    if has_sighup:

        def _on_sighup(signum: int, frame: object) -> None:
            _terminate(proc)
            raise SystemExit(128 + signum)

        signal.signal(signal.SIGHUP, _on_sighup)

    try:
        # Give snakemake a moment to write the first event/log so the TUI has
        # something to show on initial mount.
        time.sleep(0.5)
        app = SnakeseeApp(workflow_dir=demo_dir)
        app.run()
    finally:
        # If snakemake already exited on its own (e.g. workflow finished), capture
        # its rc; otherwise terminate it (returncode is None for "still running").
        completed_rc = proc.poll()
        if completed_rc is None:
            _terminate(proc)
        if has_sighup and prev_sighup is not None:
            signal.signal(signal.SIGHUP, prev_sighup)

    return completed_rc if completed_rc is not None else 0
