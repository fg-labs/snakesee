"""Render the rule DAG as a ``git log --graph``-style lane diagram.

Rules flow top (upstream) to bottom (downstream)::

    ●    download
    ├─╮
    ● │  align
    │ ●  count
    ├─╯
    ●    merge

Edges are only those observed in this run's log, so rules with no submitted jobs
yet have no known dependencies and are listed separately below the graph.
"""

from __future__ import annotations

import heapq
from collections.abc import Iterable

from rich.text import Text

from snakesee.model import RuleInfo

Row = tuple[str, str | None]
"""(graph characters, rule name for node rows / None for connector rows)."""


def topo_order(
    nodes: list[str], edges: Iterable[tuple[str, str]], key: dict[str, int]
) -> list[str]:
    """Kahn's algorithm; among ready nodes pick the smallest ``key`` (stable, readable order).

    Any cycle (shouldn't happen) is broken by emitting the remaining nodes in key order.
    """
    node_set = set(nodes)
    children: dict[str, list[str]] = {n: [] for n in nodes}
    indegree = dict.fromkeys(nodes, 0)
    for up, down in edges:
        if up in node_set and down in node_set:
            children[up].append(down)
            indegree[down] += 1
    ready = [(key[n], n) for n in nodes if indegree[n] == 0]
    heapq.heapify(ready)
    order: list[str] = []
    done: set[str] = set()
    while len(order) < len(nodes):
        if not ready:  # cycle: force the next node out
            n = min((n for n in nodes if n not in done), key=lambda x: key[x])
            ready.append((key[n], n))
        _, n = heapq.heappop(ready)
        if n in done:
            continue
        done.add(n)
        order.append(n)
        for c in children[n]:
            indegree[c] -= 1
            if indegree[c] == 0 and c not in done:
                heapq.heappush(ready, (key[c], c))
    return order


def _connector(lanes: list[str | None], col: int, others: list[int], ends: str, mid: str) -> str:
    """A row joining lane ``col`` to the lanes in ``others`` (all to the right of ``col``)."""
    hi = max(others)
    other_set = set(others)
    chars = [" "] * (2 * len(lanes) - 1)
    for i, lane in enumerate(lanes):
        x = 2 * i
        if i == col:
            chars[x] = "├"
        elif i in other_set:
            chars[x] = ends if i == hi else mid
        elif col < i < hi:
            chars[x] = "┼" if lane is not None else "─"
        elif lane is not None:
            chars[x] = "│"
        if col <= i < hi:
            chars[x + 1] = "─"
    return "".join(chars)


def layout(nodes: list[str], edges: Iterable[tuple[str, str]], key: dict[str, int]) -> list[Row]:
    """Lay out ``nodes`` as rows of lane characters, git-graph style."""
    edge_list = list(edges)
    order = topo_order(nodes, edge_list, key)
    position = {n: i for i, n in enumerate(order)}
    children: dict[str, list[str]] = {n: [] for n in nodes}
    for up, down in edge_list:
        if up in children and down in children:
            children[up].append(down)
    for kids in children.values():
        kids.sort(key=position.__getitem__)

    lanes: list[str | None] = []
    rows: list[Row] = []
    for n in order:
        targets = [i for i, lane in enumerate(lanes) if lane == n]
        if targets:
            col = targets[0]
            if len(targets) > 1:  # several parents converge
                rows.append((_connector(lanes, col, targets[1:], "╯", "┴"), None))
                for t in targets[1:]:
                    lanes[t] = None
                while lanes[-1] is None:
                    lanes.pop()
        else:  # no known parents: start a lane in the first free column
            col = next((i for i, lane in enumerate(lanes) if lane is None), len(lanes))
            if col == len(lanes):
                lanes.append(None)
            lanes[col] = n
        rows.append(("".join(_node_row(lanes, col)), n))

        kids = children[n]
        if not kids:
            lanes[col] = None
        else:
            lanes[col] = kids[0]
            new: list[int] = []
            for kid in kids[1:]:  # fork: each extra child gets a lane to the right
                c = next((i for i in range(col + 1, len(lanes)) if lanes[i] is None), len(lanes))
                if c == len(lanes):
                    lanes.append(None)
                lanes[c] = kid
                new.append(c)
            if new:
                rows.append((_connector(lanes, col, new, "╮", "┬"), None))
        while lanes and lanes[-1] is None:
            lanes.pop()
    return rows


def _node_row(lanes: list[str | None], col: int) -> list[str]:
    chars = [" "] * (2 * len(lanes) - 1)
    for i, lane in enumerate(lanes):
        if i == col:
            chars[2 * i] = "●"
        elif lane is not None:
            chars[2 * i] = "│"
    return chars


def rule_style(rule: RuleInfo) -> str:
    """Colour for a rule's node: failed > running > all done > partly done > not started."""
    if rule.failed:
        return "bold red"
    if rule.active:
        return "bold yellow"
    if rule.seen and not rule.pending:
        return "green"
    if rule.seen:
        return "cyan"
    return "dim"


def _counts(rule: RuleInfo) -> Text:
    t = Text()
    total = rule.total if rule.total is not None else rule.seen
    t.append(f"{rule.done}/{total} done", style="green" if rule.done == total else "")
    if rule.active:
        t.append(f"  {rule.active} running", style="yellow")
    if rule.failed:
        t.append(f"  {rule.failed} failed", style="bold red")
    if rule.pending:
        t.append(f"  {rule.pending} pending", style="dim")
    return t


def render(rules: tuple[RuleInfo, ...], edges: frozenset[tuple[str, str]]) -> Text:
    """The full Rules view: the graph of started rules, then rules with no jobs yet."""
    by_name = {r.name: r for r in rules}
    started = [r for r in rules if r.first_seen is not None]
    not_started = sorted((r for r in rules if r.first_seen is None), key=lambda r: r.stats_index)
    out = Text(no_wrap=True, overflow="ellipsis")
    if not rules:
        out.append("No rules yet (waiting for the Job stats table).", style="dim")
        return out

    name_width = max(len(r.name) for r in rules)
    graph_width = 1
    if started:
        key = {r.name: r.first_seen or 0 for r in started}
        rows = layout([r.name for r in started], edges, key)
        graph_width = max(len(g) for g, _ in rows)
        for graph, name in rows:
            if name is None:
                out.append(graph, style="dim")
                out.append("\n")
                continue
            rule = by_name[name]
            style = rule_style(rule)
            for ch in graph:
                out.append(ch, style=style if ch == "●" else "dim")
            out.append(" " * (graph_width - len(graph) + 2))
            out.append(name.ljust(name_width), style=style)
            out.append("  ")
            out.append_text(_counts(rule))
            out.append("\n")

    if not_started:
        if started:
            out.append("\n")
        out.append("Not started (dependencies unknown until a job is submitted)\n", style="dim")
        for rule in not_started:
            out.append("○".ljust(graph_width + 2), style="dim")
            out.append(rule.name.ljust(name_width), style="dim")
            out.append("  ")
            out.append_text(_counts(rule))
            out.append("\n")
    out.rstrip()
    return out
