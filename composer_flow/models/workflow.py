"""Domain models for workflows: nodes, edges and the workflow aggregate.

Plain dataclasses - no Qt or DB dependencies - so the core and services layers
stay framework-agnostic (Dependency Inversion).
"""
from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone


def new_id() -> str:
    return uuid.uuid4().hex


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class DagNode:
    """One DAG node in the visual workflow graph."""

    id: str = field(default_factory=new_id)
    dag_id: str = ""
    run_name: str = ""  # optional label; embedded in generated run-id
    params: dict[str, str] = field(default_factory=dict)
    x: float = 0.0
    y: float = 0.0

    def conf_json(self) -> str:
        """Compact JSON string passed to `--conf`."""
        return json.dumps(self.params, separators=(",", ":"), ensure_ascii=False)

    def display_name(self) -> str:
        return self.run_name or self.dag_id or "(unnamed)"

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "dag_id": self.dag_id,
            "run_name": self.run_name,
            "params": dict(self.params),
            "x": self.x,
            "y": self.y,
        }

    @staticmethod
    def from_dict(d: dict) -> "DagNode":
        return DagNode(
            id=str(d.get("id") or new_id()),
            dag_id=str(d.get("dag_id", "")),
            run_name=str(d.get("run_name", "")),
            params={str(k): str(v) for k, v in dict(d.get("params") or {}).items()},
            x=float(d.get("x", 0.0)),
            y=float(d.get("y", 0.0)),
        )


@dataclass
class Edge:
    """Directed dependency: target runs only after source succeeds."""

    id: str = field(default_factory=new_id)
    source: str = ""  # node id
    target: str = ""  # node id

    def to_dict(self) -> dict:
        return {"id": self.id, "source": self.source, "target": self.target}

    @staticmethod
    def from_dict(d: dict) -> "Edge":
        return Edge(
            id=str(d.get("id") or new_id()),
            source=str(d.get("source", "")),
            target=str(d.get("target", "")),
        )


@dataclass
class Workflow:
    id: str = field(default_factory=new_id)
    name: str = "New Workflow"
    description: str = ""
    nodes: list[DagNode] = field(default_factory=list)
    edges: list[Edge] = field(default_factory=list)
    # Parameters applied to EVERY DAG in this workflow (a per-DAG key of the
    # same name wins). Empty by default - only enforced when you fill it in.
    shared_params: dict[str, str] = field(default_factory=dict)
    created_at: str = field(default_factory=utc_now_iso)
    updated_at: str = field(default_factory=utc_now_iso)

    def node_by_id(self, node_id: str) -> DagNode | None:
        return next((n for n in self.nodes if n.id == node_id), None)

    def effective_params(self, node: DagNode) -> dict[str, str]:
        """Shared params merged with the node's own (node wins on conflict)."""
        return {**self.shared_params, **node.params}

    def conf_for(self, node: DagNode) -> str:
        """Compact `--conf` JSON for a node, including shared params."""
        return json.dumps(self.effective_params(node),
                          separators=(",", ":"), ensure_ascii=False)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "nodes": [n.to_dict() for n in self.nodes],
            "edges": [e.to_dict() for e in self.edges],
            "shared_params": dict(self.shared_params),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2, ensure_ascii=False)

    @staticmethod
    def from_dict(d: dict) -> "Workflow":
        wf = Workflow(
            id=str(d.get("id") or new_id()),
            name=str(d.get("name", "Imported Workflow")),
            description=str(d.get("description", "")),
            shared_params={str(k): str(v)
                           for k, v in dict(d.get("shared_params") or {}).items()},
            created_at=str(d.get("created_at") or utc_now_iso()),
            updated_at=str(d.get("updated_at") or utc_now_iso()),
        )
        wf.nodes = [DagNode.from_dict(n) for n in d.get("nodes", [])]
        node_ids = {n.id for n in wf.nodes}
        # Drop edges referencing missing nodes (defensive on import).
        wf.edges = [
            e
            for e in (Edge.from_dict(x) for x in d.get("edges", []))
            if e.source in node_ids and e.target in node_ids
        ]
        return wf

    @staticmethod
    def from_json(text: str) -> "Workflow":
        return Workflow.from_dict(json.loads(text))
