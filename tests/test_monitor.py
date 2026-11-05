from __future__ import annotations

import os
from pathlib import Path

from snakesee.model import JobStatus
from snakesee.monitor import WorkflowMonitor


def test_follows_appends_and_new_logs(tmp_path: Path) -> None:
    log_dir = tmp_path / ".snakemake" / "log"
    log_dir.mkdir(parents=True)
    first = log_dir / "2026-01-01T000000.000000.snakemake.log"
    first.write_text("rule a:\n    jobid: 1\n\n")
    m = WorkflowMonitor(tmp_path)

    u = m.poll()
    assert u.switched and u.log_path == first
    assert [j.jobid for j in u.jobs] == [1]

    assert m.poll().jobs == []  # nothing new

    with first.open("a") as fh:
        fh.write("Finished job 1.\n")
    u = m.poll()
    assert [(j.jobid, j.status) for j in u.jobs] == [(1, JobStatus.DONE)]

    second = log_dir / "2026-01-02T000000.000000.snakemake.log"
    second.write_text("rule b:\n    jobid: 9\n\n")
    # Make sure the directory mtime visibly changes even on coarse-mtime filesystems.
    st = os.stat(log_dir)
    os.utime(log_dir, (st.st_atime, st.st_mtime + 5))
    u = m.poll()
    assert u.switched and u.log_path == second
    assert [j.jobid for j in u.jobs] == [9]


def test_no_logs(tmp_path: Path) -> None:
    u = WorkflowMonitor(tmp_path).poll()
    assert "No Snakemake logs" in u.message
