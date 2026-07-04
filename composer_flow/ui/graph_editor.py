"""Visual drag-and-drop workflow graph editor.

QGraphicsScene/View based node editor:
- NodeItem: large draggable card (DAG id, run name, parameter preview and a
  status pill); input port on top, output port on bottom.
- EdgeItem: cubic bezier arrow between nodes; selectable/deletable.
- Drag from an output port to another node to create a dependency; the editor
  refuses self-loops, duplicates and edges that would create a cycle.
- Adding a node centers the camera on it and selects it.
- Dot-grid canvas with a built-in getting-started hint when empty.
- Double-click empty canvas to add a node; Delete removes selection;
  Ctrl+wheel zooms.
"""
from __future__ import annotations

import math

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import (
    QBrush,
    QColor,
    QFont,
    QPainter,
    QPainterPath,
    QPen,
    QPolygonF,
)
from PySide6.QtWidgets import (
    QGraphicsEllipseItem,
    QGraphicsItem,
    QGraphicsPathItem,
    QGraphicsScene,
    QGraphicsView,
    QMenu,
)

from composer_flow.core import graph as g
from composer_flow.models.execution import NodeStatus
from composer_flow.models.workflow import DagNode, Edge, Workflow
from composer_flow.ui.theme import (
    ACCENT,
    CANVAS_BG,
    CANVAS_GRID,
    EDGE_COLOR,
    EDGE_SELECTED,
    STATUS_COLORS,
    TEXT_MUTED,
)

NODE_W, NODE_H = 280.0, 130.0
PORT_R = 9.0
GRID_STEP = 28

EMPTY_HINT = (
    "Your workflow canvas is empty.\n\n"
    "1.  Double-click anywhere (or press “Add DAG Node”) to add a DAG.\n"
    "2.  Type its DAG ID and parameters in the panel on the right.\n"
    "3.  Drag from the ● port at the bottom of one DAG onto another\n"
    "     to define the execution order.\n"
    "4.  Press “Run Workflow” when you are ready."
)
NO_WORKFLOW_HINT = (
    "No workflow is open.\n\n"
    "Create one with “New Workflow” in the toolbar,\n"
    "or pick an existing workflow from the list on the left."
)


class PortItem(QGraphicsEllipseItem):
    def __init__(self, node_item: "NodeItem", is_output: bool) -> None:
        super().__init__(-PORT_R, -PORT_R, PORT_R * 2, PORT_R * 2, node_item)
        self.node_item = node_item
        self.is_output = is_output
        self.setBrush(QBrush(QColor(ACCENT if is_output else "#8fa6ab")))
        self.setPen(QPen(QColor("#ffffff"), 2))
        self.setPos(NODE_W / 2, NODE_H if is_output else 0)
        self.setAcceptHoverEvents(True)
        self.setZValue(3)
        self.setToolTip(
            "Drag from here onto another DAG to make it run after this one"
            if is_output else "Runs after the DAG(s) connected above"
        )

    def hoverEnterEvent(self, event) -> None:
        self.setBrush(QBrush(QColor("#0d9488")))
        self.setScale(1.25)
        super().hoverEnterEvent(event)

    def hoverLeaveEvent(self, event) -> None:
        self.setBrush(QBrush(QColor(ACCENT if self.is_output else "#8fa6ab")))
        self.setScale(1.0)
        super().hoverLeaveEvent(event)


