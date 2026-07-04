"""Unit tests for the dependency-graph algorithms."""
from composer_flow.core import graph as g
from composer_flow.models.execution import NodeStatus
from composer_flow.models.workflow import DagNode, Edge, Workflow


def make_workflow(node_ids: list[str], edge_pairs: list[tuple[str, str]]) -> Workflow:
    wf = Workflow(name="test")
    wf.nodes = [DagNode(id=n, dag_id=f"dag_{n}") for n in node_ids]
    wf.edges = [Edge(source=s, target=t) for s, t in edge_pairs]
    return wf


def test_topological_levels_linear():
    wf = make_workflow(["a", "b", "c"], [("a", "b"), ("b", "c")])
    assert g.topological_levels(wf) == [["a"], ["b"], ["c"]]


def test_topological_levels_diamond_parallel_wave():
    wf = make_workflow(["a", "b", "c", "d"], [("a", "b"), ("a", "c"), ("b", "d"), ("c", "d")])
    levels = g.topological_levels(wf)
    assert levels[0] == ["a"]
    assert sorted(levels[1]) == ["b", "c"]  # parallel wave
    assert levels[2] == ["d"]


def test_cycle_detection():
    wf = make_workflow(["a", "b", "c"], [("a", "b"), ("b", "c"), ("c", "a")])
    assert g.find_cycle(wf) is not None
    try:
        g.topological_levels(wf)
        assert False, "expected ValueError"
    except ValueError:
        pass


def test_would_create_cycle():
    wf = make_workflow(["a", "b", "c"], [("a", "b"), ("b", "c")])
    assert g.would_create_cycle(wf, "c", "a") is True
    assert g.would_create_cycle(wf, "a", "c") is False
    assert g.would_create_cycle(wf, "a", "a") is True


def test_descendants():
    wf = make_workflow(["a", "b", "c", "d"], [("a", "b"), ("b", "c"), ("b", "d")])
    assert g.descendants(wf, "a") == {"b", "c", "d"}
    assert g.descendants(wf, "b") == {"c", "d"}
    assert g.descendants(wf, "c") == set()


def test_ready_nodes_respects_dependencies():
    wf = make_workflow(["a", "b", "c", "d"], [("a", "b"), ("a", "c"), ("b", "d"), ("c", "d")])
    statuses = {n.id: NodeStatus.PENDING for n in wf.nodes}
    assert g.ready_nodes(wf, statuses) == ["a"]
    statuses["a"] = NodeStatus.SUCCESS
    assert sorted(g.ready_nodes(wf, statuses)) == ["b", "c"]
    statuses["b"] = NodeStatus.SUCCESS
    assert g.ready_nodes(wf, statuses) == ["c"]  # d still blocked by c
    statuses["c"] = NodeStatus.SUCCESS
    # b already SUCCESS, so only d is pending+ready now
    assert g.ready_nodes(wf, statuses) == ["d"]


def test_ready_nodes_blocked_by_failure():
    wf = make_workflow(["a", "b"], [("a", "b")])
    statuses = {"a": NodeStatus.FAILED, "b": NodeStatus.PENDING}
    assert g.ready_nodes(wf, statuses) == []


def test_validate_missing_dag_id_and_cycle():
    wf = make_workflow(["a", "b"], [("a", "b"), ("b", "a")])
    wf.nodes[0].dag_id = ""
    issues = g.validate(wf)
    messages = " | ".join(i.message for i in issues)
    assert any(i.is_error for i in issues)
    assert "no DAG ID" in messages
    assert "Cycle detected" in messages


def test_validate_orphan_warning():
    wf = make_workflow(["a", "b", "c"], [("a", "b")])
    issues = g.validate(wf)
    assert any("not connected" in i.message for i in issues)
    assert not any(i.is_error for i in issues)


def test_validate_empty_workflow():
    issues = g.validate(Workflow(name="empty"))
    assert issues and issues[0].is_error
