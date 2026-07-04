"""SQLite database bootstrap.

Design decisions:
- Embedded SQLite (stdlib sqlite3) — no external database server. The file
  lives in %LOCALAPPDATA%/ComposerFlow/composerflow.db.
- WAL journal mode so the UI thread and the execution engine thread can
  read/write concurrently without "database is locked" errors.
- A new short-lived connection per operation (see Database.connect) — the
  simplest thread-safe pattern for a desktop app; connection setup cost is
  negligible at this scale.
- Schema versioning via PRAGMA user_version for forward migrations.
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path

from composer_flow.config import db_path
from composer_flow.utils.logger import get_logger

log = get_logger("db")

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS workflows (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS nodes (
    id          TEXT PRIMARY KEY,
    workflow_id TEXT NOT NULL REFERENCES workflows(id) ON DELETE CASCADE,
    dag_id      TEXT NOT NULL,
    run_name    TEXT NOT NULL DEFAULT '',
    params_json TEXT NOT NULL DEFAULT '{}',
    pos_x       REAL NOT NULL DEFAULT 0,
    pos_y       REAL NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS edges (
    id             TEXT PRIMARY KEY,
    workflow_id    TEXT NOT NULL REFERENCES workflows(id) ON DELETE CASCADE,
    source_node_id TEXT NOT NULL,
    target_node_id TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS workflow_versions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    workflow_id TEXT NOT NULL REFERENCES workflows(id) ON DELETE CASCADE,
    version     INTEGER NOT NULL,
    data_json   TEXT NOT NULL,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS executions (
    id            TEXT PRIMARY KEY,
    workflow_id   TEXT NOT NULL,
    workflow_name TEXT NOT NULL,
    status        TEXT NOT NULL,
    snapshot_json TEXT NOT NULL,
    error         TEXT NOT NULL DEFAULT '',
    started_at    TEXT NOT NULL,
    finished_at   TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS node_executions (
    id               TEXT PRIMARY KEY,
    execution_id     TEXT NOT NULL REFERENCES executions(id) ON DELETE CASCADE,
    node_id          TEXT NOT NULL,
    dag_id           TEXT NOT NULL,
    run_name         TEXT NOT NULL DEFAULT '',
    airflow_run_id   TEXT NOT NULL DEFAULT '',
    status           TEXT NOT NULL,
    command          TEXT NOT NULL DEFAULT '',
    stdout           TEXT NOT NULL DEFAULT '',
    stderr           TEXT NOT NULL DEFAULT '',
    error            TEXT NOT NULL DEFAULT '',
    retry_count      INTEGER NOT NULL DEFAULT 0,
    started_at       TEXT NOT NULL DEFAULT '',
    finished_at      TEXT NOT NULL DEFAULT '',
    duration_seconds REAL NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_nodes_workflow ON nodes(workflow_id);
CREATE INDEX IF NOT EXISTS idx_edges_workflow ON edges(workflow_id);
CREATE INDEX IF NOT EXISTS idx_versions_workflow ON workflow_versions(workflow_id, version);
CREATE INDEX IF NOT EXISTS idx_nodeexec_execution ON node_executions(execution_id);
CREATE INDEX IF NOT EXISTS idx_nodeexec_dag ON node_executions(dag_id, status);
CREATE INDEX IF NOT EXISTS idx_exec_status ON executions(status);
"""


class Database:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or db_path()

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as conn:
            conn.executescript(_SCHEMA)
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            if version < SCHEMA_VERSION:
                # Future migrations: if version == 1: ... etc.
                conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        log.info("Database ready at %s (schema v%s)", self.path, SCHEMA_VERSION)

    @contextmanager
    def connect(self):
        """Short-lived connection with sane pragmas; commits on success."""
        conn = sqlite3.connect(str(self.path), timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=30000")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
