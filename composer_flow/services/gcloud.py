"""Production-safe wrapper around the Google Cloud CLI.

Why this design (CLI approaches compared):

  Trigger:  `gcloud composer environments run <env> dags trigger -- <dag_id>
             --run-id <id> --conf <json>`
            We ALWAYS generate our own --run-id. Without it, correlating the
            triggered run with later status checks is guesswork (execution
            dates are assigned server-side and racy under parallel triggers).

  Monitor:  Three CLI options exist on Composer 2.2 (Airflow 2.x):
              1. `dags state <dag_id> <execution_date>` — needs the exact
                 logical date, which we don't control precisely -> brittle.
              2. `dags list-runs -- -d <dag_id> -o json` — returns run_id,
                 state, start/end dates as JSON; we filter by our run-id.
                 Deterministic and covers queued/running/success/failed. BEST.
              3. Polling task states — excessive detail for orchestration.
            We use (2).

Robustness measures:
  - subprocess with argument LISTS, shell=False — no shell injection.
  - CREATE_NO_WINDOW so no console windows flash in the windowed .exe.
  - Timeouts on every call; bounded retries with backoff on transient errors.
  - `gcloud composer environments run` proxies the Airflow CLI through
    kubectl and mixes kubectl noise into stderr; extract_json() pulls the
    actual JSON payload out of the combined output.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

from composer_flow.config import TRANSIENT_ERROR_MARKERS
from composer_flow.models.execution import NodeStatus
from composer_flow.utils.logger import get_logger

log = get_logger("gcloud")

_IS_WINDOWS = sys.platform.startswith("win")
_NO_WINDOW = subprocess.CREATE_NO_WINDOW if _IS_WINDOWS else 0

# Airflow DagRun state -> internal NodeStatus
_AIRFLOW_STATE_MAP = {
    "queued": NodeStatus.QUEUED,
    "running": NodeStatus.RUNNING,
    "success": NodeStatus.SUCCESS,
    "failed": NodeStatus.FAILED,
}


class GcloudNotFoundError(RuntimeError):
    """gcloud CLI is not installed or not on PATH."""


class GcloudCommandError(RuntimeError):
    def __init__(self, message: str, result: "CommandResult") -> None:
        super().__init__(message)
        self.result = result


@dataclass
class CommandResult:
    command: list[str] = field(default_factory=list)
    returncode: int = -1
    stdout: str = ""
    stderr: str = ""
    duration_seconds: float = 0.0
    attempts: int = 1

    @property
    def ok(self) -> bool:
        return self.returncode == 0

    @property
    def command_str(self) -> str:
        return subprocess.list2cmdline(self.command)

    @property
    def combined_output(self) -> str:
        return f"{self.stdout}\n{self.stderr}"


@dataclass
class AuthStatus:
    authenticated: bool
    account: str = ""
    project: str = ""
    error: str = ""


@dataclass
class ComposerTarget:
    environment: str
    location: str
    project: str

    def is_complete(self) -> bool:
        return bool(self.environment and self.location and self.project)


def find_gcloud() -> str:
    """Locate the gcloud executable (gcloud.cmd on Windows)."""
    for candidate in ("gcloud.cmd", "gcloud.CMD", "gcloud"):
        path = shutil.which(candidate)
        if path:
            return path
    raise GcloudNotFoundError(
        "The 'gcloud' CLI was not found on PATH. Install the Google Cloud SDK "
        "and restart the application."
    )


def extract_json(text: str):
    """Extract the first valid JSON array/object embedded in noisy CLI output.

    Scans candidate start positions and uses bracket balancing (string-aware)
    so kubectl/log lines surrounding the payload don't break parsing.
    """
    for match in re.finditer(r"[\[{]", text):
        start = match.start()
        open_ch = text[start]
        close_ch = "]" if open_ch == "[" else "}"
        depth = 0
        in_string = False
        escaped = False
        for i in range(start, len(text)):
            ch = text[i]
            if in_string:
                if escaped:
                    escaped = False
                elif ch == "\\":
                    escaped = True
                elif ch == '"':
                    in_string = False
                continue
            if ch == '"':
                in_string = True
            elif ch == open_ch:
                depth += 1
            elif ch == close_ch:
                depth -= 1
                if depth == 0:
                    candidate = text[start : i + 1]
                    try:
                        return json.loads(candidate)
                    except json.JSONDecodeError:
                        break  # try next start position
    return None


def generate_run_id(run_name: str = "") -> str:
    """Unique, filter-friendly Airflow run id, e.g.
    cf__daily_load__20260703T101500__a1b2c3d4
    """
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    label = re.sub(r"[^A-Za-z0-9_-]", "_", run_name)[:40] if run_name else "run"
    return f"cf__{label}__{stamp}__{uuid.uuid4().hex[:8]}"


class GcloudClient:
    """All gcloud interaction goes through this class (single seam for tests:
    inject a fake runner)."""

    def __init__(
        self,
        retry_count: int = 2,
        retry_backoff_seconds: int = 5,
        runner=None,
    ) -> None:
        self.retry_count = max(0, retry_count)
        self.retry_backoff = max(1, retry_backoff_seconds)
        self._runner = runner or self._default_runner
        self._gcloud_path: str | None = None

    # -- low-level ------------------------------------------------------------

    def gcloud_path(self) -> str:
        if self._gcloud_path is None:
            self._gcloud_path = find_gcloud()
        return self._gcloud_path

    @staticmethod
    def _default_runner(cmd: list[str], timeout: int) -> subprocess.CompletedProcess:
        return subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            shell=False,
            creationflags=_NO_WINDOW,
        )

    def run(self, args: list[str], timeout: int = 120, retryable: bool = True) -> CommandResult:
        """Run a gcloud command with bounded retries on transient failures."""
        cmd = [self.gcloud_path(), *args]
        attempts = self.retry_count + 1 if retryable else 1
        last = CommandResult(command=cmd)
        for attempt in range(1, attempts + 1):
            start = time.monotonic()
            try:
                proc = self._runner(cmd, timeout)
                last = CommandResult(
                    command=cmd,
                    returncode=proc.returncode,
                    stdout=proc.stdout or "",
                    stderr=proc.stderr or "",
                    duration_seconds=round(time.monotonic() - start, 2),
                    attempts=attempt,
                )
            except subprocess.TimeoutExpired:
                last = CommandResult(
                    command=cmd,
                    returncode=-1,
                    stderr=f"Command timed out after {timeout}s",
                    duration_seconds=round(time.monotonic() - start, 2),
                    attempts=attempt,
                )
            except OSError as exc:
                last = CommandResult(
                    command=cmd,
                    returncode=-1,
                    stderr=f"Failed to launch gcloud: {exc}",
                    duration_seconds=round(time.monotonic() - start, 2),
                    attempts=attempt,
                )

            log.info(
                "CLI | attempt=%s rc=%s duration=%.2fs | %s",
                attempt, last.returncode, last.duration_seconds, last.command_str,
            )
            if last.ok:
                return last

            log.warning("CLI stderr: %s", last.stderr.strip()[:2000])
            if attempt < attempts and self._is_transient(last):
                log.info("Transient error — retrying in %ss", self.retry_backoff)
                time.sleep(self.retry_backoff)
            else:
                break
        return last

    @staticmethod
    def _is_transient(result: CommandResult) -> bool:
        blob = result.combined_output.lower()
        return any(marker in blob for marker in TRANSIENT_ERROR_MARKERS)

    # -- auth -----------------------------------------------------------------

    def check_auth(self) -> AuthStatus:
        """Verify an active gcloud credential exists (`gcloud auth list`)."""
        try:
            result = self.run(
                ["auth", "list", "--filter=status:ACTIVE", "--format=json"],
                timeout=60,
                retryable=False,
            )
        except GcloudNotFoundError as exc:
            return AuthStatus(False, error=str(exc))
        if not result.ok:
            return AuthStatus(False, error=result.stderr.strip() or "gcloud auth list failed")
        accounts = extract_json(result.stdout) or []
        if not accounts:
            return AuthStatus(False, error="No active gcloud account. Run 'gcloud auth login'.")
        account = accounts[0].get("account", "")

        proj = self.run(["config", "get-value", "project"], timeout=60, retryable=False)
        project = proj.stdout.strip() if proj.ok else ""
        if project in ("(unset)", "None"):
            project = ""
        return AuthStatus(True, account=account, project=project)

    def launch_login(self) -> None:
        """Start `gcloud auth login` hidden — it opens the browser OAuth flow
        directly and completes via a local callback server, so no console
        window is needed. Non-blocking; caller re-checks auth afterwards."""
        subprocess.Popen(
            [self.gcloud_path(), "auth", "login", "--launch-browser"],
            creationflags=_NO_WINDOW,
            shell=False,
        )
        log.info("Launched 'gcloud auth login' (browser flow)")

    def describe_environment(self, target: ComposerTarget) -> CommandResult:
        """Cheap connectivity/permission check against the Composer env."""
        return self.run(
            [
                "composer", "environments", "describe", target.environment,
                "--location", target.location,
                "--project", target.project,
                "--format=value(state)",
            ],
            timeout=90,
        )

    # -- DAG operations ---------------------------------------------------

    def _composer_run_args(self, target: ComposerTarget, airflow_cmd: list[str],
                           airflow_args: list[str]) -> list[str]:
        return [
            "composer", "environments", "run", target.environment,
            "--location", target.location,
            "--project", target.project,
            *airflow_cmd,
            "--",
            *airflow_args,
        ]

    def trigger_dag(
        self,
        target: ComposerTarget,
        dag_id: str,
        conf_json: str,
        run_id: str,
        timeout: int = 300,
    ) -> CommandResult:
        args = self._composer_run_args(
            target,
            ["dags", "trigger"],
            [dag_id, "--run-id", run_id]
            + (["--conf", conf_json] if conf_json and conf_json != "{}" else []),
        )
        # Not blindly retryable: a timeout may still have triggered the run.
        # The engine reconciles by polling for the run-id before re-triggering.
        return self.run(args, timeout=timeout, retryable=False)

    def get_run_state(
        self,
        target: ComposerTarget,
        dag_id: str,
        run_id: str,
        timeout: int = 180,
    ) -> tuple[NodeStatus | None, CommandResult]:
        """Poll `dags list-runs` and map the matching run's state.

        Returns (None, result) when the run isn't visible yet (scheduler lag)
        or output could not be parsed — the engine keeps polling.
        """
        args = self._composer_run_args(
            target,
            ["dags", "list-runs"],
            ["-d", dag_id, "-o", "json"],
        )
        result = self.run(args, timeout=timeout)
        if not result.ok:
            return None, result
        runs = extract_json(result.combined_output)
        if not isinstance(runs, list):
            log.warning("Could not parse list-runs output for %s", dag_id)
            return None, result
        for run in runs:
            if str(run.get("run_id", "")) == run_id:
                state = str(run.get("state", "")).strip().lower()
                return _AIRFLOW_STATE_MAP.get(state, NodeStatus.RUNNING), result
        return None, result
