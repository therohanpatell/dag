"""Framework-agnostic engine events.

The execution engine emits these onto a thread-safe queue instead of talking
to any particular UI toolkit. Both front-ends consume the same stream:

- Streamlit drains the queue on each auto-refresh.
- The Qt desktop UI drains it from a QTimer.

Keeping the engine UI-free means the careful scheduling logic lives in exactly
one place and is unit-testable without importing a GUI.
"""
from __future__ import annotations

from dataclasses import dataclass

# event kinds
NODE_STATUS = "node_status"   # node_id, status
LOG = "log"                   # level, message
PROGRESS = "progress"         # done, total
ETA = "eta"                   # text
FINISHED = "finished"         # status, error


@dataclass
class EngineEvent:
    type: str
    node_id: str = ""
    status: str = ""
    level: str = ""
    message: str = ""
    done: int = 0
    total: int = 0
    text: str = ""
    error: str = ""
