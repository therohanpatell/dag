"""Workflow execution engine.

Algorithm (event-driven Kahn scheduler):

  1. Snapshot the workflow into an execution record (resume-safe).
  2. All nodes start PENDING (or SUCCESS when resuming past successes).
  3. Loop:
       a. ready = PENDING nodes whose predecessors are all SUCCESS
          -> submit trigger tasks to a bounded ThreadPoolExecutor.
             Independent branches become ready together => parallel execution.
       b. QUEUED/RUNNING nodes are polled every poll_interval seconds
          (dags list-runs filtered by our generated run-id).
       c. On FAILED: mark every descendant SKIPPED, stop launching new nodes,
          finish once in-flight CLI calls drain. Fail-fast.
       d. On user cancel: PENDING -> CANCELLED, drain in-flight calls.
  4. Every state transition is persisted to SQLite BEFORE the UI is notified,
     so a crash at any point leaves a consistent, resumable record.

Threading model: the engine runs in one background thread; blocking gcloud
calls run in a worker pool sized by max_parallel_dags. UI communication is a
thread-safe event queue (see services/events.py) — the engine never touches
any GUI toolkit, so the same engine drives both the Streamlit and Qt front-ends.
"""
from __future__ import annotations

import queue
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass

from composer_flow.core import graph as g
from composer_flow.models.execution import (
    NodeExecution,
    NodeStatus,
    WorkflowExecution,
    WorkflowStatus,
)
from composer_flow.models.workflow import Workflow, utc_now_iso
from composer_flow.persistence.repositories import ExecutionRepository
from composer_flow.services.events import (
    ETA,
    FINISHED,
    LOG,
    NODE_STATUS,
    PROGRESS,
    EngineEvent,
)
from composer_flow.services.gcloud import (
    CommandResult,
    ComposerTarget,
    GcloudClient,
    generate_run_id,
)
from composer_flow.utils.logger import get_logger

log = get_logger("engine")


@dataclass
class EngineConfig:
    target: ComposerTarget
    poll_interval: int = 20
    trigger_timeout: int = 300
    poll_timeout: int = 180
    max_parallel: int = 4


