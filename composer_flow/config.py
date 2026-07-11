"""Application configuration: paths, constants and defaults.

Everything user-configurable lives in the SQLite settings table and is edited
through the GUI (Settings dialog) - no manual config files are required.
"""
from __future__ import annotations

import os
from pathlib import Path

APP_NAME = "ComposerFlow"


def app_data_dir() -> Path:
    """Per-user writable data directory (survives .exe relocation)."""
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / ".local" / "share")
    return Path(base) / APP_NAME


def db_path() -> Path:
    return app_data_dir() / "composerflow.db"


def log_dir() -> Path:
    return app_data_dir() / "logs"


def ensure_app_dirs() -> None:
    app_data_dir().mkdir(parents=True, exist_ok=True)
    log_dir().mkdir(parents=True, exist_ok=True)


# Fixed environment profiles selectable from the toolbar dropdown.
ENVIRONMENT_PROFILES = ("BLD", "INT", "PRE", "PRD")

# Optional hardcoded values per profile. Fill these in and rebuild the exe to
# ship fixed targets; they seed the Settings screen and can still be
# overridden there (overrides are stored in SQLite).
HARDCODED_PROFILES: dict[str, dict[str, str]] = {
    "BLD": {"environment": "", "location": "", "project": ""},
    "INT": {"environment": "", "location": "", "project": ""},
    "PRE": {"environment": "", "location": "", "project": ""},
    "PRD": {"environment": "", "location": "", "project": ""},
}

# Default values for the settings table. Keys are stable identifiers used by
# SettingsRepository; the GUI exposes all of them.
DEFAULT_SETTINGS: dict[str, str] = {
    "active_profile": "BLD",
    # legacy single-target keys, kept as a fallback for pre-profile databases
    "composer_environment": "",
    "composer_location": "",
    "gcp_project": "",
    "poll_interval_seconds": "20",
    "trigger_timeout_seconds": "300",
    "poll_timeout_seconds": "180",
    "max_parallel_dags": "4",
    "cli_retry_count": "2",
    "cli_retry_backoff_seconds": "5",
    "confirm_before_run": "1",
    # keep Windows awake while a workflow is executing (prevents sleep from
    # freezing the polling of a long run)
    "keep_awake_during_run": "1",
}

for _name, _vals in HARDCODED_PROFILES.items():
    DEFAULT_SETTINGS[f"profile_{_name}_environment"] = _vals["environment"]
    DEFAULT_SETTINGS[f"profile_{_name}_location"] = _vals["location"]
    DEFAULT_SETTINGS[f"profile_{_name}_project"] = _vals["project"]

# Markers in gcloud/kubectl stderr that indicate a transient (retryable) error.
TRANSIENT_ERROR_MARKERS = (
    "deadline exceeded",
    "timeout",
    "timed out",
    "unavailable",
    "connection reset",
    "connection aborted",
    "temporarily unavailable",
    "error dialing backend",
    "tls handshake",
    "socket",
    "503",
    "502",
    "429",
)
