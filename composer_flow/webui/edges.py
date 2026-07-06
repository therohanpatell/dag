"""Edge-building helpers for the UI — sequence, parallel and join wiring.

Pure functions over the Workflow model (no Streamlit import) so they are
unit-testable. Every builder refuses self-loops, duplicates and any edge that
would introduce a cycle, and reports what it skipped in human terms.
"""
from __future__ import annotations

from composer_flow.core import graph as g
from composer_flow.models.workflow import Edge, Workflow


def _name(wf: Workflow, node_id: str) -> str:
    node = wf.node_by_id(node_id)
    return node.display_name() if node else node_id


def try_add_edge(wf: Workflow, source: str, target: str) -> str:
    """Add one dependency source -> target. Returns '' on success or a reason."""
    if not source or not target or source == target:
        return f"{_name(wf, target)}: cannot depend on itself"
    if any(e.source == source and e.target == target for e in wf.edges):
        return ""  # already exists — treat as a no-op success
    if g.would_create_cycle(wf, source, target):
        return f"{_name(wf, source)} → {_name(wf, target)} (would create a cycle)"
    wf.edges.append(Edge(source=source, target=target))
    return ""


def chain(wf: Workflow, ordered_ids: list[str]) -> tuple[int, list[str]]:
    """Wire a sequence: ids[0] → ids[1] → … → ids[-1]."""
    created, skipped = 0, []
    for src, tgt in zip(ordered_ids, ordered_ids[1:]):
        before = len(wf.edges)
        err = try_add_edge(wf, src, tgt)
        if err:
            skipped.append(err)
        elif len(wf.edges) > before:
            created += 1
    return created, skipped


def fan_out(wf: Workflow, parent: str, children: list[str]) -> tuple[int, list[str]]:
    """Parallel branch: parent → each child (children run concurrently)."""
    created, skipped = 0, []
    for child in children:
        before = len(wf.edges)
        err = try_add_edge(wf, parent, child)
        if err:
            skipped.append(err)
        elif len(wf.edges) > before:
            created += 1
    return created, skipped


def fan_in(wf: Workflow, parents: list[str], child: str) -> tuple[int, list[str]]:
    """Join: each parent → child (child waits for all parents)."""
    created, skipped = 0, []
    for parent in parents:
        before = len(wf.edges)
        err = try_add_edge(wf, parent, child)
        if err:
            skipped.append(err)
        elif len(wf.edges) > before:
            created += 1
    return created, skipped


def remove_edge(wf: Workflow, edge_id: str) -> None:
    wf.edges = [e for e in wf.edges if e.id != edge_id]


def clear_edges(wf: Workflow) -> None:
    wf.edges = []
