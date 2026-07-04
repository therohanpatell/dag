"""Single corporate theme (teal/green on white) and shared status colors.

Design language: white surfaces, soft grey-teal background, one accent color
(#0F766E teal) for primary actions and highlights. No dark/light switching —
one professional look everywhere.
"""
from __future__ import annotations

from composer_flow.models.execution import NodeStatus

ACCENT = "#0f766e"          # teal-700 — primary actions, selection, ports
ACCENT_HOVER = "#115e59"    # teal-800
ACCENT_SOFT = "#ccfbf1"     # teal-100 — selected rows, chips
SURFACE = "#ffffff"
BACKGROUND = "#f4f8f8"
BORDER = "#d7e0e2"
TEXT = "#1f2937"
TEXT_MUTED = "#6b7a80"

CANVAS_BG = "#eef3f4"
CANVAS_GRID = "#dbe4e6"
EDGE_COLOR = "#7c8a90"
EDGE_SELECTED = ACCENT

# node card colors per status: (fill, border, status-text)
STATUS_COLORS = {
    NodeStatus.PENDING.value:    ("#ffffff", "#9aa8ae", "#5b6b71"),
    NodeStatus.TRIGGERING.value: ("#fffbeb", "#d97706", "#92400e"),
    NodeStatus.QUEUED.value:     ("#fefce8", "#ca8a04", "#854d0e"),
    NodeStatus.RUNNING.value:    ("#eff6ff", "#2563eb", "#1e40af"),
    NodeStatus.SUCCESS.value:    ("#f0fdf4", "#16a34a", "#166534"),
    NodeStatus.FAILED.value:     ("#fef2f2", "#dc2626", "#991b1b"),
    NodeStatus.SKIPPED.value:    ("#f1f5f9", "#94a3b8", "#64748b"),
    NodeStatus.CANCELLED.value:  ("#f5f5f4", "#a8a29e", "#57534e"),
}

# console log colors (dark text on white console)
LOG_COLORS = {
    "info": "#0e7490",
    "success": "#15803d",
    "warning": "#b45309",
    "error": "#b91c1c",
}
LOG_TIMESTAMP = "#94a3b8"

CORPORATE_QSS = f"""
QWidget {{ background-color: {BACKGROUND}; color: {TEXT}; font-size: 13px;
           font-family: 'Segoe UI', sans-serif; }}
QMainWindow::separator {{ background: {BORDER}; width: 3px; height: 3px; }}

QToolBar {{ background: {SURFACE}; border: none; border-bottom: 1px solid {BORDER};
            spacing: 2px; padding: 5px 8px; }}
QToolButton {{ background: transparent; border-radius: 6px; padding: 7px 12px;
               color: {TEXT}; font-weight: 600; }}
QToolButton:hover {{ background: #e4efee; }}
QToolButton:pressed, QToolButton:checked {{ background: {ACCENT_SOFT}; color: {ACCENT_HOVER}; }}
QToolButton:disabled {{ color: #9fb0b5; }}

QDockWidget {{ titlebar-close-icon: none; }}
QDockWidget::title {{ background: {SURFACE}; padding: 7px 10px; border-bottom: 1px solid {BORDER};
                      font-weight: 600; color: {ACCENT_HOVER}; }}

QLineEdit, QPlainTextEdit, QTextEdit, QSpinBox, QComboBox {{
    background: {SURFACE}; border: 1px solid {BORDER}; border-radius: 6px; padding: 6px;
    selection-background-color: {ACCENT_SOFT}; selection-color: {TEXT};
}}
QLineEdit:focus, QSpinBox:focus, QComboBox:focus, QPlainTextEdit:focus {{
    border: 1.5px solid {ACCENT}; }}

QPushButton {{ background: {SURFACE}; border: 1px solid {BORDER}; border-radius: 6px;
               padding: 7px 16px; font-weight: 600; }}
QPushButton:hover {{ background: #eef5f4; border-color: {ACCENT}; }}
QPushButton:disabled {{ color: #9fb0b5; background: #f0f3f4; }}
QPushButton#primary {{ background: {ACCENT}; border-color: {ACCENT}; color: white; }}
QPushButton#primary:hover {{ background: {ACCENT_HOVER}; }}
QPushButton#danger {{ background: #ffffff; border-color: #dc2626; color: #b91c1c; }}
QPushButton#danger:hover {{ background: #fef2f2; }}

QListWidget, QTableWidget, QTreeWidget {{
    background: {SURFACE}; border: 1px solid {BORDER}; border-radius: 6px;
    alternate-background-color: #f7fafa;
}}
QListWidget::item {{ padding: 8px; border-radius: 4px; }}
QListWidget::item:selected, QTableWidget::item:selected {{
    background: {ACCENT_SOFT}; color: {TEXT}; }}
QHeaderView::section {{ background: #f0f5f5; border: none;
    border-bottom: 2px solid {BORDER}; padding: 7px; font-weight: 600; }}

QTabWidget::pane {{ border: 1px solid {BORDER}; border-radius: 6px; background: {SURFACE}; }}
QTabBar::tab {{ background: transparent; padding: 8px 18px; margin-right: 2px;
    border-bottom: 3px solid transparent; font-weight: 600; color: {TEXT_MUTED}; }}
QTabBar::tab:selected {{ color: {ACCENT_HOVER}; border-bottom: 3px solid {ACCENT}; }}

QProgressBar {{ background: {SURFACE}; border: 1px solid {BORDER}; border-radius: 6px;
                text-align: center; height: 18px; }}
QProgressBar::chunk {{ background: {ACCENT}; border-radius: 5px; }}

QMenu {{ background: {SURFACE}; border: 1px solid {BORDER}; }}
QMenu::item {{ padding: 7px 26px; }}
QMenu::item:selected {{ background: {ACCENT_SOFT}; }}

QStatusBar {{ background: {SURFACE}; border-top: 1px solid {BORDER}; color: {TEXT_MUTED}; }}
QGraphicsView {{ border: none; }}

QScrollBar:vertical {{ background: transparent; width: 10px; }}
QScrollBar::handle:vertical {{ background: #c3ced1; border-radius: 5px; min-height: 24px; }}
QScrollBar:horizontal {{ background: transparent; height: 10px; }}
QScrollBar::handle:horizontal {{ background: #c3ced1; border-radius: 5px; min-width: 24px; }}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; width: 0; }}
QToolTip {{ background: #10312e; color: #ffffff; border: none; padding: 6px; }}

QLabel#stepActive {{ background: {ACCENT}; color: white; border-radius: 13px;
    padding: 5px 16px; font-weight: 700; }}
QLabel#stepDone {{ background: {ACCENT_SOFT}; color: {ACCENT_HOVER}; border-radius: 13px;
    padding: 5px 16px; font-weight: 600; }}
QLabel#stepIdle {{ background: #e8eeef; color: {TEXT_MUTED}; border-radius: 13px;
    padding: 5px 16px; font-weight: 600; }}
QLabel#stepArrow {{ color: #b3c0c4; font-weight: 700; }}
"""


def qss() -> str:
    return CORPORATE_QSS
