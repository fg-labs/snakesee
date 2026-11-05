"""Shared fixtures."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

DATA = Path(__file__).parent / "data"
LSF_LOG = DATA / "lsf_run.snakemake.log"
FIXTURE_WORKFLOW = "/data/proj"


@pytest.fixture
def workflow(tmp_path: Path) -> Path:
    """A workflow dir containing the LSF fixture log, with paths rewritten to ``tmp_path``."""
    log_dir = tmp_path / ".snakemake" / "log"
    log_dir.mkdir(parents=True)
    text = LSF_LOG.read_text().replace(FIXTURE_WORKFLOW, str(tmp_path))
    (log_dir / "2026-11-05T121544.990726.snakemake.log").write_text(text)
    return tmp_path


@pytest.fixture
def fake_bpeek(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Put a fake ``bpeek`` on PATH that echoes its argument."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    script = bindir / "bpeek"
    script.write_text('#!/bin/sh\necho "<< output from stdout >>"\necho "peeking at job $1"\n')
    script.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bindir}:{os.environ.get('PATH', '')}")
    return script
