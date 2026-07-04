"""Dependency-graph algorithms: validation, cycle detection, topological
levels, descendants and ready-set computation.

Pure functions over the Workflow model — fully unit-testable without Qt/DB.

Parallelism falls out of the graph structure automatically: at any moment the
"ready set" is every PENDING node whose predecessors have all SUCCEEDED, so
independent branches (e.g. B and C under A) become ready together and are
executed concurrently by the engine.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass

from composer_flow.models.execution import NodeStatus
from composer_flow.models.workflow import Workflow


@dataclass(frozen=True)
class ValidationIssue:
    level: str  # "error" | "warning"
    message: str

    @property
    def is_error(self) -> bool:
        return self.level == "error"


def adjacency(workflow: Workflow) -> tuple[dict[str, set[str]], dict[str, set[str]]]:
    """Return (successors, predecessors) keyed by node id."""
    succ: dict[str, set[str]] = {n.id: set() for n in workflow.nodes}
    pred: dict[str, set[str]] = {n.id: set() for n in workflow.nodes}
    for e in workflow.edges:
        if e.source in succ and e.target in pred:
            succ[e.source].add(e.target)
            pred[e.target].add(e.source)
    return succ, pred


def find_cycle(workflow: Workflow) -> list[str] | None:
    """Return one cycle as a list of node ids, or None (iterative DFS)."""
    succ, _ = adjacency(workflow)
    WHITE, GRAY, BLACK = 0, 1, 2
    color = {nid: WHITE for nid in succ}
    parent: dict[str, str] = {}

    for start in succ:
        if color[start] != WHITE:
            continue
        stack: list[tuple[str, iter]] = [(start, iter(sorted(succ[start])))]
        color[start] = GRAY
        while stack:
            node, it = stack[-1]
            advanced = False
            for nxt in it:
                if color[nxt] == WHITE:
                    color[nxt] = GRAY
                    parent[nxt] = node
                    stack.append((nxt, iter(sorted(succ[nxt]))))
                    advanced = True
                    break
                if color[nxt] == GRAY:  # back edge -> cycle
                    cycle = [nxt, node]
                    cur = node
                    while cur != nxt and cur in parent:
                        cur = parent[cur]
                        cycle.append(cur)
                    cycle.reverse()
                    return cycle
            if not advanced:
                color[node] = BLACK
                stack.pop()
    return None


def would_create_cycle(workflow: Workflow, source: str, target: str) -> bool:
    """True if adding edge source->target creates a cycle (target reaches source)."""
    if source == target:
        return True
    succ, _ = adjacency(workflow)
    seen, queue = {target}, deque([target])
    while queue:
        cur = queue.popleft()
        if cur == source:
            return True
        for nxt in succ.get(cur, ()):
            if nxt not in seen:
                seen.add(nxt)
                queue.append(nxt)
    return False


def topological_levels(workflow: Workflow) -> list[list[str]]:
    """Kahn's algorithm returning execution 'waves'. Nodes in the same level
    have no dependencies among each other and may run in parallel.
    Raises ValueError if the graph contains a cycle.
    """
    succ, pred = adjacency(workflow)
    indegree = {nid: len(ps) for nid, ps in pred.items()}
    level = [nid for nid, d in sorted(indegree.items()) if d == 0]
    levels: list[list[str]] = []
    visited = 0
    while level:
        levels.append(level)
        visited += len(level)
        nxt: list[str] = []
        for nid in level:
            for s in sorted(succ[nid]):
                indegree[s] -= 1
                if indegree[s] == 0:
                    nxt.append(s)
        level = nxt
    if visited != len(workflow.nodes):
        raise ValueError("Workflow graph contains a cycle")
    return levels


def descendants(workflow: Workflow, node_id: str) -> set[str]:
    """All nodes downstream of node_id (used to skip after a failure)."""
    succ, _ = adjacency(workflow)
    seen: set[str] = set()
    queue = deque(succ.get(node_id, ()))
    while queue:
        cur = queue.popleft()
        if cur in seen:
            continue
        seen.add(cur)
        queue.extend(succ.get(cur, ()))
    return seen


def ready_nodes(workflow: Workflow, statuses: dict[str, NodeStatus]) -> list[str]:
    """PENDING nodes whose predecessors have all SUCCEEDED."""
    _, pred = adjacency(workflow)
    out: list[str] = []
    for n in workflow.nodes:
        if statuses.get(n.id) != NodeStatus.PENDING:
            continue
        if all(statuses.get(p) == NodeStatus.SUCCESS for p in pred[n.id]):
            out.append(n.id)
    return out


def validate(workflow: Workflow) -> list[ValidationIssue]:
    """Pre-execution validation: cycles, missing DAG IDs, orphan nodes,
    duplicate edges, self-loops, dangling edges, duplicate DAG ids.
    """
    issues: list[ValidationIssue] = []
    if not workflow.nodes:
        issues.append(ValidationIssue("error", "Workflow has no DAG nodes."))
        return issues

    node_ids = {n.id for n in workflow.nodes}
    for n in workflow.nodes:
        if not n.dag_id.strip():
            issues.append(
                ValidationIssue("error", f"Node '{n.display_name()}' has no DAG ID.")
            )
        elif any(c.isspace() for c in n.dag_id.strip()):
            issues.append(
                ValidationIssue("error", f"DAG ID '{n.dag_id}' contains whitespace.")
            )

    seen_pairs: set[tuple[str, str]] = set()
    for e in workflow.edges:
        if e.source not in node_ids or e.target not in node_ids:
            issues.append(ValidationIssue("error", "Edge references a missing node."))
            continue
        if e.source == e.target:
            src = workflow.node_by_id(e.source)
            issues.append(
                ValidationIssue(
                    "error", f"Self-dependency on '{src.display_name() if src else e.source}'."
                )
            )
        pair = (e.source, e.target)
        if pair in seen_pairs:
            issues.append(ValidationIssue("warning", "Duplicate edge between two nodes."))
        seen_pairs.add(pair)

    cycle = find_cycle(workflow)
    if cycle:
        names = " -> ".join(
            (workflow.node_by_id(nid).display_name() if workflow.node_by_id(nid) else nid)
            for nid in cycle
        )
        issues.append(ValidationIssue("error", f"Cycle detected: {names}"))

    if len(workflow.nodes) > 1:
        connected = {e.source for e in workflow.edges} | {e.target for e in workflow.edges}
        for n in workflow.nodes:
            if n.id not in connected:
                issues.append(
                    ValidationIssue(
                        "warning",
                        f"Node '{n.display_name()}' is not connected to any other node "
                        "(it will run immediately in parallel with the first wave).",
                    )
                )

    dag_counts: dict[str, int] = {}
    for n in workflow.nodes:
        key = n.dag_id.strip()
        if key:
            dag_counts[key] = dag_counts.get(key, 0) + 1
    for dag_id, count in dag_counts.items():
        if count > 1:
            issues.append(
                ValidationIssue(
                    "warning",
                    f"DAG '{dag_id}' appears {count} times — runs get distinct run-ids, "
                    "but verify this is intentional.",
                )
            )
    return issues
