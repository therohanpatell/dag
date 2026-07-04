"""Dock panels: workflow list (with search), DAG properties (with JSON
preview), execution console, and execution timeline."""
from __future__ import annotations

import json
from datetime import datetime

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPlainTextEdit,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from composer_flow.models.execution import NodeStatus
from composer_flow.models.workflow import DagNode, Workflow
from composer_flow.ui.theme import LOG_COLORS, LOG_TIMESTAMP, STATUS_COLORS


class StepRibbon(QWidget):
    """Numbered workflow ribbon: 1 Design → 2 Configure → 3 Validate → 4 Run.

    Always visible under the toolbar so a first-time user can see where they
    are in the process. The main window calls set_step()/mark_done().
    """

    STEPS = [
        ("1  Design", "Add DAG nodes and connect them with arrows"),
        ("2  Configure", "Set each DAG's ID and parameters in the right panel"),
        ("3  Validate", "Check the workflow for cycles and missing DAG IDs"),
        ("4  Run", "Trigger the DAGs in Cloud Composer and watch progress"),
    ]

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 4, 12, 4)
        layout.setSpacing(8)
        self._labels: list[QLabel] = []
        for i, (text, tip) in enumerate(self.STEPS):
            if i:
                arrow = QLabel("→")
                arrow.setObjectName("stepArrow")
                layout.addWidget(arrow)
            label = QLabel(text)
            label.setToolTip(tip)
            label.setObjectName("stepIdle")
            layout.addWidget(label)
            self._labels.append(label)
        layout.addStretch(1)
        self.set_step(1)

    def set_step(self, step: int) -> None:
        """Highlight the active step (1-4); earlier steps show as done."""
        step = max(1, min(step, len(self._labels)))
        for i, label in enumerate(self._labels, start=1):
            if i < step:
                label.setObjectName("stepDone")
            elif i == step:
                label.setObjectName("stepActive")
            else:
                label.setObjectName("stepIdle")
            # re-polish so the QSS object-name style applies
            label.style().unpolish(label)
            label.style().polish(label)


class WorkflowListPanel(QWidget):
    workflow_activated = Signal(str)   # workflow id
    new_requested = Signal()
    delete_requested = Signal(str)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)

        self.search = QLineEdit()
        self.search.setPlaceholderText("Search workflows…")
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self._apply_filter)
        layout.addWidget(self.search)

        self.list = QListWidget()
        self.list.itemActivated.connect(self._emit_activated)
        self.list.itemClicked.connect(self._emit_activated)
        layout.addWidget(self.list, 1)

        buttons = QHBoxLayout()
        new_btn = QPushButton("New")
        new_btn.clicked.connect(self.new_requested.emit)
        del_btn = QPushButton("Delete")
        del_btn.setObjectName("danger")
        del_btn.clicked.connect(self._emit_delete)
        buttons.addWidget(new_btn)
        buttons.addWidget(del_btn)
        layout.addLayout(buttons)

    def set_workflows(self, summaries: list[dict], selected_id: str = "") -> None:
        self.list.clear()
        for s in summaries:
            item = QListWidgetItem(f"{s['name']}   ({s['node_count']} DAGs)")
            item.setData(Qt.UserRole, s["id"])
            item.setToolTip(s.get("description") or s["name"])
            self.list.addItem(item)
            if s["id"] == selected_id:
                item.setSelected(True)
        self._apply_filter(self.search.text())

    def _apply_filter(self, text: str) -> None:
        needle = text.strip().lower()
        for i in range(self.list.count()):
            item = self.list.item(i)
            item.setHidden(bool(needle) and needle not in item.text().lower())

    def _emit_activated(self, item: QListWidgetItem) -> None:
        self.workflow_activated.emit(item.data(Qt.UserRole))

    def _emit_delete(self) -> None:
        item = self.list.currentItem()
        if item:
            self.delete_requested.emit(item.data(Qt.UserRole))


