from __future__ import annotations

from snakesee.model import RuleInfo
from snakesee.tui.rulegraph import layout
from snakesee.tui.rulegraph import render
from snakesee.tui.rulegraph import rule_style


def draw(nodes: list[str], edges: list[tuple[str, str]]) -> list[str]:
    rows = layout(nodes, edges, {n: i for i, n in enumerate(nodes)})
    return [f"{g} {n}".rstrip() if n else g.rstrip() for g, n in rows]


def test_diamond() -> None:
    assert draw(list("abcd"), [("a", "b"), ("a", "c"), ("b", "d"), ("c", "d")]) == [
        "● a",
        "├─╮",
        "● │ b",
        "│ ● c",
        "├─╯",
        "● d",
    ]


def test_fan_in_and_crossing() -> None:
    nodes = ["dl", "align", "count", "qc", "merge", "all"]
    edges = [
        ("dl", "align"),
        ("dl", "qc"),
        ("align", "count"),
        ("align", "merge"),
        ("count", "merge"),
        ("qc", "all"),
        ("merge", "all"),
    ]
    assert draw(nodes, edges) == [
        "● dl",
        "├─╮",
        "● │ align",
        "├─┼─╮",
        "● │ │ count",
        "│ ● │ qc",
        "├─┼─╯",
        "● │ merge",
        "├─╯",
        "● all",
    ]


def test_independent_sources_merge() -> None:
    assert draw(["x", "y", "all"], [("x", "all"), ("y", "all")]) == [
        "● x",
        "│ ● y",
        "├─╯",
        "● all",
    ]


def test_topological_even_if_first_seen_order_disagrees() -> None:
    rows = layout(["b", "a"], [("a", "b")], {"b": 0, "a": 1})
    assert [n for _, n in rows if n] == ["a", "b"]


def test_cycle_does_not_hang() -> None:
    rows = layout(["a", "b"], [("a", "b"), ("b", "a")], {"a": 0, "b": 1})
    assert sorted(n for _, n in rows if n) == ["a", "b"]


def test_render_lists_not_started_rules() -> None:
    rules = (
        RuleInfo("fetch", total=2, done=2, first_seen=0),
        RuleInfo("align", total=2, active=1, failed=1, first_seen=1),
        RuleInfo("merge", total=1, stats_index=2),
    )
    text = render(rules, frozenset({("fetch", "align")})).plain
    assert "fetch  2/2 done" in text
    assert "align  0/2 done  1 running  1 failed" in text
    assert "Not started" in text
    assert "○  merge  0/1 done  1 pending" in text


def test_rule_style_priority() -> None:
    assert rule_style(RuleInfo("r", total=3, active=1, failed=1, first_seen=0)) == "bold red"
    assert rule_style(RuleInfo("r", total=3, active=1, first_seen=0)) == "bold yellow"
    assert rule_style(RuleInfo("r", total=1, done=1, first_seen=0)) == "green"
    assert rule_style(RuleInfo("r", total=3)) == "dim"
