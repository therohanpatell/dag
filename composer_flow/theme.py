"""Single corporate theme — teal/green on white. UI-toolkit-free.

Holds only color constants so both the graph rendering (graphviz) and the
Streamlit CSS share one palette. No Qt, no Streamlit imports here.
"""
from __future__ import annotations

from composer_flow.models.execution import NodeStatus

ACCENT = "#0f766e"          # teal-700 — primary actions, selection
ACCENT_HOVER = "#115e59"    # teal-800
ACCENT_SOFT = "#ccfbf1"     # teal-100
SURFACE = "#ffffff"
BACKGROUND = "#f4f8f8"
BORDER = "#d7e0e2"
TEXT = "#1f2937"
TEXT_MUTED = "#6b7a80"

# node fill / border / text per status (used for the graphviz workflow view)
STATUS_COLORS: dict[str, tuple[str, str, str]] = {
    NodeStatus.PENDING.value:    ("#ffffff", "#9aa8ae", "#5b6b71"),
    NodeStatus.TRIGGERING.value: ("#fffbeb", "#d97706", "#92400e"),
    NodeStatus.QUEUED.value:     ("#fefce8", "#ca8a04", "#854d0e"),
    NodeStatus.RUNNING.value:    ("#eff6ff", "#2563eb", "#1e40af"),
    NodeStatus.SUCCESS.value:    ("#f0fdf4", "#16a34a", "#166534"),
    NodeStatus.FAILED.value:     ("#fef2f2", "#dc2626", "#991b1b"),
    NodeStatus.SKIPPED.value:    ("#f1f5f9", "#94a3b8", "#64748b"),
    NodeStatus.CANCELLED.value:  ("#f5f5f4", "#a8a29e", "#57534e"),
}

# console log line colors
LOG_COLORS: dict[str, str] = {
    "info": "#0e7490",
    "success": "#15803d",
    "warning": "#b45309",
    "error": "#b91c1c",
}

# short status badges shown in tables / node captions
STATUS_ICON: dict[str, str] = {
    NodeStatus.PENDING.value: "⚪",
    NodeStatus.TRIGGERING.value: "🟠",
    NodeStatus.QUEUED.value: "🟡",
    NodeStatus.RUNNING.value: "🔵",
    NodeStatus.SUCCESS.value: "🟢",
    NodeStatus.FAILED.value: "🔴",
    NodeStatus.SKIPPED.value: "⚫",
    NodeStatus.CANCELLED.value: "⚫",
}
