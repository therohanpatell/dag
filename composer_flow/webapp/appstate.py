"""Application state for the web app - no Streamlit, no GUI toolkit.

Owns the singleton Database, repositories, gcloud client factory, the active
environment target, and the single running execution (this is a local,
single-user app, so one workflow runs at a time). A background drainer thread
copies engine events into `run_state` so the browser can poll for live status.
"""
from __future__ import annotations

import ctypes
import sys
import threading

from composer_flow.config import ENVIRONMENT_PROFILES
from composer_flow.persistence.db import Database
from composer_flow.persistence.repositories import (
    ExecutionRepository,
    SettingsRepository,
    WorkflowRepository,
)
from composer_flow.services import events as ev
from composer_flow.services.engine import EngineConfig, WorkflowEngine
from composer_flow.services.gcloud import ComposerTarget, GcloudClient
from composer_flow.utils.logger import get_logger

log = get_logger("webapp")

_db: Database | None = None
_lock = threading.Lock()

# live run state (thread-safe via _run_lock)
_run_lock = threading.Lock()
_current_engine: WorkflowEngine | None = None
_run_state: dict = {
    "running": False,
    "execution_id": "",
    "statuses": {},        # node_id -> status
    "log": [],             # list of {level, message}
    "progress": {"done": 0, "total": 0},
    "eta": "",
    "final_status": "",
    "error": "",
}


def db() -> Database:
    global _db
    with _lock:
        if _db is None:
            _db = Database()
            _db.initialize()
    return _db


def workflows() -> WorkflowRepository:
    return WorkflowRepository(db())


def executions() -> ExecutionRepository:
    return ExecutionRepository(db())


def settings() -> SettingsRepository:
    return SettingsRepository(db())


def build_gcloud() -> GcloudClient:
    s = settings()
    return GcloudClient(
        retry_count=s.get_int("cli_retry_count", 0),
        retry_backoff_seconds=s.get_int("cli_retry_backoff_seconds", 1),
    )


def active_profile() -> str:
    name = settings().get("active_profile")
    return name if name in ENVIRONMENT_PROFILES else ENVIRONMENT_PROFILES[0]


def target() -> ComposerTarget:
    s = settings()
    name = active_profile()
    env = s.get(f"profile_{name}_environment") or s.get("composer_environment")
    loc = s.get(f"profile_{name}_location") or s.get("composer_location")
    proj = s.get(f"profile_{name}_project") or s.get("gcp_project")
    return ComposerTarget(environment=env, location=loc, project=proj)


# --- keep the machine awake during a run --------------------------------- #
# Windows: SetThreadExecutionState. The flags persist for the CALLING THREAD
# until reset or the thread exits, so this MUST be called from the long-lived
# drain thread that lives for the whole run.
_ES_CONTINUOUS = 0x80000000
_ES_SYSTEM_REQUIRED = 0x00000001
_ES_DISPLAY_REQUIRED = 0x00000002


def set_keep_awake(on: bool) -> None:
    if not sys.platform.startswith("win"):
        return
    try:
        flags = _ES_CONTINUOUS
        if on:
            flags |= _ES_SYSTEM_REQUIRED | _ES_DISPLAY_REQUIRED
        ctypes.windll.kernel32.SetThreadExecutionState(flags)
        log.info("Keep-awake %s", "ON" if on else "OFF")
    except Exception as exc:
        log.warning("Could not set keep-awake: %s", exc)


# --- execution control --------------------------------------------------- #

def run_state_snapshot() -> dict:
    with _run_lock:
        return {
            "running": _run_state["running"],
            "execution_id": _run_state["execution_id"],
            "statuses": dict(_run_state["statuses"]),
            "log": list(_run_state["log"]),
            "progress": dict(_run_state["progress"]),
            "eta": _run_state["eta"],
            "final_status": _run_state["final_status"],
            "error": _run_state["error"],
        }


def is_running() -> bool:
    with _run_lock:
        return _run_state["running"]


def start_run(workflow, resume_execution_id=None, completed=None) -> str:
    global _current_engine
    s = settings()
    config = EngineConfig(
        target=target(),
        poll_interval=s.get_int("poll_interval_seconds", 5),
        trigger_timeout=s.get_int("trigger_timeout_seconds", 30),
        poll_timeout=s.get_int("poll_timeout_seconds", 30),
        max_parallel=s.get_int("max_parallel_dags", 1),
    )
    engine = WorkflowEngine(
        workflow=workflow, gcloud=build_gcloud(), exec_repo=executions(),
        config=config, resume_execution_id=resume_execution_id,
        completed_node_ids=completed,
    )
    with _run_lock:
        _current_engine = engine
        _run_state.update({
            "running": True, "execution_id": "", "statuses": {}, "log": [],
            "progress": {"done": 0, "total": len(workflow.nodes)},
            "eta": "", "final_status": "", "error": "",
        })
    engine.start()
    threading.Thread(target=_drain_loop, args=(engine,),
                     name="run-drainer", daemon=True).start()
    return engine.execution_id


def cancel_run() -> None:
    with _run_lock:
        engine = _current_engine
    if engine is not None and engine.is_running():
        engine.cancel()


def _drain_loop(engine: WorkflowEngine) -> None:
    import queue as _queue

    awake = settings().get("keep_awake_during_run") != "0"
    if awake:
        set_keep_awake(True)   # stop Windows sleeping mid-run
    try:
        while True:
            try:
                event = engine.events.get(timeout=0.5)
            except _queue.Empty:
                if not engine.is_running() and engine.events.empty():
                    break
                continue
            with _run_lock:
                if event.type == ev.NODE_STATUS:
                    _run_state["statuses"][event.node_id] = event.status
                elif event.type == ev.LOG:
                    _run_state["log"].append({"level": event.level, "message": event.message})
                elif event.type == ev.PROGRESS:
                    _run_state["progress"] = {"done": event.done, "total": event.total}
                elif event.type == ev.ETA:
                    _run_state["eta"] = event.text
                elif event.type == ev.FINISHED:
                    _run_state["final_status"] = event.status
                    _run_state["error"] = event.error
                if not _run_state["execution_id"] and engine.execution_id:
                    _run_state["execution_id"] = engine.execution_id
    finally:
        with _run_lock:
            _run_state["running"] = False
            if not _run_state["execution_id"] and engine.execution_id:
                _run_state["execution_id"] = engine.execution_id
        if awake:
            set_keep_awake(False)
    log.info("Run drainer finished for execution %s", engine.execution_id)
