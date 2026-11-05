from __future__ import annotations

from pathlib import Path

from snakesee.tail import FileTailer
from snakesee.tail import read_tail


def test_partial_lines_are_held_back(tmp_path: Path) -> None:
    f = tmp_path / "x.log"
    f.write_bytes(b"one\ntw")
    t = FileTailer(f)
    assert t.read_new_lines() == (["one"], False, False)
    with f.open("ab") as fh:
        fh.write(b"o\nthree\n")
    assert t.read_new_lines() == (["two", "three"], False, False)
    assert t.read_new_lines() == ([], False, False)


def test_truncation_resets(tmp_path: Path) -> None:
    f = tmp_path / "x.log"
    f.write_text("a\nb\nc\n")
    t = FileTailer(f)
    t.read_new_lines()
    f.write_text("z\n")
    lines, reset, _ = t.read_new_lines()
    assert reset and lines == ["z"]


def test_chunked_reads_report_more(tmp_path: Path) -> None:
    f = tmp_path / "x.log"
    f.write_text("".join(f"line {i}\n" for i in range(100)))
    t = FileTailer(f, max_chunk=64)
    out: list[str] = []
    while True:
        lines, _, more = t.read_new_lines()
        out += lines
        if not more:
            break
    assert out == [f"line {i}" for i in range(100)]


def test_missing_file(tmp_path: Path) -> None:
    assert FileTailer(tmp_path / "nope").read_new_lines() == ([], False, False)


def test_read_tail(tmp_path: Path) -> None:
    f = tmp_path / "x.log"
    f.write_text("".join(f"line {i}\n" for i in range(1000)))
    lines, truncated = read_tail(f, max_bytes=100)
    assert truncated
    assert lines[-1] == "line 999"
    assert all(ln.startswith("line ") for ln in lines)
    assert read_tail(f) == ([f"line {i}" for i in range(1000)], False)