class WorkflowEngine:
    """One instance per workflow run. Create -> start(); consume `events`.

    All state changes are published as EngineEvent objects on the thread-safe
    `events` queue. Front-ends drain it and never reach into the engine.
    """

    def __init__(
        self,
        workflow: Workflow,
        gcloud: GcloudClient,
        exec_repo: ExecutionRepository,
        config: EngineConfig,
        resume_execution_id: str | None = None,
        completed_node_ids: set[str] | None = None,
    ) -> None:
        self.events: "queue.Queue[EngineEvent]" = queue.Queue()
        self.workflow = workflow
        self.gcloud = gcloud
        self.exec_repo = exec_repo
        self.config = config
        self._resume_execution_id = resume_execution_id
        self._completed = set(completed_node_ids or ())
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self.execution_id: str = ""

        self._statuses: dict[str, NodeStatus] = {}
        self._records: dict[str, NodeExecution] = {}
        self._start_monotonic: dict[str, float] = {}
        self._last_poll: dict[str, float] = {}
        self._eta_estimates: dict[str, float | None] = {}

    # -- lifecycle -------------------------------------------------------

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="workflow-engine", daemon=True)
        self._thread.start()

    def cancel(self) -> None:
        self._stop.set()
        self._log("warning", "Cancellation requested — pending DAGs will not be triggered.")

    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    # -- helpers ---------------------------------------------------------

    def _emit(self, event: EngineEvent) -> None:
        self.events.put(event)

    def _log(self, level: str, message: str) -> None:
        getattr(log, level if level != "success" else "info")(message)
        self._emit(EngineEvent(LOG, level=level, message=message))

    def _set_status(self, node_id: str, status: NodeStatus) -> None:
        """Persist first, then notify the UI (crash-consistent ordering)."""
        with self._lock:
            self._statuses[node_id] = status
            rec = self._records[node_id]
            rec.status = status.value
            if status in (NodeStatus.TRIGGERING,) and not rec.started_at:
                rec.started_at = utc_now_iso()
                self._start_monotonic[node_id] = time.monotonic()
            if status.is_terminal:
                rec.finished_at = utc_now_iso()
                started = self._start_monotonic.get(node_id)
                if started is not None:
                    rec.duration_seconds = round(time.monotonic() - started, 1)
            self.exec_repo.update_node(rec)
        self._emit(EngineEvent(NODE_STATUS, node_id=node_id, status=status.value))
        self._emit_progress()

    def _emit_progress(self) -> None:
        total = len(self.workflow.nodes)
        done = sum(1 for s in self._statuses.values() if s.is_terminal)
        self._emit(EngineEvent(PROGRESS, done=done, total=total))
        self._emit_eta()

    def _emit_eta(self) -> None:
        """Estimate remaining time from historical average DAG durations."""
        remaining = 0.0
        unknown = 0
        for node in self.workflow.nodes:
            status = self._statuses.get(node.id, NodeStatus.PENDING)
            if status.is_terminal:
                continue
            avg = self._eta_estimates.get(node.id)
            if avg is None:
                unknown += 1
                continue
            if status.is_active:
                elapsed = time.monotonic() - self._start_monotonic.get(node.id, time.monotonic())
                remaining += max(avg - elapsed, self.config.poll_interval / 2)
            else:
                remaining += avg
        if remaining <= 0 and unknown == 0:
            self._emit(EngineEvent(ETA, text=""))
            return
        mins, secs = divmod(int(remaining), 60)
        text = f"~{mins}m {secs:02d}s remaining"
        if unknown:
            text += f" (+{unknown} DAG(s) with no history)"
        self._emit(EngineEvent(ETA, text=text))

    # -- main loop ---------------------------------------------------------

    def _run(self) -> None:
        try:
            self._run_inner()
        except Exception as exc:  # never let the engine thread die silently
            log.exception("Engine crashed")
            try:
                if self.execution_id:
                    self.exec_repo.finish_execution(
                        self.execution_id, WorkflowStatus.FAILED, f"Engine error: {exc}"
                    )
            finally:
                self._emit(EngineEvent(FINISHED, status=WorkflowStatus.FAILED.value,
                                       error=str(exc)))

    def _prepare_execution(self) -> None:
        wf = self.workflow
        for node in wf.nodes:
            self._eta_estimates[node.id] = self.exec_repo.average_dag_duration(node.dag_id)

        if self._resume_execution_id:
            self.execution_id = self._resume_execution_id
            existing = {n.node_id: n for n in self.exec_repo.get_node_executions(self.execution_id)}
            for node in wf.nodes:
                rec = existing.get(node.id) or NodeExecution(
                    execution_id=self.execution_id, node_id=node.id,
                    dag_id=node.dag_id, run_name=node.run_name,
                )
                if node.id in self._completed:
                    rec.status = NodeStatus.SUCCESS.value
                    self._statuses[node.id] = NodeStatus.SUCCESS
                else:
                    rec.status = NodeStatus.PENDING.value
                    rec.airflow_run_id = ""
                    rec.error = ""
                    rec.started_at = ""
                    rec.finished_at = ""
                    rec.duration_seconds = 0.0
                    self._statuses[node.id] = NodeStatus.PENDING
                self._records[node.id] = rec
                self.exec_repo.update_node(rec)
            self._log("info", f"Resuming execution — {len(self._completed)} DAG(s) already succeeded.")
        else:
            execution = WorkflowExecution(
                workflow_id=wf.id, workflow_name=wf.name,
                snapshot_json=wf.to_json(),
            )
            self.execution_id = execution.id
            records = []
            for node in wf.nodes:
                rec = NodeExecution(
                    execution_id=execution.id, node_id=node.id,
                    dag_id=node.dag_id, run_name=node.run_name,
                )
                records.append(rec)
                self._records[node.id] = rec
                self._statuses[node.id] = NodeStatus.PENDING
            self.exec_repo.create_execution(execution, records)

        for node_id, status in self._statuses.items():
            self._emit(EngineEvent(NODE_STATUS, node_id=node_id, status=status.value))
        self._emit_progress()

    def _run_inner(self) -> None:
        wf = self.workflow
        issues = [i for i in g.validate(wf) if i.is_error]
        if issues:
            msg = "; ".join(i.message for i in issues)
            self._log("error", f"Validation failed: {msg}")
            self._emit(EngineEvent(FINISHED, status=WorkflowStatus.FAILED.value, error=msg))
            return

        self._prepare_execution()
        levels = g.topological_levels(wf)
        self._log(
            "info",
            f"Execution plan: {len(levels)} wave(s) — "
            + " | ".join(
                ", ".join(wf.node_by_id(n).display_name() for n in lvl) for lvl in levels
            ),
        )

        failed_any = False
        pool = ThreadPoolExecutor(
            max_workers=self.config.max_parallel, thread_name_prefix="dag-worker"
        )
        trigger_futures: dict[str, Future] = {}
        poll_futures: dict[str, Future] = {}

        try:
            while True:
                if self._stop.is_set():
                    self._cancel_pending()
                    break

                if not failed_any:
                    for node_id in g.ready_nodes(wf, self._statuses):
                        if node_id in trigger_futures:
                            continue
                        self._set_status(node_id, NodeStatus.TRIGGERING)
                        trigger_futures[node_id] = pool.submit(self._trigger_node, node_id)

                for futures in (trigger_futures, poll_futures):
                    for node_id, fut in list(futures.items()):
                        if fut.done():
                            del futures[node_id]

                now = time.monotonic()
                for node_id, status in list(self._statuses.items()):
                    if status in (NodeStatus.QUEUED, NodeStatus.RUNNING):
                        if node_id in poll_futures:
                            continue
                        if now - self._last_poll.get(node_id, 0.0) >= self.config.poll_interval:
                            self._last_poll[node_id] = now
                            poll_futures[node_id] = pool.submit(self._poll_node, node_id)

                if any(s == NodeStatus.FAILED for s in self._statuses.values()) and not failed_any:
                    failed_any = True
                    self._skip_descendants_of_failures()

                statuses = list(self._statuses.values())
                in_flight = bool(trigger_futures) or bool(poll_futures)
                if all(s.is_terminal for s in statuses) and not in_flight:
                    break
                if (
                    failed_any
                    and not in_flight
                    and all(s.is_terminal or s == NodeStatus.PENDING for s in statuses)
                ):
                    # fail-fast: remaining PENDING nodes will never become ready
                    for node_id, s in self._statuses.items():
                        if s == NodeStatus.PENDING:
                            self._set_status(node_id, NodeStatus.SKIPPED)
                    break

                time.sleep(0.5)
        finally:
            pool.shutdown(wait=True, cancel_futures=True)

        self._finish(failed_any)

    def _finish(self, failed_any: bool) -> None:
        if self._stop.is_set():
            status, error = WorkflowStatus.CANCELLED, "Cancelled by user"
        elif failed_any or any(s == NodeStatus.FAILED for s in self._statuses.values()):
            failed = [
                self.workflow.node_by_id(nid).display_name()
                for nid, s in self._statuses.items()
                if s == NodeStatus.FAILED and self.workflow.node_by_id(nid)
            ]
            status, error = WorkflowStatus.FAILED, f"Failed DAG(s): {', '.join(failed)}"
        else:
            status, error = WorkflowStatus.SUCCESS, ""
        self.exec_repo.finish_execution(self.execution_id, status, error)
        level = {"success": "success", "failed": "error", "cancelled": "warning"}.get(
            status.value, "info"
        )
        self._log(level, f"Workflow finished: {status.value.upper()}"
                          + (f" — {error}" if error else ""))
        self._emit(EngineEvent(FINISHED, status=status.value, error=error))

    def _cancel_pending(self) -> None:
        for node_id, status in list(self._statuses.items()):
            if status == NodeStatus.PENDING:
                self._set_status(node_id, NodeStatus.CANCELLED)

    def _skip_descendants_of_failures(self) -> None:
        skip: set[str] = set()
        for node_id, status in self._statuses.items():
            if status == NodeStatus.FAILED:
                skip |= g.descendants(self.workflow, node_id)
        for node_id in skip:
            if self._statuses.get(node_id) == NodeStatus.PENDING:
                node = self.workflow.node_by_id(node_id)
                self._log("warning", f"Skipping '{node.display_name()}' (upstream failure).")
                self._set_status(node_id, NodeStatus.SKIPPED)

    # -- per-node work (runs in pool threads) ------------------------------

    def _trigger_node(self, node_id: str) -> None:
        node = self.workflow.node_by_id(node_id)
        rec = self._records[node_id]
        try:
            run_id = generate_run_id(node.run_name or node.dag_id)
            rec.airflow_run_id = run_id
            self._log("info", f"Triggering DAG '{node.dag_id}' (run-id {run_id})")
            result = self.gcloud.trigger_dag(
                self.config.target, node.dag_id, node.conf_json(), run_id,
                timeout=self.config.trigger_timeout,
            )
            with self._lock:
                rec.command = result.command_str
                rec.stdout = result.stdout[-20000:]
                rec.stderr = result.stderr[-20000:]
                rec.retry_count = result.attempts - 1

            if result.ok:
                self._set_status(node_id, NodeStatus.QUEUED)
                self._log("info", f"'{node.dag_id}' accepted by Airflow — monitoring run state.")
                return

            # A trigger timeout may still have started the run server-side —
            # reconcile by checking whether our run-id exists before failing.
            self._log(
                "warning",
                f"Trigger command for '{node.dag_id}' failed (rc={result.returncode}); "
                "checking whether the run was created anyway...",
            )
            state, _ = self.gcloud.get_run_state(
                self.config.target, node.dag_id, run_id, timeout=self.config.poll_timeout
            )
            if state is not None:
                self._set_status(node_id, NodeStatus.QUEUED)
                self._log("info", f"'{node.dag_id}' run exists despite CLI error — monitoring.")
                return

            with self._lock:
                rec.error = (result.stderr or result.stdout or "Trigger failed").strip()[-4000:]
            self._set_status(node_id, NodeStatus.FAILED)
            self._report_failure(node.dag_id, rec, result)
        except Exception as exc:
            log.exception("Trigger crashed for %s", node.dag_id)
            with self._lock:
                rec.error = f"Internal error: {exc}"
            self._set_status(node_id, NodeStatus.FAILED)
            self._log("error", f"'{node.dag_id}' trigger raised: {exc}")

    def _poll_node(self, node_id: str) -> None:
        node = self.workflow.node_by_id(node_id)
        rec = self._records[node_id]
        try:
            state, result = self.gcloud.get_run_state(
                self.config.target, node.dag_id, rec.airflow_run_id,
                timeout=self.config.poll_timeout,
            )
            if state is None:
                if not result.ok:
                    self._log(
                        "warning",
                        f"Status poll for '{node.dag_id}' failed (rc={result.returncode}); "
                        "will retry.",
                    )
                return  # not visible yet / transient — keep polling
            current = self._statuses.get(node_id)
            if state == current or current is None or current.is_terminal:
                return
            if state == NodeStatus.SUCCESS:
                self._set_status(node_id, NodeStatus.SUCCESS)
                self._log(
                    "success",
                    f"'{node.dag_id}' SUCCEEDED in {rec.duration_seconds or 0:.0f}s.",
                )
            elif state == NodeStatus.FAILED:
                with self._lock:
                    rec.error = f"Airflow reported state 'failed' for run {rec.airflow_run_id}"
                self._set_status(node_id, NodeStatus.FAILED)
                self._report_failure(node.dag_id, rec, result)
            else:
                self._set_status(node_id, state)
                self._log("info", f"'{node.dag_id}' is {state.value}.")
        except Exception as exc:
            log.exception("Poll crashed for %s", node.dag_id)
            self._log("warning", f"Status poll for '{node.dag_id}' raised: {exc}; will retry.")

    def _report_failure(self, dag_id: str, rec: NodeExecution, result: CommandResult) -> None:
        self._log("error", f"DAG '{dag_id}' FAILED after {rec.duration_seconds:.0f}s.")
        self._log("error", f"  Command : {rec.command or result.command_str}")
        if rec.error:
            self._log("error", f"  Error   : {rec.error}")
        if result.stdout.strip():
            self._log("error", f"  stdout  : {result.stdout.strip()[-1500:]}")
        if result.stderr.strip():
            self._log("error", f"  stderr  : {result.stderr.strip()[-1500:]}")