class PropertiesPanel(QWidget):
    """Edits the selected DAG node: DAG ID, run name and unlimited key/value
    parameters, with a live JSON preview of the generated --conf payload."""

    node_edited = Signal(str)  # node id

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._node: DagNode | None = None
        self._updating = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)

        layout.addWidget(QLabel("DAG ID"))
        self.dag_id_edit = QLineEdit()
        self.dag_id_edit.setPlaceholderText("e.g. daily_sales_load")
        self.dag_id_edit.textEdited.connect(self._apply)
        layout.addWidget(self.dag_id_edit)

        layout.addWidget(QLabel("Run name (optional)"))
        self.run_name_edit = QLineEdit()
        self.run_name_edit.setPlaceholderText("Friendly label; embedded in run-id")
        self.run_name_edit.textEdited.connect(self._apply)
        layout.addWidget(self.run_name_edit)

        header = QHBoxLayout()
        header.addWidget(QLabel("Parameters (--conf)"), 1)
        add_btn = QPushButton("Add")
        add_btn.setToolTip("Add a parameter row")
        add_btn.clicked.connect(self._add_row)
        remove_btn = QPushButton("Remove")
        remove_btn.setToolTip("Remove the selected parameter row")
        remove_btn.clicked.connect(self._remove_row)
        header.addWidget(add_btn)
        header.addWidget(remove_btn)
        layout.addLayout(header)

        self.params_table = QTableWidget(0, 2)
        self.params_table.setHorizontalHeaderLabels(["Key", "Value"])
        self.params_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.params_table.verticalHeader().setVisible(False)
        self.params_table.itemChanged.connect(self._apply)
        layout.addWidget(self.params_table, 2)

        layout.addWidget(QLabel("Generated JSON"))
        self.json_preview = QPlainTextEdit()
        self.json_preview.setReadOnly(True)
        self.json_preview.setMaximumHeight(150)
        layout.addWidget(self.json_preview, 1)

        self.setEnabled(False)

    def show_node(self, node: DagNode | None) -> None:
        self._updating = True
        self._node = node
        self.setEnabled(node is not None)
        if node is None:
            self.dag_id_edit.clear()
            self.run_name_edit.clear()
            self.params_table.setRowCount(0)
            self.json_preview.clear()
        else:
            self.dag_id_edit.setText(node.dag_id)
            self.run_name_edit.setText(node.run_name)
            self.params_table.setRowCount(0)
            for key, value in node.params.items():
                self._append_row(key, value)
            self._refresh_preview()
        self._updating = False

    def _append_row(self, key: str = "", value: str = "") -> None:
        row = self.params_table.rowCount()
        self.params_table.insertRow(row)
        self.params_table.setItem(row, 0, QTableWidgetItem(key))
        self.params_table.setItem(row, 1, QTableWidgetItem(value))

    def _add_row(self) -> None:
        if self._node is None:
            return
        self._updating = True
        self._append_row()
        self._updating = False
        self.params_table.editItem(self.params_table.item(self.params_table.rowCount() - 1, 0))

    def _remove_row(self) -> None:
        row = self.params_table.currentRow()
        if row >= 0:
            self.params_table.removeRow(row)
            self._apply()

    def _collect_params(self) -> dict[str, str]:
        params: dict[str, str] = {}
        for row in range(self.params_table.rowCount()):
            key_item = self.params_table.item(row, 0)
            value_item = self.params_table.item(row, 1)
            key = (key_item.text() if key_item else "").strip()
            if key:
                params[key] = (value_item.text() if value_item else "").strip()
        return params

    def _apply(self, *_args) -> None:
        if self._updating or self._node is None:
            return
        self._node.dag_id = self.dag_id_edit.text().strip()
        self._node.run_name = self.run_name_edit.text().strip()
        self._node.params = self._collect_params()
        self._refresh_preview()
        self.node_edited.emit(self._node.id)

    def _refresh_preview(self) -> None:
        if self._node is not None:
            self.json_preview.setPlainText(
                json.dumps(self._node.params, indent=2, ensure_ascii=False)
            )


class ConsolePanel(QWidget):
    """Color-coded execution console with live logs."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        self.output = QTextEdit()
        self.output.setReadOnly(True)
        self.output.setLineWrapMode(QTextEdit.NoWrap)
        layout.addWidget(self.output)
        controls = QHBoxLayout()
        clear_btn = QPushButton("Clear")
        clear_btn.clicked.connect(self.output.clear)
        controls.addStretch(1)
        controls.addWidget(clear_btn)
        layout.addLayout(controls)

    def append(self, level: str, message: str) -> None:
        color = LOG_COLORS.get(level, "#e5e7eb")
        stamp = datetime.now().strftime("%H:%M:%S")
        safe = (
            message.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace("\n", "<br>&nbsp;&nbsp;")
        )
        self.output.append(
            f'<span style="color:{LOG_TIMESTAMP}">[{stamp}]</span> '
            f'<span style="color:{color}">{safe}</span>'
        )
        self.output.verticalScrollBar().setValue(self.output.verticalScrollBar().maximum())


class TimelinePanel(QWidget):
    """Per-node execution timeline: status, run-id, start/end, duration."""

    COLS = ["DAG", "Run name", "Status", "Airflow run-id", "Started", "Finished", "Duration"]

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        self.table = QTableWidget(0, len(self.COLS))
        self.table.setHorizontalHeaderLabels(self.COLS)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        layout.addWidget(self.table)
        self._rows: dict[str, int] = {}

    def load_workflow(self, workflow: Workflow) -> None:
        self.table.setRowCount(0)
        self._rows = {}
        for node in workflow.nodes:
            row = self.table.rowCount()
            self.table.insertRow(row)
            self._rows[node.id] = row
            self.table.setItem(row, 0, QTableWidgetItem(node.dag_id))
            self.table.setItem(row, 1, QTableWidgetItem(node.run_name))
            for col in range(2, len(self.COLS)):
                self.table.setItem(row, col, QTableWidgetItem(""))
            self.update_node(node.id, NodeStatus.PENDING.value)

    def update_node(
        self,
        node_id: str,
        status: str,
        run_id: str = "",
        started: str = "",
        finished: str = "",
        duration: float = 0.0,
    ) -> None:
        row = self._rows.get(node_id)
        if row is None:
            return
        status_item = QTableWidgetItem(status.upper())
        _, border, _ = STATUS_COLORS.get(status, STATUS_COLORS[NodeStatus.PENDING.value])
        status_item.setForeground(QColor(border))
        self.table.setItem(row, 2, status_item)
        if run_id:
            self.table.setItem(row, 3, QTableWidgetItem(run_id))
        if started:
            self.table.setItem(row, 4, QTableWidgetItem(started))
        if finished:
            self.table.setItem(row, 5, QTableWidgetItem(finished))
        if duration:
            self.table.setItem(row, 6, QTableWidgetItem(f"{duration:.0f}s"))
