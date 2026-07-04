"""ComposerFlow — visual workflow orchestrator for Google Cloud Composer DAGs.

Entry point. Initializes logging, the SQLite database, the Qt application,
performs the gcloud authentication check and launches the main window.
"""
from __future__ import annotations

import sys

from PySide6.QtWidgets import QApplication

from composer_flow.config import APP_NAME, APP_VERSION, ensure_app_dirs
from composer_flow.persistence.db import Database
from composer_flow.utils.logger import get_logger, setup_logging


def main() -> int:
    ensure_app_dirs()
    setup_logging()
    log = get_logger("main")
    log.info("Starting %s %s", APP_NAME, APP_VERSION)

    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setOrganizationName("ComposerFlow")

    db = Database()
    db.initialize()

    # Imported here so Qt is fully initialized before widgets are created.
    from composer_flow.ui.main_window import MainWindow

    window = MainWindow(db)
    window.show()
    window.run_startup_checks()

    rc = app.exec()
    log.info("%s exited with code %s", APP_NAME, rc)
    return rc


if __name__ == "__main__":
    sys.exit(main())