class NodeItem(QGraphicsItem):
    def __init__(self, node: DagNode) -> None:
        super().__init__()
        self.node = node
        self.status = NodeStatus.PENDING.value
        self.is_current = False
        self.edges: list[EdgeItem] = []
        self.setFlags(
            QGraphicsItem.ItemIsMovable
            | QGraphicsItem.ItemIsSelectable
            | QGraphicsItem.ItemSendsGeometryChanges
        )
        self.setPos(node.x, node.y)
        self.setZValue(2)
        self.input_port = PortItem(self, is_output=False)
        self.output_port = PortItem(self, is_output=True)

    def boundingRect(self) -> QRectF:
        return QRectF(-4, -4, NODE_W + 8, NODE_H + 8)

    def set_status(self, status: str, is_current: bool = False) -> None:
        self.status = status
        self.is_current = is_current
        self.update()

    def paint(self, painter: QPainter, option, widget=None) -> None:
        fill, border, status_text = STATUS_COLORS.get(
            self.status, STATUS_COLORS[NodeStatus.PENDING.value]
        )
        rect = QRectF(0, 0, NODE_W, NODE_H)
        painter.setRenderHint(QPainter.Antialiasing)

        # soft drop shadow
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(16, 42, 40, 22))
        painter.drawRoundedRect(rect.translated(0, 2.5), 12, 12)

        # card
        selected = self.isSelected() or self.is_current
        pen = QPen(QColor(ACCENT if self.isSelected() else border),
                   2.6 if selected else 1.6)
        if self.is_current and not self.isSelected():
            pen.setStyle(Qt.DashLine)
        painter.setPen(pen)
        painter.setBrush(QBrush(QColor(fill)))
        painter.drawRoundedRect(rect, 12, 12)

        # colored status strip on the left edge
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(border))
        strip = QPainterPath()
        strip.addRoundedRect(QRectF(0, 0, 6, NODE_H), 3, 3)
        painter.drawPath(strip)

        # title: DAG id
        painter.setPen(QColor("#111827"))
        font = QFont("Segoe UI", 11, QFont.Bold)
        painter.setFont(font)
        title = self.node.dag_id or "(set DAG ID →)"
        painter.drawText(
            QRectF(18, 10, NODE_W - 32, 24), Qt.AlignLeft | Qt.AlignVCenter,
            painter.fontMetrics().elidedText(title, Qt.ElideRight, int(NODE_W - 32)),
        )

        # run name
        painter.setPen(QColor(TEXT_MUTED))
        painter.setFont(QFont("Segoe UI", 9))
        sub = self.node.run_name or ""
        painter.drawText(
            QRectF(18, 34, NODE_W - 32, 16), Qt.AlignLeft | Qt.AlignVCenter,
            painter.fontMetrics().elidedText(sub, Qt.ElideRight, int(NODE_W - 32)),
        )

        # parameter preview (first 2)
        painter.setFont(QFont("Consolas", 8))
        y = 54.0
        items = list(self.node.params.items())
        for key, value in items[:2]:
            line = f"{key}: {value}"
            painter.drawText(
                QRectF(18, y, NODE_W - 32, 14), Qt.AlignLeft | Qt.AlignVCenter,
                painter.fontMetrics().elidedText(line, Qt.ElideRight, int(NODE_W - 32)),
            )
            y += 15
        if len(items) > 2:
            painter.drawText(QRectF(18, y, NODE_W - 32, 14),
                             Qt.AlignLeft | Qt.AlignVCenter,
                             f"+ {len(items) - 2} more parameter(s)")

        # status pill (bottom-left)
        painter.setFont(QFont("Segoe UI", 8, QFont.Bold))
        label = self.status.upper()
        metrics = painter.fontMetrics()
        pill_w = metrics.horizontalAdvance(label) + 22
        pill = QRectF(18, NODE_H - 28, pill_w, 19)
        painter.setPen(QPen(QColor(border), 1.2))
        painter.setBrush(QColor(border).lighter(185) if self.status == "pending"
                         else QColor(fill).darker(103))
        painter.drawRoundedRect(pill, 9, 9)
        painter.setPen(QColor(status_text))
        painter.drawText(pill, Qt.AlignCenter, label)

    def itemChange(self, change, value):
        if change == QGraphicsItem.ItemPositionHasChanged:
            self.node.x = self.pos().x()
            self.node.y = self.pos().y()
            for edge in self.edges:
                edge.update_path()
            scene = self.scene()
            if isinstance(scene, WorkflowScene):
                scene.editor.mark_dirty()
        return super().itemChange(change, value)


