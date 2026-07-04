"""Dialogs: settings, gcloud authentication, resume-interrupted, run
confirmation, execution history, and workflow versions."""
from __future__ import annotations

import json

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from composer_flow.config import ENVIRONMENT_PROFILES
from composer_flow.models.execution import NodeStatus
from composer_flow.persistence.repositories import (
    ExecutionRepository,
    SettingsRepository,
    WorkflowRepository,
)
from composer_flow.services.gcloud import AuthStatus, GcloudClient


class SettingsDialog(QDialog):
    def __init__(self, settings: SettingsRepository, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Settings")
        self.setMinimumWidth(640)
        self._settings = settings
        values = settings.all()

        # -- environment profiles (BLD/INT/PRE/PRD) -------------------------
        profiles_grid = QGridLayout()
        for col, header in enumerate(
            ["", "Composer environment", "Location", "GCP project"]
        ):
            profiles_grid.addWidget(QLabel(f"<b>{header}</b>"), 0, col)
        self._profile_edits: dict[str, tuple[QLineEdit, QLineEdit, QLineEdit]] = {}
        for row, name in enumerate(ENVIRONMENT_PROFILES, start=1):
            profiles_grid.addWidget(QLabel(f"<b>{name}</b>"), row, 0)
            env = QLineEdit(values.get(f"profile_{name}_environment", ""))
            loc = QLineEdit(values.get(f"profile_{name}_location", ""))
            loc.setPlaceholderText("e.g. europe-west1")
            proj = QLineEdit(values.get(f"profile_{name}_project", ""))
            profiles_grid.addWidget(env, row, 1)
            profiles_grid.addWidget(loc, row, 2)
            profiles_grid.addWidget(proj, row, 3)
            self._profile_edits[name] = (env, loc, proj)

        # migrate: pre-profile databases had one flat target — seed BLD row
        if not any(
            e.text() or l.text() or p.text()
            for e, l, p in self._profile_edits.values()
        ) and values.get("composer_environment"):
            env, loc, proj = self._profile_edits[ENVIRONMENT_PROFILES[0]]
            env.setText(values["composer_environment"])
            loc.setText(values["composer_location"])
            proj.setText(values["gcp_project"])

        form = QFormLayout()

        self.poll_interval = QSpinBox()
        self.poll_interval.setRange(5, 600)
        self.poll_interval.setValue(int(values["poll_interval_seconds"]))
        self.poll_interval.setSuffix(" s")
        form.addRow("Status poll interval", self.poll_interval)

        self.trigger_timeout = QSpinBox()
        self.trigger_timeout.setRange(30, 1800)
        self.trigger_timeout.setValue(int(values["trigger_timeout_seconds"]))
        self.trigger_timeout.setSuffix(" s")
        form.addRow("Trigger command timeout", self.trigger_timeout)

        self.max_parallel = QSpinBox()
        self.max_parallel.setRange(1, 16)
        self.max_parallel.setValue(int(values["max_parallel_dags"]))
        form.addRow("Max parallel DAGs", self.max_parallel)

        self.retry_count = QSpinBox()
        self.retry_count.setRange(0, 10)
        self.retry_count.setValue(int(values["cli_retry_count"]))
        form.addRow("CLI retries (transient errors)", self.retry_count)

        self.confirm_run = QCheckBox("Ask for confirmation before triggering a workflow")
        self.confirm_run.setChecked(values["confirm_before_run"] == "1")
        form.addRow("", self.confirm_run)

        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        title = QLabel(
            "<b>Environment profiles</b> — fill these in once; pick the active "
            "one from the toolbar dropdown."
        )
        title.setWordWrap(True)
        layout.addWidget(title)
        layout.addLayout(profiles_grid)
        layout.addSpacing(12)
        layout.addLayout(form)
        layout.addWidget(buttons)

    def _save(self) -> None:
        s = self._settings
        for name, (env, loc, proj) in self._profile_edits.items():
            s.set(f"profile_{name}_environment", env.text().strip())
            s.set(f"profile_{name}_location", loc.text().strip())
            s.set(f"profile_{name}_project", proj.text().strip())
        s.set("poll_interval_seconds", str(self.poll_interval.value()))
        s.set("trigger_timeout_seconds", str(self.trigger_timeout.value()))
        s.set("max_parallel_dags", str(self.max_parallel.value()))
        s.set("cli_retry_count", str(self.retry_count.value()))
        s.set("confirm_before_run", "1" if self.confirm_run.isChecked() else "0")
        self.accept()


class _AuthCheckThread(QThread):
    finished_with = Signal(object)  # AuthStatus

    def __init__(self, gcloud: GcloudClient, parent=None) -> None:
        super().__init__(parent)
        self._gcloud = gcloud

    def run(self) -> None:
        self.finished_with.emit(self._gcloud.check_auth())


class AuthDialog(QDialog):
    """Shown at startup when no active gcloud credential is found. Lets the
    user launch `gcloud auth login` and re-check without leaving the app."""

    def __init__(self, gcloud: GcloudClient, initial: AuthStatus, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Google Cloud Authentication")
        self.setMinimumWidth(460)
        self._gcloud = gcloud
        self.status = initial
        self._thread: _AuthCheckThread | None = None

        layout = QVBoxLayout(self)
        self.message = QLabel()
        self.message.setWordWrap(True)
        layout.addWidget(self.message)

        buttons = QHBoxLayout()
        self.login_btn = QPushButton("Run 'gcloud auth login'")
        self.login_btn.setObjectName("primary")
        self.login_btn.clicked.connect(self._launch_login)
        self.recheck_btn = QPushButton("Re-check")
        self.recheck_btn.clicked.connect(self._recheck)
        self.continue_btn = QPushButton("Continue anyway")
        self.continue_btn.clicked.connect(self.reject)
        buttons.addWidget(self.login_btn)
        buttons.addWidget(self.recheck_btn)
        buttons.addStretch(1)
        buttons.addWidget(self.continue_btn)
        layout.addLayout(buttons)
        self._render()

    def _render(self) -> None:
        if self.status.authenticated:
            self.message.setText(
                f"✅ Authenticated as <b>{self.status.account}</b>"
                + (f" (project <b>{self.status.project}</b>)" if self.status.project else "")
                + "<br><br>To use a different Google account, click "
                  "<b>Sign in with a different account</b> — your browser will open."
            )
            self.login_btn.setText("Sign in with a different account")
            self.continue_btn.setText("Continue")
        else:
            self.message.setText(
                "⚠️ No active Google Cloud credential was found.<br><br>"
                f"<i>{self.status.error}</i><br><br>"
                "Click the button below — your browser opens with the Google "
                "sign-in page. Complete it there, then press <b>Re-check</b>."
            )
            self.login_btn.setText("Sign in with Google (gcloud auth login)")

    def _launch_login(self) -> None:
        try:
            self._gcloud.launch_login()
            self.message.setText(
                "Your browser should open with the Google sign-in page "
                "(<b>gcloud auth login</b>). Complete it there, then press "
                "<b>Re-check</b>."
            )
        except Exception as exc:
            self.message.setText(f"❌ Could not launch gcloud: {exc}")

    def _recheck(self) -> None:
        self.recheck_btn.setEnabled(False)
        self.recheck_btn.setText("Checking…")
        self._thread = _AuthCheckThread(self._gcloud, self)
        self._thread.finished_with.connect(self._on_checked)
        self._thread.start()

    def _on_checked(self, status: AuthStatus) -> None:
        self.status = status
        self.recheck_btn.setEnabled(True)
        self.recheck_btn.setText("Re-check")
        self._render()
        if status.authenticated:
            self.accept()


class ResumeDialog(QDialog):
    """Offered on startup when a previous execution was interrupted."""

    RESUME, RESTART, DISCARD = 1, 2, 3

    def __init__(self, workflow_name: str, done: int, total: int, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Interrupted execution found")
        self.choice = self.DISCARD
        layout = QVBoxLayout(self)
        label = QLabel(
            f"Previous execution of <b>{workflow_name}</b> was interrupted.<br>"
            f"{done} of {total} DAG(s) had already succeeded.<br><br>"
            "Resume from the last successful DAG?"
        )
        label.setWordWrap(True)
        layout.addWidget(label)
        buttons = QHBoxLayout()
        resume_btn = QPushButton("Resume")
        resume_btn.setObjectName("primary")
        restart_btn = QPushButton("Restart from beginning")
        discard_btn = QPushButton("Discard")
        resume_btn.clicked.connect(lambda: self._pick(self.RESUME))
        restart_btn.clicked.connect(lambda: self._pick(self.RESTART))
        discard_btn.clicked.connect(lambda: self._pick(self.DISCARD))
        buttons.addWidget(resume_btn)
        buttons.addWidget(restart_btn)
        buttons.addWidget(discard_btn)
        layout.addLayout(buttons)

    def _pick(self, choice: int) -> None:
        self.choice = choice
        self.accept()


class ConfirmRunDialog(QDialog):
    """Summary + explicit confirmation before triggering production DAGs."""

    def __init__(self, workflow_name: str, plan_lines: list[str], target_desc: str,
                 parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Confirm execution")
        self.setMinimumWidth(520)
        layout = QVBoxLayout(self)
        header = QLabel(
            f"You are about to trigger workflow <b>{workflow_name}</b> against:<br>"
            f"<b>{target_desc}</b><br><br>Execution plan:"
        )
        header.setWordWrap(True)
        layout.addWidget(header)
        plan = QPlainTextEdit("\n".join(plan_lines))
        plan.setReadOnly(True)
        plan.setMaximumHeight(220)
        layout.addWidget(plan)
        buttons = QDialogButtonBox(QDialogButtonBox.Cancel)
        run_btn = QPushButton("Trigger workflow")
        run_btn.setObjectName("primary")
        buttons.addButton(run_btn, QDialogButtonBox.AcceptRole)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)


class HistoryDialog(QDialog):
    """Execution history dashboard with search/status filters and drill-down
    into per-node commands, stdout/stderr and errors. Also exposes
    'Rerun failed only' for a selected failed execution."""

    rerun_failed_requested = Signal(str)  # execution id

    def __init__(self, exec_repo: ExecutionRepository, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Execution history")
        self.resize(1000, 640)
        self._repo = exec_repo

        layout = QVBoxLayout(self)
        filters = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText("Filter by workflow name…")
        self.search.textChanged.connect(self.refresh)
        self.status_filter = QComboBox()
        self.status_filter.addItems(["all", "success", "failed", "cancelled", "running"])
        self.status_filter.currentTextChanged.connect(self.refresh)
        self.rerun_btn = QPushButton("Rerun failed DAGs only")
        self.rerun_btn.setEnabled(False)
        self.rerun_btn.clicked.connect(self._request_rerun)
        filters.addWidget(self.search, 1)
        filters.addWidget(self.status_filter)
        filters.addWidget(self.rerun_btn)
        layout.addLayout(filters)

        splitter = QSplitter(Qt.Vertical)
        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(["Workflow", "Status", "Started", "Finished", "Error"])
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.itemSelectionChanged.connect(self._show_details)
        splitter.addWidget(self.table)

        details = QWidget()
        details_layout = QVBoxLayout(details)
        details_layout.setContentsMargins(0, 0, 0, 0)
        self.nodes_table = QTableWidget(0, 6)
        self.nodes_table.setHorizontalHeaderLabels(
            ["DAG", "Status", "Run-id", "Duration", "Retries", "Error"]
        )
        self.nodes_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        self.nodes_table.horizontalHeader().setStretchLastSection(True)
        self.nodes_table.verticalHeader().setVisible(False)
        self.nodes_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.nodes_table.setSelectionBehavior(QTableWidget.SelectRows)
        self.nodes_table.itemSelectionChanged.connect(self._show_node_output)
        details_layout.addWidget(self.nodes_table)
        self.output_view = QPlainTextEdit()
        self.output_view.setReadOnly(True)
        self.output_view.setPlaceholderText("Select a DAG row to see its command and output…")
        details_layout.addWidget(self.output_view)
        splitter.addWidget(details)
        layout.addWidget(splitter, 1)

        self._executions: list[dict] = []
        self._node_execs: list = []
        self.refresh()

    def refresh(self) -> None:
        status = self.status_filter.currentText()
        self._executions = self._repo.list_history(
            search=self.search.text().strip(),
            status="" if status == "all" else status,
        )
        self.table.setRowCount(0)
        for ex in self._executions:
            row = self.table.rowCount()
            self.table.insertRow(row)
            for col, key in enumerate(["workflow_name", "status", "started_at", "finished_at", "error"]):
                self.table.setItem(row, col, QTableWidgetItem(str(ex.get(key, ""))))
        self.nodes_table.setRowCount(0)
        self.output_view.clear()
        self.rerun_btn.setEnabled(False)

    def _selected_execution(self) -> dict | None:
        row = self.table.currentRow()
        return self._executions[row] if 0 <= row < len(self._executions) else None

    def _show_details(self) -> None:
        ex = self._selected_execution()
        self.nodes_table.setRowCount(0)
        self.output_view.clear()
        if ex is None:
            return
        self.rerun_btn.setEnabled(ex["status"] == "failed")
        self._node_execs = self._repo.get_node_executions(ex["id"])
        for ne in self._node_execs:
            row = self.nodes_table.rowCount()
            self.nodes_table.insertRow(row)
            self.nodes_table.setItem(row, 0, QTableWidgetItem(ne.dag_id))
            self.nodes_table.setItem(row, 1, QTableWidgetItem(ne.status.upper()))
            self.nodes_table.setItem(row, 2, QTableWidgetItem(ne.airflow_run_id))
            self.nodes_table.setItem(row, 3, QTableWidgetItem(f"{ne.duration_seconds:.0f}s"))
            self.nodes_table.setItem(row, 4, QTableWidgetItem(str(ne.retry_count)))
            self.nodes_table.setItem(row, 5, QTableWidgetItem(ne.error[:200]))

    def _show_node_output(self) -> None:
        row = self.nodes_table.currentRow()
        if not (0 <= row < len(self._node_execs)):
            return
        ne = self._node_execs[row]
        self.output_view.setPlainText(
            f"COMMAND:\n{ne.command}\n\n"
            f"ERROR:\n{ne.error or '(none)'}\n\n"
            f"STDOUT:\n{ne.stdout or '(empty)'}\n\n"
            f"STDERR:\n{ne.stderr or '(empty)'}"
        )

    def _request_rerun(self) -> None:
        ex = self._selected_execution()
        if ex:
            self.rerun_failed_requested.emit(ex["id"])
            self.accept()


class VersionsDialog(QDialog):
    """Browse and restore previous saved versions of a workflow."""

    def __init__(self, repo: WorkflowRepository, workflow_id: str, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Workflow versions")
        self.resize(700, 480)
        self._repo = repo
        self._workflow_id = workflow_id
        self.restored_version_row_id: int | None = None

        layout = QVBoxLayout(self)
        self.table = QTableWidget(0, 2)
        self.table.setHorizontalHeaderLabels(["Version", "Saved at"])
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.itemSelectionChanged.connect(self._preview)
        layout.addWidget(self.table)
        self.preview = QPlainTextEdit()
        self.preview.setReadOnly(True)
        layout.addWidget(self.preview)

        buttons = QDialogButtonBox(QDialogButtonBox.Cancel)
        restore_btn = QPushButton("Restore selected version")
        restore_btn.setObjectName("primary")
        buttons.addButton(restore_btn, QDialogButtonBox.AcceptRole)
        buttons.accepted.connect(self._restore)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self._versions = self._repo.list_versions(workflow_id)
        for v in self._versions:
            row = self.table.rowCount()
            self.table.insertRow(row)
            self.table.setItem(row, 0, QTableWidgetItem(f"v{v['version']}"))
            self.table.setItem(row, 1, QTableWidgetItem(v["created_at"]))

    def _selected(self) -> dict | None:
        row = self.table.currentRow()
        return self._versions[row] if 0 <= row < len(self._versions) else None

    def _preview(self) -> None:
        v = self._selected()
        if v:
            wf = self._repo.get_version(v["id"])
            if wf:
                summary = {
                    "name": wf.name,
                    "nodes": [n.dag_id for n in wf.nodes],
                    "edges": len(wf.edges),
                }
                self.preview.setPlainText(json.dumps(summary, indent=2))

    def _restore(self) -> None:
        v = self._selected()
        if v:
            self.restored_version_row_id = v["id"]
            self.accept()
