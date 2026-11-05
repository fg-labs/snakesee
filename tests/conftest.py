"""Shared fixtures."""

from __future__ import annotations

import os
import sys
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


FAKE_BJOBS = """\
import json, sys
ids = [a for a in sys.argv[1:] if a.isdigit()]
records = []
for i in ids:
    if i == "99999999":
        records.append({"JOBID": i, "ERROR": f"Job <{i}> is not found"})
    elif int(i) % 2:  # odd ids are running
        records.append({"JOBID": i, "STAT": "RUN", "MAX_MEM": "21.5 Gbytes",
                        "AVG_MEM": "7.8 Gbytes", "MEMLIMIT": "23 G", "RUN_TIME": "60 second(s)"})
    else:
        records.append({"JOBID": i, "STAT": "PEND", "MAX_MEM": "", "AVG_MEM": "",
                        "MEMLIMIT": "23 G", "RUN_TIME": "0 second(s)"})
print(json.dumps({"COMMAND": "bjobs", "JOBS": len(records), "RECORDS": records}))
"""


@pytest.fixture
def fake_bjobs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A fake ``bjobs`` on PATH: odd job ids RUN using 21.5G of 23G, even ids PEND."""
    bindir = tmp_path / "bjobs-bin"
    bindir.mkdir()
    script = bindir / "bjobs"
    script.write_text(f"#!{sys.executable}\n" + FAKE_BJOBS)
    script.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bindir}:{os.environ.get('PATH', '')}")
    return script
