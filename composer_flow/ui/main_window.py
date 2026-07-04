"""Main window — the application controller.

Wires: workflow list <-> graph editor <-> properties panel <-> execution
engine <-> console/timeline. Owns startup checks (gcloud auth, interrupted
execution resume), workflow CRUD, versioning, import/export, history and
theme switching.
"""
from __future__ import annotations

import json

from PySide6.QtCore import QPointF, Qt, QThread, Signal
from PySide6.QtGui import QAction, QKeySequence
from PySide6.QtWidgets import (
    QComboBox,
    QDockWidget,
    QFileDialog,
    QInputDialog,
    QLabel,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSizePolicy,
    QTabWidget,
    QToolBar,
    QWidget,
)

from composer_flow.config import ENVIRONMENT_PROFILES
from composer_flow.core import graph as g
from composer_flow.models.execution import NodeStatus, WorkflowStatus
from composer_flow.models.workflow import Workflow
from composer_flow.persistence.db import Database
from composer_flow.persistence.repositories import (
    ExecutionRepository,
    SettingsRepository,
    WorkflowRepository,
)
from composer_flow.services.engine import EngineConfig, WorkflowEngine
from composer_flow.services.gcloud import (
    AuthStatus,
    ComposerTarget,
    GcloudClient,
    GcloudNotFoundError,
)
from composer_flow.ui.dialogs import (
    AuthDialog,
    ConfirmRunDialog,
    HistoryDialog,
    ResumeDialog,
    SettingsDialog,
    VersionsDialog,
)
from composer_flow.ui.graph_editor import GraphEditor
from composer_flow.ui.panels import (
    ConsolePanel,
    PropertiesPanel,
    StepRibbon,
    TimelinePanel,
    WorkflowListPanel,
)
from composer_flow.ui.theme import qss
from composer_flow.utils.logger import get_logger

log = get_logger("ui")


class _StartupAuthThread(QThread):
    result = Signal(object)

    def __init__(self, gcloud: GcloudClient, parent=None) -> None:
        super().__init__(parent)
        self._gcloud = gcloud

    def run(self) -> None:
        try:
            self.result.emit(self._gcloud.check_auth())
        except GcloudNotFoundError as exc:
            self.result.emit(AuthStatus(False, error=str(exc)))


