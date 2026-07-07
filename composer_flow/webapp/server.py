"""Standard-library HTTP server for the ComposerFlow web app.

No third-party web framework — ThreadingHTTPServer + BaseHTTPRequestHandler.
Serves the static frontend and a small JSON API backed by the shared
composer_flow backend. The browser polls /api/run-state during execution for
live status (no websockets needed).
"""
from __future__ import annotations

import json
import mimetypes
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from composer_flow.config import ENVIRONMENT_PROFILES
from composer_flow.core import graph as g
from composer_flow.models.execution import NodeStatus
from composer_flow.models.workflow import Workflow, new_id
from composer_flow.utils.logger import get_logger
from composer_flow.webapp import appstate

log = get_logger("server")

STATIC_DIR = Path(__file__).resolve().parent / "static"

# cached gcloud auth (checking spawns a subprocess; refresh on demand)
_auth_cache: dict = {"checked": False, "authenticated": False, "account": "", "error": ""}
_auth_lock = threading.Lock()


def _auth_status(force: bool = False) -> dict:
    with _auth_lock:
        if _auth_cache["checked"] and not force:
            return dict(_auth_cache)
    try:
        st = appstate.build_gcloud().check_auth()
        result = {"checked": True, "authenticated": st.authenticated,
                  "account": st.account, "project": st.project, "error": st.error}
    except Exception as exc:
        result = {"checked": True, "authenticated": False, "account": "",
                  "project": "", "error": str(exc)}
    with _auth_lock:
        _auth_cache.update(result)
    return dict(result)


def _target_dict() -> dict:
    t = appstate.target()
    return {"environment": t.environment, "location": t.location,
            "project": t.project, "complete": t.is_complete()}


