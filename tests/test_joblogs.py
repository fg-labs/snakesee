from __future__ import annotations

from pathlib import Path

from snakesee.joblogs import LogSource
from snakesee.joblogs import SourceKind
from snakesee.joblogs import fetch
from snakesee.joblogs import sources_for
from snakesee.joblogs import stderr_path_for
from snakesee.model import Job


def test_stderr_path() -> None:
    assert stderr_path_for(Path("/a/b/x.out")) == Path("/a/b/x.err")
    assert stderr_path_for(Path("/a/b/x.log")) == Path("/a/b/x.log.err")


def test_sources_for_lsf_job(tmp_path: Path) -> None:
    job = Job(
        jobid=1,
        rule="r",
        external_id="99",
        cluster_log=".snakemake/lsf_logs/rule_r/u.out",
        rule_logs=["logs/r.log"],
    )
    sources = sources_for(job, tmp_path)
    assert [s.kind for s in sources] == [
        SourceKind.STDOUT,
        SourceKind.STDERR,
        SourceKind.BPEEK,
        SourceKind.RULE_LOG,
    ]
    assert sources[0].path == tmp_path / ".snakemake/lsf_logs/rule_r/u.out"
    assert sources[1].path == tmp_path / ".snakemake/lsf_logs/rule_r/u.err"
    assert sources[3].path == tmp_path / "logs/r.log"


def test_fetch_missing_file_explains(tmp_path: Path) -> None:
    lines = fetch(LogSource(SourceKind.STDERR, "stderr", path=tmp_path / "x.err"))
    assert "does not exist" in lines[0]
    assert "bpeek" in lines[1]


def test_fetch_file(tmp_path: Path) -> None:
    f = tmp_path / "x.err"
    f.write_text("Traceback\nValueError: bad\n")
    assert fetch(LogSource(SourceKind.STDERR, "stderr", path=f)) == [
        "Traceback",
        "ValueError: bad",
    ]


def test_fetch_bpeek(fake_bpeek: Path) -> None:
    lines = fetch(LogSource(SourceKind.BPEEK, "bpeek", external_id="12345"))
    assert lines == ["<< output from stdout >>", "peeking at job 12345"]
