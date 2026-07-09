"""Execution-state models: node/workflow statuses and execution records."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from composer_flow.models.workflow import new_id, utc_now_iso


class NodeStatus(str, Enum):
    PENDING = "pending"        # waiting for upstream dependencies
    TRIGGERING = "triggering"  # gcloud trigger command in flight
    QUEUED = "queued"          # Airflow reports queued (or trigger accepted)
    RUNNING = "running"        # Airflow reports running
    SUCCESS = "success"
    FAILED = "failed"
    SKIPPED = "skipped"        # upstream failed - never triggered
    CANCELLED = "cancelled"    # user cancelled before trigger

    @property
    def is_terminal(self) -> bool:
        return self in (
            NodeStatus.SUCCESS,
            NodeStatus.FAILED,
            NodeStatus.SKIPPED,
            NodeStatus.CANCELLED,
        )

    @property
    def is_active(self) -> bool:
        return self in (NodeStatus.TRIGGERING, NodeStatus.QUEUED, NodeStatus.RUNNING)


class WorkflowStatus(str, Enum):
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass
class NodeExecution:
    """Persistent record of one DAG node execution attempt."""

    id: str = field(default_factory=new_id)
    execution_id: str = ""
    node_id: str = ""
    dag_id: str = ""
    run_name: str = ""
    airflow_run_id: str = ""
    status: str = NodeStatus.PENDING.value
    command: str = ""
    stdout: str = ""
    stderr: str = ""
    error: str = ""
    retry_count: int = 0
    started_at: str = ""
    finished_at: str = ""
    duration_seconds: float = 0.0


@dataclass
class WorkflowExecution:
    """Persistent record of one workflow run (with graph snapshot for resume)."""

    id: str = field(default_factory=new_id)
    workflow_id: str = ""
    workflow_name: str = ""
    status: str = WorkflowStatus.RUNNING.value
    snapshot_json: str = ""  # full Workflow JSON at run time - resume-safe
    error: str = ""
    started_at: str = field(default_factory=utc_now_iso)
    finished_at: str = ""