class EdgeItem(QGraphicsPathItem):
    def __init__(self, edge: Edge, source: NodeItem, target: NodeItem) -> None:
        super().__init__()
        self.edge = edge
        self.source_item = source
        self.target_item = target
        source.edges.append(self)
        target.edges.append(self)
        self.setFlag(QGraphicsItem.ItemIsSelectable)
        self.setZValue(1)
        self.update_path()

    def update_path(self) -> None:
        p1 = self.source_item.output_port.scenePos()
        p2 = self.target_item.input_port.scenePos()
        path = QPainterPath(p1)
        dy = max(abs(p2.y() - p1.y()) * 0.5, 40.0)
        c1 = QPointF(p1.x(), p1.y() + dy)
        c2 = QPointF(p2.x(), p2.y() - dy)
        path.cubicTo(c1, c2, p2)
        self.setPath(path)

    def paint(self, painter: QPainter, option, widget=None) -> None:
        painter.setRenderHint(QPainter.Antialiasing)
        color = QColor(EDGE_SELECTED) if self.isSelected() else QColor(EDGE_COLOR)
        painter.setPen(QPen(color, 2.6 if self.isSelected() else 2.0))
        painter.setBrush(Qt.NoBrush)
        painter.drawPath(self.path())

        # arrowhead near the target input port
        pct = self.path().percentAtLength(max(self.path().length() - 12.0, 0.0))
        tip = self.path().pointAtPercent(min(pct + 0.02, 1.0))
        base = self.path().pointAtPercent(pct)
        angle = math.atan2(tip.y() - base.y(), tip.x() - base.x())
        size = 11.0
        left = QPointF(
            tip.x() - size * math.cos(angle - math.pi / 6),
            tip.y() - size * math.sin(angle - math.pi / 6),
        )
        right = QPointF(
            tip.x() - size * math.cos(angle + math.pi / 6),
            tip.y() - size * math.sin(angle + math.pi / 6),
        )
        painter.setBrush(QBrush(color))
        painter.drawPolygon(QPolygonF([tip, left, right]))


class WorkflowScene(QGraphicsScene):
    def __init__(self, editor: "GraphEditor") -> None:
        super().__init__()
        self.editor = editor
        self.setSceneRect(-2000, -2000, 6000, 6000)