class Handler(BaseHTTPRequestHandler):
    server_version = "ComposerFlow"

    # -- helpers ---------------------------------------------------------- #

    def _send_json(self, obj, status=200) -> None:
        body = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length", 0))
        if not length:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError:
            return {}

    def log_message(self, *args) -> None:  # quiet default logging
        pass

    # -- static ----------------------------------------------------------- #

    def _serve_static(self, path: str) -> None:
        rel = path[len("/static/"):] if path.startswith("/static/") else "index.html"
        target = (STATIC_DIR / rel).resolve()
        if not str(target).startswith(str(STATIC_DIR)) or not target.is_file():
            self.send_error(404)
            return
        ctype = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        data = target.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    # -- routing ---------------------------------------------------------- #

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path, qs = parsed.path, parse_qs(parsed.query)
        try:
            if path in ("/", "/index.html"):
                self._serve_static("index.html")
            elif path.startswith("/static/"):
                self._serve_static(path)
            elif path == "/api/bootstrap":
                self._api_bootstrap()
            elif path == "/api/workflow":
                self._api_get_workflow(qs.get("id", [""])[0])
            elif path == "/api/auth":
                self._send_json(_auth_status(force=qs.get("force", ["0"])[0] == "1"))
            elif path == "/api/run-state":
                self._send_json(appstate.run_state_snapshot())
            elif path == "/api/history":
                self._api_history(qs)
            elif path == "/api/execution":
                self._api_execution(qs.get("id", [""])[0])
            else:
                self.send_error(404)
        except Exception as exc:
            log.exception("GET %s failed", path)
            self._send_json({"error": str(exc)}, status=500)

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        try:
            body = self._read_json()
            if path == "/api/workflow/save":
                self._api_save_workflow(body)
            elif path == "/api/workflow/new":
                self._api_new_workflow(body)
            elif path == "/api/workflow/delete":
                self._api_delete_workflow(body)
            elif path == "/api/workflow/import":
                self._api_import_workflow(body)
            elif path == "/api/validate":
                self._api_validate(body)
            elif path == "/api/settings/save":
                self._api_save_settings(body)
            elif path == "/api/profile":
                appstate.settings().set("active_profile", body.get("name", "BLD"))
                self._send_json({"ok": True, "target": _target_dict()})
            elif path == "/api/auth/login":
                self._api_login()
            elif path == "/api/run":
                self._api_run(body)
            elif path == "/api/cancel":
                appstate.cancel_run()
                self._send_json({"ok": True})
            elif path == "/api/rerun-failed":
                self._api_rerun_failed(body)
            else:
                self.send_error(404)
        except Exception as exc:
            log.exception("POST %s failed", path)
            self._send_json({"error": str(exc)}, status=500)

    # -- API handlers ----------------------------------------------------- #

    def _api_bootstrap(self) -> None:
        s = appstate.settings()
        self._send_json({
            "profiles": list(ENVIRONMENT_PROFILES),
            "active_profile": appstate.active_profile(),
            "target": _target_dict(),
            "auth": _auth_status(),
            "settings": s.all(),
            "workflows": appstate.workflows().list_summaries(),
        })

    def _api_get_workflow(self, workflow_id: str) -> None:
        wf = appstate.workflows().get(workflow_id) if workflow_id else None
        self._send_json(wf.to_dict() if wf else {})

    def _api_save_workflow(self, body: dict) -> None:
        wf = Workflow.from_dict(body)
        appstate.workflows().save(wf)
        self._send_json({"ok": True, "id": wf.id,
                         "workflows": appstate.workflows().list_summaries()})

    def _api_new_workflow(self, body: dict) -> None:
        wf = Workflow(name=(body.get("name") or "New Workflow").strip())
        appstate.workflows().save(wf)
        self._send_json({"ok": True, "id": wf.id,
                         "workflows": appstate.workflows().list_summaries()})

    def _api_delete_workflow(self, body: dict) -> None:
        appstate.workflows().delete(body.get("id", ""))
        self._send_json({"ok": True,
                         "workflows": appstate.workflows().list_summaries()})

    def _api_import_workflow(self, body: dict) -> None:
        wf = Workflow.from_dict(body.get("workflow", {}))
        old_to_new = {n.id: new_id() for n in wf.nodes}
        wf.id = new_id()
        for n in wf.nodes:
            n.id = old_to_new[n.id]
        for e in wf.edges:
            e.id = new_id()
            e.source = old_to_new.get(e.source, e.source)
            e.target = old_to_new.get(e.target, e.target)
        wf.name = f"{wf.name} (imported)"
        appstate.workflows().save(wf)
        self._send_json({"ok": True, "id": wf.id,
                         "workflows": appstate.workflows().list_summaries()})

    def _api_validate(self, body: dict) -> None:
        wf = Workflow.from_dict(body)
        issues = [{"level": i.level, "message": i.message} for i in g.validate(wf)]
        self._send_json({"issues": issues,
                         "ok": not any(i["level"] == "error" for i in issues)})

    def _api_save_settings(self, body: dict) -> None:
        s = appstate.settings()
        for key, value in body.items():
            s.set(key, str(value))
        self._send_json({"ok": True, "target": _target_dict()})

    def _api_login(self) -> None:
        try:
            appstate.build_gcloud().launch_login()
            self._send_json({"ok": True})
        except Exception as exc:
            self._send_json({"ok": False, "error": str(exc)})

    def _api_run(self, body: dict) -> None:
        if appstate.is_running():
            self._send_json({"error": "A workflow is already running."}, status=409)
            return
        wf = Workflow.from_dict(body)
        errors = [i.message for i in g.validate(wf) if i.is_error]
        if errors:
            self._send_json({"error": "; ".join(errors)}, status=400)
            return
        if not appstate.target().is_complete():
            self._send_json({"error": "Active environment profile is not configured."},
                            status=400)
            return
        appstate.workflows().save(wf)
        exec_id = appstate.start_run(wf)
        self._send_json({"ok": True, "execution_id": exec_id})

    def _api_rerun_failed(self, body: dict) -> None:
        if appstate.is_running():
            self._send_json({"error": "A workflow is already running."}, status=409)
            return
        execution_id = body.get("execution_id", "")
        ex = appstate.executions().get_execution(execution_id)
        if not ex:
            self._send_json({"error": "Execution not found."}, status=404)
            return
        wf = Workflow.from_json(ex["snapshot_json"])
        done = {r.node_id for r in appstate.executions().get_node_executions(execution_id)
                if r.status == NodeStatus.SUCCESS.value}
        exec_id = appstate.start_run(wf, resume_execution_id=execution_id, completed=done)
        self._send_json({"ok": True, "execution_id": exec_id, "workflow": wf.to_dict()})

    def _api_history(self, qs: dict) -> None:
        search = qs.get("search", [""])[0]
        status = qs.get("status", [""])[0]
        rows = appstate.executions().list_history(
            search=search, status="" if status in ("", "all") else status)
        self._send_json({"executions": rows})

    def _api_execution(self, execution_id: str) -> None:
        recs = appstate.executions().get_node_executions(execution_id)
        self._send_json({"nodes": [vars(r) for r in recs]})


def run_server(port: int, host: str = "127.0.0.1") -> ThreadingHTTPServer:
    httpd = ThreadingHTTPServer((host, port), Handler)
    log.info("ComposerFlow web server listening on http://%s:%s", host, port)
    return httpd
