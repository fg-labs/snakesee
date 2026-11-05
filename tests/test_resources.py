from __future__ import annotations

from pathlib import Path

import pytest

from snakesee.resources import parse_bjobs_json
from snakesee.resources import parse_lsf_report
from snakesee.resources import parse_mem_mb
from snakesee.resources import query_bjobs
from snakesee.resources import read_lsf_report

from .conftest import DATA


@pytest.mark.parametrize(
    ("text", "mb"),
    [
        ("9.9 Gbytes", 9.9 * 1024),
        ("23 G", 23 * 1024),
        ("10912 MB", 10912.0),
        ("9619.73 MB", 9619.73),
        ("512 Mbytes", 512.0),
        ("2048 Kbytes", 2.0),
        ("1 T", 1024.0**2),
        ("2048", 2.0),  # bare number: KB
        ("-", None),
        ("", None),
        (None, None),
    ],
)
def test_parse_mem_mb(text: str | None, mb: float | None) -> None:
    assert parse_mem_mb(text) == pytest.approx(mb) if mb is not None else parse_mem_mb(text) is None


BJOBS_JSON = """{
  "COMMAND":"bjobs",
  "JOBS":3,
  "RECORDS":[
    {
      "JOBID":"101874519",
      "STAT":"RUN",
      "MAX_MEM":"9.9 Gbytes",
      "AVG_MEM":"7.8 Gbytes",
      "MEMLIMIT":"23 G",
      "RUN_TIME":"115 second(s)"
    },
    {
      "JOBID":"101874520",
      "STAT":"PEND",
      "MAX_MEM":"",
      "AVG_MEM":"",
      "MEMLIMIT":"23 G",
      "RUN_TIME":"0 second(s)"
    },
    {
      "JOBID":"99999999",
      "ERROR":"Job <99999999> is not found"
    }
  ]
}"""


def test_parse_bjobs_json() -> None:
    res = parse_bjobs_json(BJOBS_JSON)
    assert set(res) == {"101874519", "101874520"}
    run = res["101874519"]
    assert run.stat == "RUN"
    assert run.max_mem_mb == pytest.approx(9.9 * 1024)
    assert run.mem_limit_mb == pytest.approx(23 * 1024)
    assert run.run_time_s == 115
    assert run.mem_fraction == pytest.approx(9.9 / 23)
    assert not run.final
    pend = res["101874520"]
    assert pend.stat == "PEND" and pend.max_mem_mb is None and pend.mem_fraction is None


def test_parse_bjobs_garbage() -> None:
    assert parse_bjobs_json("Job <1> is not found") == {}
    assert parse_bjobs_json("") == {}


def test_lsf_report_done() -> None:
    res = parse_lsf_report((DATA / "lsf_done.out").read_text())
    assert res is not None
    assert res.final
    assert res.max_mem_mb == 10912
    assert res.avg_mem_mb == pytest.approx(9619.73)
    assert res.mem_limit_mb == 24000
    assert res.cpu_time_s == pytest.approx(702.93)
    assert res.run_time_s == 498
    assert res.max_threads == 53
    assert res.lsf_note == ""


def test_lsf_report_memlimit() -> None:
    res = parse_lsf_report((DATA / "lsf_memlimit.out").read_text())
    assert res is not None
    assert res.lsf_note == "TERM_MEMLIMIT: job killed after reaching LSF memory usage limit."
    assert res.mem_fraction == pytest.approx(23552 / 24000)


def test_no_report() -> None:
    assert parse_lsf_report("just some job output\n") is None


def test_read_report_at_end_of_large_file(tmp_path: Path) -> None:
    f = tmp_path / "x.out"
    report = (DATA / "lsf_done.out").read_text().split("The output (if any)")[0]
    f.write_text("noise line\n" * 5000 + report)
    res = read_lsf_report(f)
    assert res is not None and res.max_mem_mb == 10912


def test_query_bjobs_with_fake(fake_bjobs: Path) -> None:
    res = query_bjobs(["101", "102", "99999999"])
    assert res["101"].stat == "RUN" and res["102"].stat == "PEND"
    assert "99999999" not in res
