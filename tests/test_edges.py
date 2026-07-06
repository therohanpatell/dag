"""Unit tests for the sequence / parallel / join edge builders."""
from composer_flow.models.workflow import DagNode, Edge, Workflow
from composer_flow.webui import edges


def wf_with(n: int) -> Workflow:
    w = Workflow(name="t")
    w.nodes = [DagNode(id=chr(ord("a") + i), dag_id=f"dag_{chr(ord('a') + i)}")
               for i in range(n)]
    return w


def pairs(w: Workflow) -> set[tuple[str, str]]:
    return {(e.source, e.target) for e in w.edges}


def test_chain_sequence():
    w = wf_with(4)
    created, skipped = edges.chain(w, ["a", "b", "c", "d"])
    assert created == 3 and not skipped
    assert pairs(w) == {("a", "b"), ("b", "c"), ("c", "d")}


def test_fan_out_parallel():
    w = wf_with(4)
    created, skipped = edges.fan_out(w, "a", ["b", "c", "d"])
    assert created == 3 and not skipped
    assert pairs(w) == {("a", "b"), ("a", "c"), ("a", "d")}


def test_fan_in_join():
    w = wf_with(4)
    created, skipped = edges.fan_in(w, ["a", "b", "c"], "d")
    assert created == 3 and not skipped
    assert pairs(w) == {("a", "d"), ("b", "d"), ("c", "d")}


def test_diamond_via_builders():
    w = wf_with(4)  # a,b,c,d
    edges.fan_out(w, "a", ["b", "c"])
    edges.fan_in(w, ["b", "c"], "d")
    assert pairs(w) == {("a", "b"), ("a", "c"), ("b", "d"), ("c", "d")}


def test_cycle_is_skipped():
    w = wf_with(3)
    edges.chain(w, ["a", "b", "c"])  # a->b->c
    created, skipped = edges.try_add_edge(w, "c", "a"), None
    # try_add_edge returns a reason string, not a count
    reason = edges.try_add_edge(w, "c", "a")
    assert "cycle" in reason.lower()
    assert ("c", "a") not in pairs(w)


def test_duplicate_is_noop():
    w = wf_with(2)
    edges.try_add_edge(w, "a", "b")
    reason = edges.try_add_edge(w, "a", "b")
    assert reason == ""
    assert len(w.edges) == 1


def test_self_loop_rejected():
    w = wf_with(1)
    reason = edges.try_add_edge(w, "a", "a")
    assert "itself" in reason.lower()
    assert not w.edges


def test_chain_skips_cycle_link():
    w = wf_with(3)
    edges.chain(w, ["a", "b"])          # a->b
    created, skipped = edges.chain(w, ["b", "a"])  # b->a would cycle
    assert created == 0 and len(skipped) == 1
    assert "cycle" in skipped[0].lower()


def test_remove_and_clear():
    w = wf_with(3)
    edges.chain(w, ["a", "b", "c"])
    target = w.edges[0]
    edges.remove_edge(w, target.id)
    assert target.id not in {e.id for e in w.edges}
    edges.clear_edges(w)
    assert w.edges == []
