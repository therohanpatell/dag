"""EXE entry point - starts the local web server and opens the browser.

Double-click ComposerFlow.exe → a tiny standard-library HTTP server starts on
a free local port and the default browser opens to the app. No console, no
manual commands, nothing to install (uses the browser already on the machine).
"""
from __future__ import annotations

import socket
import threading
import time
import webbrowser

from composer_flow.config import ensure_app_dirs
from composer_flow.utils.logger import get_logger, setup_logging
from composer_flow.webapp.server import run_server


def _free_port(preferred: int = 8760) -> int:
    for port in (preferred, 8761, 8762, 8763, 0):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            try:
                sock.bind(("127.0.0.1", port))
                return sock.getsockname()[1]
            except OSError:
                continue
    return preferred


def _open_when_ready(url: str, port: int) -> None:
    for _ in range(120):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            if sock.connect_ex(("127.0.0.1", port)) == 0:
                webbrowser.open(url)
                return
        time.sleep(0.3)


def main() -> int:
    ensure_app_dirs()
    setup_logging()
    log = get_logger("main")
    port = _free_port()
    url = f"http://127.0.0.1:{port}"
    httpd = run_server(port)
    threading.Thread(target=_open_when_ready, args=(url, port), daemon=True).start()
    log.info("ComposerFlow ready at %s", url)
    print(f"ComposerFlow running at {url}  (close this window to quit)")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