class GraphEditor(QGraphicsView):
    node_selected = Signal(str)     # node id ("" when selection cleared)
    graph_changed = Signal()
    request_add_node = Signal(QPointF)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._scene = WorkflowScene(self)
        self.setScene(self._scene)
        self.setRenderHint(QPainter.Antialiasing)
        self.setDragMode(QGraphicsView.RubberBandDrag)
        self.setTransformationAnchor(QGraphicsView.AnchorUnderMouse)
        self.setBackgroundBrush(QBrush(QColor(CANVAS_BG)))

        self.workflow: Workflow = Workflow()
        self.node_items: dict[str, NodeItem] = {}
        self.edge_items: dict[str, EdgeItem] = {}
        self.read_only = False
        self.has_open_workflow = False  # controls which empty hint is shown

        self._temp_edge: QGraphicsPathItem | None = None
        self._connect_source: NodeItem | None = None

        self._scene.selectionChanged.connect(self._on_selection_changed)

    # -- canvas painting -------------------------------------------------------

    def drawBackground(self, painter: QPainter, rect: QRectF) -> None:
        super().drawBackground(painter, rect)
        painter.setPen(QPen(QColor(CANVAS_GRID), 1.4))
        left = int(rect.left()) - (int(rect.left()) % GRID_STEP)
        top = int(rect.top()) - (int(rect.top()) % GRID_STEP)
        for x in range(left, int(rect.right()), GRID_STEP):
            for y in range(top, int(rect.bottom()), GRID_STEP):
                painter.drawPoint(x, y)

    def drawForeground(self, painter: QPainter, rect: QRectF) -> None:
        super().drawForeground(painter, rect)
        if self.node_items:
            return
        hint = EMPTY_HINT if self.has_open_workflow else NO_WORKFLOW_HINT
        visible = self.mapToScene(self.viewport().rect()).boundingRect()
        painter.setPen(QColor(TEXT_MUTED))
        painter.setFont(QFont("Segoe UI", 11))
        painter.drawText(visible, Qt.AlignCenter, hint)

    # -- load/save ------------------------------------------------------------

    def load_workflow(self, workflow: Workflow, is_open: bool = True) -> None:
        self._scene.clear()
        self.node_items.clear()
        self.edge_items.clear()
        self._temp_edge = None
        self._connect_source = None
        self.workflow = workflow
        self.has_open_workflow = is_open
        for node in workflow.nodes:
            item = NodeItem(node)
            self._scene.addItem(item)
            self.node_items[node.id] = item
        for edge in workflow.edges:
            self._add_edge_item(edge)
        if self.node_items:
            self.centerOn(next(iter(self.node_items.values())))
        self.viewport().update()
        self.graph_changed.emit()

    def _add_edge_item(self, edge: Edge) -> None:
        src = self.node_items.get(edge.source)
        dst = self.node_items.get(edge.target)
        if src and dst:
            item = EdgeItem(edge, src, dst)
            self._scene.addItem(item)
            self.edge_items[edge.id] = item

    def mark_dirty(self) -> None:
        self.graph_changed.emit()

    # -- editing operations -----------------------------------------------------

    def add_node(self, pos: QPointF | None = None) -> DagNode:
        """Add a node, center the camera on it and select it."""
        if pos is None:
            # place below the lowest existing node, or at origin
            if self.node_items:
                lowest = max(i.pos().y() for i in self.node_items.values())
                pos = QPointF(0.0, lowest + NODE_H + 90)
            else:
                pos = QPointF(0.0, 0.0)
        node = DagNode(dag_id="", x=pos.x(), y=pos.y())
        self.workflow.nodes.append(node)
        item = NodeItem(node)
        self._scene.addItem(item)
        self.node_items[node.id] = item
        self._scene.clearSelection()
        item.setSelected(True)
        self.centerOn(item)  # pin the view to the new node
        self.viewport().update()
        self.graph_changed.emit()
        return node

    def delete_selection(self) -> None:
        if self.read_only:
            return
        selected = self._scene.selectedItems()
        edges = [i for i in selected if isinstance(i, EdgeItem)]
        nodes = [i for i in selected if isinstance(i, NodeItem)]
        for node_item in nodes:
            edges.extend(node_item.edges)
        for edge_item in {id(e): e for e in edges}.values():
            self._remove_edge_item(edge_item)
        for node_item in nodes:
            self.workflow.nodes = [n for n in self.workflow.nodes if n.id != node_item.node.id]
            self.node_items.pop(node_item.node.id, None)
            self._scene.removeItem(node_item)
        if nodes or edges:
            self.node_selected.emit("")
            self.viewport().update()
            self.graph_changed.emit()

    def _remove_edge_item(self, edge_item: EdgeItem) -> None:
        self.workflow.edges = [e for e in self.workflow.edges if e.id != edge_item.edge.id]
        self.edge_items.pop(edge_item.edge.id, None)
        for node_item in (edge_item.source_item, edge_item.target_item):
            if edge_item in node_item.edges:
                node_item.edges.remove(edge_item)
        if edge_item.scene() is not None:
            self._scene.removeItem(edge_item)

    def try_create_edge(self, source_id: str, target_id: str) -> str:
        """Create edge if valid; return error message ('' on success)."""
        if source_id == target_id:
            return "A DAG cannot depend on itself."
        if any(e.source == source_id and e.target == target_id for e in self.workflow.edges):
            return "That dependency already exists."
        if g.would_create_cycle(self.workflow, source_id, target_id):
            return "That dependency would create a cycle."
        edge = Edge(source=source_id, target=target_id)
        self.workflow.edges.append(edge)
        self._add_edge_item(edge)
        self.graph_changed.emit()
        return ""

    def refresh_node(self, node_id: str) -> None:
        item = self.node_items.get(node_id)
        if item:
            item.update()

    # -- execution feedback ------------------------------------------------

    def set_node_status(self, node_id: str, status: str) -> None:
        item = self.node_items.get(node_id)
        if item:
            item.set_status(status, is_current=status in (
                NodeStatus.TRIGGERING.value, NodeStatus.RUNNING.value,
                NodeStatus.QUEUED.value,
            ))

    def reset_statuses(self) -> None:
        for item in self.node_items.values():
            item.set_status(NodeStatus.PENDING.value)

    def set_read_only(self, read_only: bool) -> None:
        self.read_only = read_only
        for item in self.node_items.values():
            item.setFlag(QGraphicsItem.ItemIsMovable, not read_only)

    # -- events ----------------------------------------------------------------

    def _on_selection_changed(self) -> None:
        try:
            selected = self._scene.selectedItems()
        except RuntimeError:  # scene already deleted during app shutdown
            return
        nodes = [i for i in selected if isinstance(i, NodeItem)]
        self.node_selected.emit(nodes[0].node.id if len(nodes) == 1 else "")

    def mousePressEvent(self, event) -> None:
        if not self.read_only and event.button() == Qt.LeftButton:
            item = self.itemAt(event.position().toPoint())
            if isinstance(item, PortItem) and item.is_output:
                self._connect_source = item.node_item
                self._temp_edge = QGraphicsPathItem()
                self._temp_edge.setPen(QPen(QColor(ACCENT), 2.2, Qt.DashLine))
                self._temp_edge.setZValue(5)
                self._scene.addItem(self._temp_edge)
                event.accept()
                return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:
        if self._temp_edge and self._connect_source:
            p1 = self._connect_source.output_port.scenePos()
            p2 = self.mapToScene(event.position().toPoint())
            path = QPainterPath(p1)
            dy = max(abs(p2.y() - p1.y()) * 0.5, 40.0)
            path.cubicTo(QPointF(p1.x(), p1.y() + dy), QPointF(p2.x(), p2.y() - dy), p2)
            self._temp_edge.setPath(path)
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        if self._temp_edge and self._connect_source:
            self._scene.removeItem(self._temp_edge)
            self._temp_edge = None
            source = self._connect_source
            self._connect_source = None
            target_item = None
            for item in self.items(event.position().toPoint()):
                if isinstance(item, PortItem):
                    target_item = item.node_item
                    break
                if isinstance(item, NodeItem):
                    target_item = item
                    break
            if target_item is not None and target_item is not source:
                error = self.try_create_edge(source.node.id, target_item.node.id)
                if error:
                    from PySide6.QtWidgets import QMessageBox

                    QMessageBox.warning(self, "Invalid dependency", error)
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event) -> None:
        if not self.read_only and self.itemAt(event.position().toPoint()) is None:
            self.request_add_node.emit(self.mapToScene(event.position().toPoint()))
            event.accept()
            return
        super().mouseDoubleClickEvent(event)

    def keyPressEvent(self, event) -> None:
        if event.key() in (Qt.Key_Delete, Qt.Key_Backspace):
            self.delete_selection()
            event.accept()
            return
        super().keyPressEvent(event)

    def wheelEvent(self, event) -> None:
        if event.modifiers() & Qt.ControlModifier:
            factor = 1.15 if event.angleDelta().y() > 0 else 1 / 1.15
            current = self.transform().m11()
            if 0.2 <= current * factor <= 3.0:
                self.scale(factor, factor)
            event.accept()
            return
        super().wheelEvent(event)

    def contextMenuEvent(self, event) -> None:
        if self.read_only:
            return
        menu = QMenu(self)
        add_action = menu.addAction("Add DAG node here")
        delete_action = menu.addAction("Delete selection")
        delete_action.setEnabled(bool(self._scene.selectedItems()))
        chosen = menu.exec(event.globalPos())
        if chosen == add_action:
            self.request_add_node.emit(self.mapToScene(event.pos()))
        elif chosen == delete_action:
            self.delete_selection()

    def auto_layout(self) -> None:
        """Arrange nodes by topological level (best effort; ignores cycles)."""
        try:
            levels = g.topological_levels(self.workflow)
        except ValueError:
            return
        for row, level in enumerate(levels):
            total_w = len(level) * (NODE_W + 70)
            for col, node_id in enumerate(level):
                item = self.node_items.get(node_id)
                if item:
                    item.setPos(col * (NODE_W + 70) - total_w / 2 + 140, row * (NODE_H + 100))
        if self.node_items:
            self.centerOn(next(iter(self.node_items.values())))
        self.graph_changed.emit()