class MainWindow(QMainWindow):
    def __init__(self, db: Database) -> None:
        super().__init__()
        self.db = db
        self.workflows = WorkflowRepository(db)
        self.executions = ExecutionRepository(db)
        self.settings = SettingsRepository(db)

        self.current_workflow: Workflow | None = None
        self.engine: WorkflowEngine | None = None
        self._dirty = False
        self._auth_thread: _StartupAuthThread | None = None
        self.auth_status = AuthStatus(False)

        self.setWindowTitle("ComposerFlow — Cloud Composer DAG Orchestrator")
        self.resize(1480, 900)

        self._build_ui()
        self.setStyleSheet(qss())
        self._reload_workflow_list()

    # -- construction ----------------------------------------------------

    def _build_gcloud(self) -> GcloudClient:
        return GcloudClient(
            retry_count=self.settings.get_int("cli_retry_count", 0),
            retry_backoff_seconds=self.settings.get_int("cli_retry_backoff_seconds", 1),
        )

    def _active_profile(self) -> str:
        name = self.settings.get("active_profile")
        return name if name in ENVIRONMENT_PROFILES else ENVIRONMENT_PROFILES[0]

    def _target(self) -> ComposerTarget:
        name = self._active_profile()
        env = self.settings.get(f"profile_{name}_environment")
        loc = self.settings.get(f"profile_{name}_location")
        proj = self.settings.get(f"profile_{name}_project")
        if not (env and loc and proj):
            # fallback for databases created before profiles existed
            env = env or self.settings.get("composer_environment")
            loc = loc or self.settings.get("composer_location")
            proj = proj or self.settings.get("gcp_project")
        return ComposerTarget(environment=env, location=loc, project=proj)

    def _build_ui(self) -> None:
        self.editor = GraphEditor(self)
        self.setCentralWidget(self.editor)
        self.editor.node_selected.connect(self._on_node_selected)
        self.editor.graph_changed.connect(self._on_graph_changed)
        self.editor.request_add_node.connect(self._add_node_at)

        # left: workflow list
        self.workflow_panel = WorkflowListPanel(self)
        self.workflow_panel.workflow_activated.connect(self._open_workflow)
        self.workflow_panel.new_requested.connect(self._new_workflow)
        self.workflow_panel.delete_requested.connect(self._delete_workflow)
        left = QDockWidget("Workflows", self)
        left.setWidget(self.workflow_panel)
        left.setFeatures(QDockWidget.DockWidgetMovable)
        self.addDockWidget(Qt.LeftDockWidgetArea, left)

        # right: properties
        self.properties = PropertiesPanel(self)
        self.properties.node_edited.connect(self._on_node_edited)
        right = QDockWidget("DAG properties", self)
        right.setWidget(self.properties)
        right.setFeatures(QDockWidget.DockWidgetMovable)
        self.addDockWidget(Qt.RightDockWidgetArea, right)

        # bottom: console + timeline
        self.console = ConsolePanel(self)
        self.timeline = TimelinePanel(self)
        tabs = QTabWidget()
        tabs.addTab(self.console, "Execution console")
        tabs.addTab(self.timeline, "Timeline")
        bottom = QDockWidget("Execution", self)
        bottom.setWidget(tabs)
        bottom.setFeatures(QDockWidget.DockWidgetMovable)
        self.addDockWidget(Qt.BottomDockWidgetArea, bottom)

        # toolbar — plain-text labeled buttons, grouped by task
        toolbar = QToolBar("Main", self)
        toolbar.setMovable(False)
        toolbar.setToolButtonStyle(Qt.ToolButtonTextOnly)
        self.addToolBar(toolbar)

        def action(text: str, slot, tooltip: str = "", shortcut: str = "") -> QAction:
            act = QAction(text, self)
            if shortcut:
                act.setShortcut(QKeySequence(shortcut))
            act.setToolTip(tooltip + (f"   ({shortcut})" if shortcut else ""))
            act.triggered.connect(slot)
            toolbar.addAction(act)
            return act

        action("New Workflow", self._new_workflow,
               "Create a new, empty workflow", "Ctrl+N")
        self.save_action = action("Save", self._save_workflow,
                                  "Save the current workflow", "Ctrl+S")
        toolbar.addSeparator()
        action("Add DAG Node", lambda: self._add_node_at(None),
               "Add a DAG to the canvas — the view centers on it")
        action("Auto-arrange", self.editor.auto_layout,
               "Neatly arrange the graph by execution order")
        action("Validate", self._validate_clicked,
               "Check for cycles, missing DAG IDs and disconnected nodes")
        toolbar.addSeparator()
        self.run_action = action("Run Workflow", self._run_workflow,
                                 "Trigger the DAGs in Cloud Composer", "F5")
        self.cancel_action = action("Stop", self._cancel_run,
                                    "Stop the running workflow (pending DAGs are cancelled)")
        self.cancel_action.setEnabled(False)
        toolbar.addSeparator()
        action("History", self._show_history,
               "Past executions — drill into logs, rerun failed DAGs")
        action("Versions", self._show_versions,
               "Restore an earlier saved version of this workflow")
        action("Import", self._import_workflow, "Import a workflow from a JSON file")
        action("Export", self._export_workflow, "Export this workflow to a JSON file")
        toolbar.addSeparator()
        action("Settings", self._show_settings,
               "Environment profiles, polling and execution options")

        # environment profile selector, pushed to the far right
        spacer = QWidget()
        spacer.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        toolbar.addWidget(spacer)
        toolbar.addWidget(QLabel("Environment:  "))
        self.env_combo = QComboBox()
        self.env_combo.addItems(list(ENVIRONMENT_PROFILES))
        self.env_combo.setToolTip(
            "Which Composer environment to run against — values per profile "
            "are set once in Settings"
        )
        self.env_combo.setCurrentText(self._active_profile())
        self.env_combo.currentTextChanged.connect(self._on_profile_changed)
        toolbar.addWidget(self.env_combo)

        # numbered step ribbon under the toolbar
        self.addToolBarBreak()
        ribbon_bar = QToolBar("Steps", self)
        ribbon_bar.setMovable(False)
        self.ribbon = StepRibbon(self)
        ribbon_bar.addWidget(self.ribbon)
        self.addToolBar(ribbon_bar)

        # status bar
        self.progress = QProgressBar()
        self.progress.setFixedWidth(220)
        self.progress.setVisible(False)
        self.eta_label = QLabel("")
        self.auth_label = QLabel("gcloud: checking…")
        self.auth_button = QPushButton("Sign in / Switch account")
        self.auth_button.setToolTip(
            "Open the Google sign-in page in your browser (gcloud auth login)"
        )
        self.auth_button.clicked.connect(self._show_auth_dialog)
        self.target_label = QLabel("")
        self.statusBar().addWidget(self.auth_label)
        self.statusBar().addWidget(self.auth_button)
        self.statusBar().addWidget(QLabel("  |  "))
        self.statusBar().addWidget(self.target_label)
        self.statusBar().addPermanentWidget(self.eta_label)
        self.statusBar().addPermanentWidget(self.progress)
        self._refresh_target_label()

    # -- startup checks -------------------------------------------------------

    def run_startup_checks(self) -> None:
        """Called once after show(): async gcloud auth check, then resume."""
        try:
            gcloud = self._build_gcloud()
            gcloud.gcloud_path()  # raises if gcloud missing
        except GcloudNotFoundError as exc:
            self.auth_label.setText("gcloud: NOT FOUND")
            QMessageBox.critical(self, "gcloud not found", str(exc))
            self._check_interrupted_executions()
            return

        self._auth_thread = _StartupAuthThread(gcloud, self)
        self._auth_thread.result.connect(self._on_auth_checked)
        self._auth_thread.start()

    def _on_auth_checked(self, status: AuthStatus) -> None:
        self.auth_status = status
        if status.authenticated:
            self.auth_label.setText(f"gcloud: {status.account}")
            self.console.append("success", f"Authenticated with gcloud as {status.account}.")
        else:
            self.auth_label.setText("gcloud: not authenticated")
            self.console.append("warning", f"gcloud auth check failed: {status.error}")
            dialog = AuthDialog(self._build_gcloud(), status, self)
            if dialog.exec() and dialog.status.authenticated:
                self.auth_status = dialog.status
                self.auth_label.setText(f"gcloud: {dialog.status.account}")
                self.console.append("success", f"Authenticated as {dialog.status.account}.")
        self._check_interrupted_executions()

    def _show_auth_dialog(self) -> None:
        """Sign in or switch Google account at any time (status-bar button)."""
        dialog = AuthDialog(self._build_gcloud(), self.auth_status, self)
        dialog.exec()
        if dialog.status.authenticated:
            self.auth_status = dialog.status
            self.auth_label.setText(f"gcloud: {dialog.status.account}")
            self.console.append("success", f"Authenticated as {dialog.status.account}.")

    def _check_interrupted_executions(self) -> None:
        for ex in self.executions.find_interrupted():
            nodes = self.executions.get_node_executions(ex["id"])
            done = [n for n in nodes if n.status == NodeStatus.SUCCESS.value]
            dialog = ResumeDialog(ex["workflow_name"], len(done), len(nodes), self)
            dialog.exec()
            if dialog.choice == ResumeDialog.RESUME:
                workflow = Workflow.from_json(ex["snapshot_json"])
                self._load_into_editor(workflow, dirty=False)
                self._start_engine(
                    workflow,
                    resume_execution_id=ex["id"],
                    completed={n.node_id for n in done},
                )
                return  # handle one at a time; others stay for next launch
            elif dialog.choice == ResumeDialog.RESTART:
                self.executions.mark_interrupted_as_cancelled(ex["id"])
                workflow = Workflow.from_json(ex["snapshot_json"])
                self._load_into_editor(workflow, dirty=False)
                self._run_workflow()
                return
            else:
                self.executions.mark_interrupted_as_cancelled(ex["id"])

    # -- workflow CRUD ---------------------------------------------------

    def _reload_workflow_list(self) -> None:
        selected = self.current_workflow.id if self.current_workflow else ""
        self.workflow_panel.set_workflows(self.workflows.list_summaries(), selected)

    def _new_workflow(self) -> None:
        if not self._confirm_discard_changes():
            return
        name, ok = QInputDialog.getText(self, "New workflow", "Workflow name:")
        if not ok or not name.strip():
            return
        workflow = Workflow(name=name.strip())
        self.workflows.save(workflow)
        self._load_into_editor(workflow, dirty=False)
        self._reload_workflow_list()
        self.console.append("info", f"Created workflow '{workflow.name}'.")

    def _open_workflow(self, workflow_id: str) -> None:
        if self.current_workflow and self.current_workflow.id == workflow_id:
            return
        if not self._confirm_discard_changes():
            self._reload_workflow_list()
            return
        workflow = self.workflows.get(workflow_id)
        if workflow:
            self._load_into_editor(workflow, dirty=False)

    def _delete_workflow(self, workflow_id: str) -> None:
        summary = next((s for s in self.workflows.list_summaries() if s["id"] == workflow_id), None)
        if summary is None:
            return
        answer = QMessageBox.question(
            self, "Delete workflow",
            f"Delete workflow '{summary['name']}' and its saved versions?\n"
            "Execution history is kept.",
        )
        if answer != QMessageBox.Yes:
            return
        self.workflows.delete(workflow_id)
        if self.current_workflow and self.current_workflow.id == workflow_id:
            self.current_workflow = None
            self.editor.load_workflow(Workflow(name="(no workflow)"), is_open=False)
            self.properties.show_node(None)
            self._set_dirty(False)
            self.ribbon.set_step(1)
        self._reload_workflow_list()

    def _save_workflow(self) -> None:
        if self.current_workflow is None:
            return
        issues = g.validate(self.current_workflow)
        errors = [i for i in issues if i.is_error]
        # Saving is allowed with warnings/errors (work in progress) — running is not.
        self.workflows.save(self.current_workflow)
        self._set_dirty(False)
        self._reload_workflow_list()
        note = f" ({len(errors)} validation error(s) outstanding)" if errors else ""
        self.console.append("success", f"Workflow '{self.current_workflow.name}' saved{note}.")

    def _load_into_editor(self, workflow: Workflow, dirty: bool) -> None:
        self.current_workflow = workflow
        self.editor.load_workflow(workflow, is_open=True)
        self.timeline.load_workflow(workflow)
        self.properties.show_node(None)
        self._set_dirty(dirty)
        self._reload_workflow_list()
        self.ribbon.set_step(1)

    def _confirm_discard_changes(self) -> bool:
        if not self._dirty or self.current_workflow is None:
            return True
        answer = QMessageBox.question(
            self, "Unsaved changes",
            f"'{self.current_workflow.name}' has unsaved changes. Save them?",
            QMessageBox.Save | QMessageBox.Discard | QMessageBox.Cancel,
        )
        if answer == QMessageBox.Save:
            self._save_workflow()
            return True
        return answer == QMessageBox.Discard

    def _set_dirty(self, dirty: bool) -> None:
        self._dirty = dirty
        name = self.current_workflow.name if self.current_workflow else "no workflow"
        star = " *" if dirty else ""
        self.setWindowTitle(f"ComposerFlow — {name}{star}")

    # -- editor callbacks --------------------------------------------------

    def _add_node_at(self, pos: QPointF | None) -> None:
        if self.current_workflow is None:
            QMessageBox.information(self, "No workflow", "Create or open a workflow first.")
            return
        if self.engine and self.engine.is_running():
            return
        self.editor.add_node(pos)
        self.timeline.load_workflow(self.current_workflow)
        self.statusBar().showMessage(
            "DAG node added — set its DAG ID in the panel on the right.", 8000
        )

    def _on_node_selected(self, node_id: str) -> None:
        node = self.current_workflow.node_by_id(node_id) if self.current_workflow else None
        self.properties.show_node(node if node_id else None)
        if node_id:
            self.ribbon.set_step(2)

    def _on_node_edited(self, node_id: str) -> None:
        self.editor.refresh_node(node_id)
        self._set_dirty(True)
        if self.current_workflow:
            self.timeline.load_workflow(self.current_workflow)

    def _on_graph_changed(self) -> None:
        self._set_dirty(True)

    # -- validation / run -----------------------------------------------------

    def _validate_clicked(self) -> None:
        if self.current_workflow is None:
            return
        issues = g.validate(self.current_workflow)
        if not issues:
            QMessageBox.information(self, "Validation", "✅ Workflow is valid.")
            self.console.append("success", "Validation passed.")
            self.ribbon.set_step(3)
            return
        text = "\n".join(f"[{i.level.upper()}] {i.message}" for i in issues)
        self.console.append("warning", f"Validation issues:\n{text}")
        if any(i.is_error for i in issues):
            QMessageBox.warning(self, "Validation errors", text)
        else:
            QMessageBox.information(self, "Validation warnings", text)

    def _preflight(self) -> bool:
        if self.current_workflow is None:
            QMessageBox.information(self, "No workflow", "Create or open a workflow first.")
            return False
        if self.engine and self.engine.is_running():
            QMessageBox.information(self, "Busy", "An execution is already running.")
            return False
        target = self._target()
        if not target.is_complete():
            QMessageBox.warning(
                self, "Missing settings",
                f"The '{self._active_profile()}' profile has no Composer "
                "environment / location / project yet.\nFill them in Settings "
                "(one time), then just pick the environment from the dropdown.",
            )
            self._show_settings()
            return False
        issues = [i for i in g.validate(self.current_workflow) if i.is_error]
        if issues:
            QMessageBox.warning(
                self, "Validation failed",
                "\n".join(i.message for i in issues),
            )
            return False
        if not self.auth_status.authenticated:
            answer = QMessageBox.question(
                self, "Not authenticated",
                "gcloud does not appear to be authenticated. Run anyway?",
            )
            if answer != QMessageBox.Yes:
                return False
        return True

    def _run_workflow(self) -> None:
        if not self._preflight():
            return
        workflow = self.current_workflow
        if self._dirty:
            self._save_workflow()

        if self.settings.get_bool("confirm_before_run"):
            target = self._target()
            levels = g.topological_levels(workflow)
            plan = []
            for i, level in enumerate(levels, 1):
                names = ", ".join(workflow.node_by_id(n).display_name() for n in level)
                suffix = "  (parallel)" if len(level) > 1 else ""
                plan.append(f"Wave {i}: {names}{suffix}")
            dialog = ConfirmRunDialog(
                workflow.name, plan,
                f"{target.project} / {target.location} / {target.environment}", self,
            )
            if not dialog.exec():
                return
        self._start_engine(workflow)

    def _start_engine(
        self,
        workflow: Workflow,
        resume_execution_id: str | None = None,
        completed: set[str] | None = None,
    ) -> None:
        config = EngineConfig(
            target=self._target(),
            poll_interval=self.settings.get_int("poll_interval_seconds", 5),
            trigger_timeout=self.settings.get_int("trigger_timeout_seconds", 30),
            poll_timeout=self.settings.get_int("poll_timeout_seconds", 30),
            max_parallel=self.settings.get_int("max_parallel_dags", 1),
        )
        self.engine = WorkflowEngine(
            workflow=workflow,
            gcloud=self._build_gcloud(),
            exec_repo=self.executions,
            config=config,
            resume_execution_id=resume_execution_id,
            completed_node_ids=completed,
            parent=self,
        )
        self.engine.node_status_changed.connect(self._on_node_status)
        self.engine.log_message.connect(self.console.append)
        self.engine.progress_changed.connect(self._on_progress)
        self.engine.eta_changed.connect(self.eta_label.setText)
        self.engine.execution_finished.connect(self._on_execution_finished)

        self.editor.set_read_only(True)
        self.run_action.setEnabled(False)
        self.cancel_action.setEnabled(True)
        self.progress.setVisible(True)
        self.progress.setValue(0)
        self.editor.reset_statuses()
        self.timeline.load_workflow(workflow)
        self.ribbon.set_step(4)
        self.console.append("info", f"Starting workflow '{workflow.name}'…")
        self.engine.start()

    def _cancel_run(self) -> None:
        if self.engine and self.engine.is_running():
            self.engine.cancel()

    def _on_node_status(self, node_id: str, status: str) -> None:
        self.editor.set_node_status(node_id, status)
        if self.engine:
            for rec in self.executions.get_node_executions(self.engine.execution_id):
                if rec.node_id == node_id:
                    self.timeline.update_node(
                        node_id, status, rec.airflow_run_id,
                        rec.started_at, rec.finished_at, rec.duration_seconds,
                    )
                    break

    def _on_progress(self, done: int, total: int) -> None:
        self.progress.setMaximum(max(total, 1))
        self.progress.setValue(done)
        self.progress.setFormat(f"{done}/{total} DAGs")

    def _on_execution_finished(self, status: str, error: str) -> None:
        self.editor.set_read_only(False)
        self.run_action.setEnabled(True)
        self.cancel_action.setEnabled(False)
        self.eta_label.setText("")
        self.ribbon.set_step(1)
        if status == WorkflowStatus.SUCCESS.value:
            self.statusBar().showMessage("Workflow completed successfully ✔", 15000)
        elif status == WorkflowStatus.FAILED.value:
            self.statusBar().showMessage(f"Workflow FAILED — {error}", 30000)
        else:
            self.statusBar().showMessage("Workflow cancelled.", 15000)

    # -- history / rerun-failed --------------------------------------------

    def _show_history(self) -> None:
        dialog = HistoryDialog(self.executions, self)
        dialog.rerun_failed_requested.connect(self._rerun_failed)
        dialog.exec()

    def _rerun_failed(self, execution_id: str) -> None:
        """Re-run only the DAGs that did not succeed in a past execution."""
        ex = self.executions.get_execution(execution_id)
        if ex is None:
            return
        if self.engine and self.engine.is_running():
            QMessageBox.information(self, "Busy", "An execution is already running.")
            return
        workflow = Workflow.from_json(ex["snapshot_json"])
        done = {
            n.node_id
            for n in self.executions.get_node_executions(execution_id)
            if n.status == NodeStatus.SUCCESS.value
        }
        self._load_into_editor(workflow, dirty=False)
        self.console.append(
            "info",
            f"Re-running failed DAGs of '{workflow.name}' "
            f"({len(done)} previously successful DAG(s) will be skipped).",
        )
        self._start_engine(workflow, resume_execution_id=execution_id, completed=done)

    # -- versions / import / export --------------------------------------------

    def _show_versions(self) -> None:
        if self.current_workflow is None:
            return
        dialog = VersionsDialog(self.workflows, self.current_workflow.id, self)
        if dialog.exec() and dialog.restored_version_row_id is not None:
            restored = self.workflows.get_version(dialog.restored_version_row_id)
            if restored:
                restored.id = self.current_workflow.id  # restore in place
                self._load_into_editor(restored, dirty=True)
                self.console.append("info", "Version restored — press Save to keep it.")

    def _export_workflow(self) -> None:
        if self.current_workflow is None:
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "Export workflow", f"{self.current_workflow.name}.json", "JSON (*.json)"
        )
        if path:
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(self.current_workflow.to_json())
            self.console.append("success", f"Exported to {path}")

    def _import_workflow(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Import workflow", "", "JSON (*.json)")
        if not path:
            return
        try:
            with open(path, "r", encoding="utf-8") as fh:
                workflow = Workflow.from_dict(json.load(fh))
        except (OSError, ValueError, KeyError) as exc:
            QMessageBox.critical(self, "Import failed", f"Not a valid workflow file:\n{exc}")
            return
        # New identity so an import never overwrites an existing workflow.
        from composer_flow.models.workflow import new_id

        old_to_new = {n.id: new_id() for n in workflow.nodes}
        workflow.id = new_id()
        for node in workflow.nodes:
            node.id = old_to_new[node.id]
        for edge in workflow.edges:
            edge.id = new_id()
            edge.source = old_to_new.get(edge.source, edge.source)
            edge.target = old_to_new.get(edge.target, edge.target)
        workflow.name = f"{workflow.name} (imported)"
        self.workflows.save(workflow)
        self._load_into_editor(workflow, dirty=False)
        self.console.append("success", f"Imported workflow '{workflow.name}'.")

    # -- settings / theme --------------------------------------------------

    def _show_settings(self) -> None:
        dialog = SettingsDialog(self.settings, self)
        if dialog.exec():
            self._refresh_target_label()
            self.console.append("info", "Settings saved.")

    def _on_profile_changed(self, name: str) -> None:
        self.settings.set("active_profile", name)
        self._refresh_target_label()
        target = self._target()
        detail = (
            f"{target.project} / {target.location} / {target.environment}"
            if target.is_complete()
            else "values not set — fill them in Settings"
        )
        self.console.append(
            "warning" if name == "PRD" else "info",
            f"Environment switched to {name} ({detail}).",
        )

    def _refresh_target_label(self) -> None:
        target = self._target()
        self.target_label.setText(
            f"{self._active_profile()}: {target.project or '?'} / "
            f"{target.location or '?'} / {target.environment or '?'}"
        )

    # -- shutdown --------------------------------------------------------------

    def closeEvent(self, event) -> None:
        if self.engine and self.engine.is_running():
            answer = QMessageBox.question(
                self, "Execution in progress",
                "A workflow is executing. Close anyway?\n"
                "(State is saved — you can resume on next launch.)",
            )
            if answer != QMessageBox.Yes:
                event.ignore()
                return
            # Leave the execution marked 'running' so startup offers Resume.
        if not self._confirm_discard_changes():
            event.ignore()
            return
        event.accept()
