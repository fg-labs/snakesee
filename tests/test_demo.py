"""Tests for `snakesee demo`."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from snakesee.demo.runner import _cache_root
from snakesee.demo.runner import build_snakemake_argv
from snakesee.demo.runner import copy_workflow
from snakesee.demo.runner import run_demo


class TestSnakefileDryRun:
    def test_dry_run_succeeds(self, tmp_path: Path) -> None:
        """The bundled Snakefile parses cleanly and resolves the DAG."""
        copy_workflow(tmp_path)
        snakemake = shutil.which("snakemake")
        if snakemake is None:
            pytest.skip("snakemake executable not on PATH")
        result = subprocess.run(
            [snakemake, "--dry-run", "all"],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, (
            f"snakemake --dry-run failed:\nstdout:\n{result.stdout}\n\nstderr:\n{result.stderr}"
        )
        # Sanity: the dry-run summary should mention all six rule names.
        for rule in ("download", "qc", "align", "dedup", "call_variants", "report"):
            assert rule in result.stdout, f"Rule '{rule}' missing from dry-run summary"


class TestArgvBuilder:
    def test_argv_includes_logger_when_available(self) -> None:
        argv = build_snakemake_argv(cores=2, sleep_min=1, sleep_max=3, use_logger_plugin=True)
        assert "--logger" in argv
        assert "snakesee" in argv
        assert "--cores" in argv
        assert "2" in argv

    def test_argv_omits_logger_when_unavailable(self) -> None:
        argv = build_snakemake_argv(cores=2, sleep_min=1, sleep_max=3, use_logger_plugin=False)
        assert "--logger" not in argv

    def test_argv_passes_sleep_config(self) -> None:
        argv = build_snakemake_argv(cores=4, sleep_min=2, sleep_max=8, use_logger_plugin=False)
        assert "sleep_min=2" in argv
        assert "sleep_max=8" in argv

    def test_argv_target_precedes_config(self) -> None:
        argv = build_snakemake_argv(cores=2, sleep_min=1, sleep_max=3, use_logger_plugin=False)
        assert "all" in argv
        assert argv.index("all") < argv.index("--config")


class TestEndToEndShortRun:
    """Exercise the full pipeline end-to-end via --no-tui.

    This test takes ~30-60s on a typical machine.
    """

    @pytest.mark.timeout(120)
    def test_short_run_completes(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        if shutil.which("snakemake") is None:
            pytest.skip("snakemake executable not on PATH")
        # Redirect the cache root to a tmp dir so the test is hermetic.
        monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))

        rc = run_demo(cores=8, duration="short", keep_runs=5, no_tui=True)
        # Expect non-zero because of the intentional dedup[E] failure.
        assert rc != 0

        # Verify the demo dir was created and contains expected outputs for A-D.
        demo_root = _cache_root()
        assert demo_root.is_dir(), f"Cache root {demo_root} not created"
        runs = sorted(p for p in demo_root.iterdir() if p.is_dir())
        assert len(runs) == 1, f"Expected 1 demo run dir, got {len(runs)}"
        run = runs[0]

        # Successful samples must have a call_variants output.
        for sample in ("A", "B", "C", "D"):
            out = run / "output" / "call_variants" / f"{sample}.txt"
            assert out.is_file(), f"Missing expected output for sample {sample}: {out}"

        # Sample E intentionally fails at dedup, so call_variants[E] should not exist.
        assert not (run / "output" / "call_variants" / "E.txt").is_file()


class TestCleanFlag:
    def test_clean_removes_cache(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
        cache = _cache_root()
        cache.mkdir(parents=True)
        (cache / "20240101T000000Z").mkdir()
        (cache / "20240102T000000Z").mkdir()

        rc = run_demo(clean=True)
        assert rc == 0
        assert not cache.is_dir()


class TestMissingSnakemake:
    def test_run_demo_exits_127_when_snakemake_missing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """If snakemake is not on PATH, run_demo returns 127 without raising."""
        monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
        monkeypatch.setattr("snakesee.demo.runner.shutil.which", lambda _name: None)

        rc = run_demo(cores=2, duration="short", keep_runs=1, no_tui=True)
        assert rc == 127


class TestPruneOldRuns:
    def test_prune_keeps_n_newest(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        from snakesee.demo.runner import _prune_old_runs

        monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
        cache = _cache_root()
        cache.mkdir(parents=True)
        for ts in ("20240101T000000Z", "20240102T000000Z", "20240103T000000Z", "20240104T000000Z"):
            (cache / ts).mkdir()

        _prune_old_runs(cache, keep=2)

        remaining = sorted(p.name for p in cache.iterdir() if p.is_dir())
        assert remaining == ["20240103T000000Z", "20240104T000000Z"]
