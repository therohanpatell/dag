"""Repository layer — the only place that speaks SQL.

WorkflowRepository  : workflows + nodes + edges + versions + import/export
ExecutionRepository : execution history, node executions, resume state, stats
SettingsRepository  : key/value app settings with defaults
"""
from __future__ import annotations

import json

from composer_flow.config import DEFAULT_SETTINGS
from composer_flow.models.execution import (
    NodeExecution,
    NodeStatus,
    WorkflowExecution,
    WorkflowStatus,
)
from composer_flow.models.workflow import DagNode, Edge, Workflow, utc_now_iso
from composer_flow.persistence.db import Database
from composer_flow.utils.logger import get_logger

log = get_logger("repo")


class WorkflowRepository:
    def __init__(self, db: Database) -> None:
        self._db = db

    def list_summaries(self) -> list[dict]:
        with self._db.connect() as conn:
            rows = conn.execute(
                """SELECT w.id, w.name, w.description, w.updated_at,
                          (SELECT COUNT(*) FROM nodes n WHERE n.workflow_id = w.id) AS node_count
                   FROM workflows w ORDER BY w.name COLLATE NOCASE"""
            ).fetchall()
        return [dict(r) for r in rows]

    def get(self, workflow_id: str) -> Workflow | None:
        with self._db.connect() as conn:
            row = conn.execute(
                "SELECT * FROM workflows WHERE id = ?", (workflow_id,)
            ).fetchone()
            if row is None:
                return None
            wf = Workflow(
                id=row["id"],
                name=row["name"],
                description=row["description"],
                created_at=row["created_at"],
                updated_at=row["updated_at"],
            )
            wf.nodes = [
                DagNode(
                    id=n["id"],
                    dag_id=n["dag_id"],
                    run_name=n["run_name"],
                    params=json.loads(n["params_json"] or "{}"),
                    x=n["pos_x"],
                    y=n["pos_y"],
                )
                for n in conn.execute(
                    "SELECT * FROM nodes WHERE workflow_id = ?", (workflow_id,)
                ).fetchall()
            ]
            wf.edges = [
                Edge(id=e["id"], source=e["source_node_id"], target=e["target_node_id"])
                for e in conn.execute(
                    "SELECT * FROM edges WHERE workflow_id = ?", (workflow_id,)
                ).fetchall()
            ]
        return wf

    def save(self, wf: Workflow, snapshot_version: bool = True) -> None:
        """Upsert the workflow atomically and record a version snapshot."""
        wf.updated_at = utc_now_iso()
        with self._db.connect() as conn:
            conn.execute(
                """INSERT INTO workflows (id, name, description, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(id) DO UPDATE SET
                       name = excluded.name,
                       description = excluded.description,
                       updated_at = excluded.updated_at""",
                (wf.id, wf.name, wf.description, wf.created_at, wf.updated_at),
            )
            conn.execute("DELETE FROM nodes WHERE workflow_id = ?", (wf.id,))
            conn.execute("DELETE FROM edges WHERE workflow_id = ?", (wf.id,))
            conn.executemany(
                """INSERT INTO nodes (id, workflow_id, dag_id, run_name, params_json, pos_x, pos_y)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                [
                    (n.id, wf.id, n.dag_id, n.run_name, n.conf_json(), n.x, n.y)
                    for n in wf.nodes
                ],
            )
            conn.executemany(
                """INSERT INTO edges (id, workflow_id, source_node_id, target_node_id)
                   VALUES (?, ?, ?, ?)""",
                [(e.id, wf.id, e.source, e.target) for e in wf.edges],
            )
            if snapshot_version:
                last = conn.execute(
                    "SELECT COALESCE(MAX(version), 0) FROM workflow_versions WHERE workflow_id = ?",
                    (wf.id,),
                ).fetchone()[0]
                conn.execute(
                    """INSERT INTO workflow_versions (workflow_id, version, data_json, created_at)
                       VALUES (?, ?, ?, ?)""",
                    (wf.id, last + 1, wf.to_json(), utc_now_iso()),
                )
                # Keep the most recent 50 versions per workflow.
                conn.execute(
                    """DELETE FROM workflow_versions
                       WHERE workflow_id = ? AND version <= ?""",
                    (wf.id, last + 1 - 50),
                )
        log.info("Workflow saved: %s (%s nodes, %s edges)", wf.name, len(wf.nodes), len(wf.edges))

    def delete(self, workflow_id: str) -> None:
        with self._db.connect() as conn:
            conn.execute("DELETE FROM workflows WHERE id = ?", (workflow_id,))
        log.info("Workflow deleted: %s", workflow_id)

    def list_versions(self, workflow_id: str) -> list[dict]:
        with self._db.connect() as conn:
            rows = conn.execute(
                """SELECT id, version, created_at FROM workflow_versions
                   WHERE workflow_id = ? ORDER BY version DESC""",
                (workflow_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    def get_version(self, version_row_id: int) -> Workflow | None:
        with self._db.connect() as conn:
            row = conn.execute(
                "SELECT data_json FROM workflow_versions WHERE id = ?",
                (version_row_id,),
            ).fetchone()
        return Workflow.from_json(row["data_json"]) if row else None


class ExecutionRepository:
    def __init__(self, db: Database) -> None:
        self._db = db

    # -- workflow executions -------------------------------------------------

    def create_execution(self, ex: WorkflowExecution, nodes: list[NodeExecution]) -> None:
        with self._db.connect() as conn:
            conn.execute(
                """INSERT INTO executions
                   (id, workflow_id, workflow_name, status, snapshot_json, error,
                    started_at, finished_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (ex.id, ex.workflow_id, ex.workflow_name, ex.status, ex.snapshot_json,
                 ex.error, ex.started_at, ex.finished_at),
            )
            conn.executemany(
                """INSERT INTO node_executions
                   (id, execution_id, node_id, dag_id, run_name, airflow_run_id, status,
                    command, stdout, stderr, error, retry_count, started_at, finished_at,
                    duration_seconds)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                [
                    (n.id, ex.id, n.node_id, n.dag_id, n.run_name, n.airflow_run_id,
                     n.status, n.command, n.stdout, n.stderr, n.error, n.retry_count,
                     n.started_at, n.finished_at, n.duration_seconds)
                    for n in nodes
                ],
            )

    def finish_execution(self, execution_id: str, status: WorkflowStatus, error: str = "") -> None:
        with self._db.connect() as conn:
            conn.execute(
                "UPDATE executions SET status = ?, error = ?, finished_at = ? WHERE id = ?",
                (status.value, error, utc_now_iso(), execution_id),
            )

    def update_node(self, node_exec: NodeExecution) -> None:
        with self._db.connect() as conn:
            conn.execute(
                """UPDATE node_executions SET
                       airflow_run_id = ?, status = ?, command = ?, stdout = ?,
                       stderr = ?, error = ?, retry_count = ?, started_at = ?,
                       finished_at = ?, duration_seconds = ?
                   WHERE id = ?""",
                (node_exec.airflow_run_id, node_exec.status, node_exec.command,
                 node_exec.stdout, node_exec.stderr, node_exec.error,
                 node_exec.retry_count, node_exec.started_at, node_exec.finished_at,
                 node_exec.duration_seconds, node_exec.id),
            )

    # -- resume / history ----------------------------------------------------

    def find_interrupted(self) -> list[dict]:
        """Executions still marked 'running' — i.e. the app died mid-run."""
        with self._db.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM executions WHERE status = ? ORDER BY started_at DESC",
                (WorkflowStatus.RUNNING.value,),
            ).fetchall()
        return [dict(r) for r in rows]

    def mark_interrupted_as_cancelled(self, execution_id: str) -> None:
        with self._db.connect() as conn:
            conn.execute(
                "UPDATE executions SET status = ?, error = ?, finished_at = ? WHERE id = ?",
                (WorkflowStatus.CANCELLED.value, "Interrupted (application closed)",
                 utc_now_iso(), execution_id),
            )
            conn.execute(
                """UPDATE node_executions SET status = ?
                   WHERE execution_id = ? AND status IN (?, ?, ?, ?)""",
                (NodeStatus.CANCELLED.value, execution_id,
                 NodeStatus.PENDING.value, NodeStatus.TRIGGERING.value,
                 NodeStatus.QUEUED.value, NodeStatus.RUNNING.value),
            )

    def get_execution(self, execution_id: str) -> dict | None:
        with self._db.connect() as conn:
            row = conn.execute(
                "SELECT * FROM executions WHERE id = ?", (execution_id,)
            ).fetchone()
        return dict(row) if row else None

    def get_node_executions(self, execution_id: str) -> list[NodeExecution]:
        with self._db.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM node_executions WHERE execution_id = ?", (execution_id,)
            ).fetchall()
        return [NodeExecution(**{k: r[k] for k in r.keys()}) for r in rows]

    def list_history(self, search: str = "", status: str = "", limit: int = 200) -> list[dict]:
        query = "SELECT * FROM executions WHERE 1=1"
        args: list = []
        if search:
            query += " AND workflow_name LIKE ?"
            args.append(f"%{search}%")
        if status:
            query += " AND status = ?"
            args.append(status)
        query += " ORDER BY started_at DESC LIMIT ?"
        args.append(limit)
        with self._db.connect() as conn:
            rows = conn.execute(query, args).fetchall()
        return [dict(r) for r in rows]

    def average_dag_duration(self, dag_id: str) -> float | None:
        """Mean duration of past successful runs — used for ETA estimates."""
        with self._db.connect() as conn:
            row = conn.execute(
                """SELECT AVG(duration_seconds) FROM node_executions
                   WHERE dag_id = ? AND status = ? AND duration_seconds > 0""",
                (dag_id, NodeStatus.SUCCESS.value),
            ).fetchone()
        return float(row[0]) if row and row[0] else None


class SettingsRepository:
    def __init__(self, db: Database) -> None:
        self._db = db

    def get(self, key: str) -> str:
        with self._db.connect() as conn:
            row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        if row is not None:
            return row["value"]
        return DEFAULT_SETTINGS.get(key, "")

    def get_int(self, key: str, minimum: int = 1) -> int:
        try:
            return max(minimum, int(self.get(key)))
        except (TypeError, ValueError):
            return max(minimum, int(DEFAULT_SETTINGS.get(key, minimum)))

    def get_bool(self, key: str) -> bool:
        return self.get(key) in ("1", "true", "True", "yes")

    def set(self, key: str, value: str) -> None:
        with self._db.connect() as conn:
            conn.execute(
                """INSERT INTO settings (key, value) VALUES (?, ?)
                   ON CONFLICT(key) DO UPDATE SET value = excluded.value""",
                (key, str(value)),
            )

    def all(self) -> dict[str, str]:
        merged = dict(DEFAULT_SETTINGS)
        with self._db.connect() as conn:
            for row in conn.execute("SELECT key, value FROM settings").fetchall():
                merged[row["key"]] = row["value"]
        return merged
