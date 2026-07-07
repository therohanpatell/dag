# ComposerFlow

A production-grade tool for visually designing and orchestrating **existing
Apache Airflow DAGs in Google Cloud Composer 2.2**, using **only the Google
Cloud CLI** (no Airflow UI, no Airflow REST API).

The UI is a **local web app built on the Python standard library only** — no
Streamlit, no web framework, no Node.js. It ships as a single Windows **`.exe`**:
double-click it and it starts a tiny local HTTP server and opens your browser
automatically. The canvas supports **mouse drag-to-connect** (drag from one DAG's
port to another) via a vendored copy of Drawflow. Data lives in an embedded
**SQLite** file; no external database, nothing to install.

Think "Azure Data Factory pipeline canvas", but every activity is one of your
existing Composer DAGs triggered via `gcloud composer environments run`.

## Architecture at a glance

```
run_app.py (.exe entry) ──starts──▶ stdlib HTTP server ──opens──▶ browser
        │                          (composer_flow/webapp/server.py)
        │                                   │ serves
        │                                   ▼
        │                    static/ (index.html, app.js, styles.css,
        │                             vendor/drawflow.min.js)  ← drag-to-connect
        └── composer_flow/            (UI-framework-free backend, shared & tested)
              ├── models/             Workflow, DagNode, Edge, execution records
              ├── core/graph.py       validation, cycles, topological waves, ready-set
              ├── services/
              │     ├── gcloud.py     gcloud CLI wrapper (trigger / poll / auth / retries)
              │     ├── engine.py     execution engine (threads) — emits EngineEvent
              │     └── events.py     thread-safe event queue (no GUI toolkit)
              ├── persistence/        SQLite (WAL) + Repository pattern
              ├── webapp/             HTTP server + appstate + static frontend
              └── theme.py            single teal/green corporate palette
```

The engine is GUI-free: it publishes `EngineEvent` objects onto a thread-safe
queue; a background thread drains them into `appstate.run_state`, and the browser
polls `/api/run-state` (~1.2 s) during a run for live node coloring, console and
progress. The frontend does its own cycle check as you draw connections; the
backend re-validates on save/run. The same backend is exercised directly by the
tests, and has survived three UI rewrites (PySide6 → Streamlit → stdlib web)
unchanged.

---

## 1. Functional requirements

| # | Requirement |
|---|---|
| F1 | Create, edit, delete, save multiple workflows (all managed in the GUI) |
| F2 | Drag-and-drop graph editor: add DAG nodes, connect them with arrows, define dependencies visually |
| F3 | Per-node configuration: DAG ID, optional run name, unlimited JSON key/value parameters converted to a `--conf` JSON payload |
| F4 | Execution order derived automatically from the dependency graph (sequential chains and parallel branches — diamond patterns supported) |
| F5 | Trigger DAGs through `gcloud composer environments run … dags trigger` with a client-generated `--run-id` |
| F6 | Monitor run states (queued / running / success / failed) by polling `dags list-runs` |
| F7 | Fail fast: on any DAG failure, stop launching, mark all downstream DAGs skipped and pending ones cancelled; show failed node in red with the exact CLI command, stdout, stderr and duration |
| F8 | Crash-safe resume: every state transition is persisted; on restart the app offers **Resume / Restart / Discard** for interrupted executions |
| F9 | Workflow validation before run: cycles, missing DAG IDs, self-loops, orphan nodes, dangling/duplicate edges |
| F10 | Startup `gcloud auth list` check; one-click **`gcloud auth login`** launcher with re-check |
| F11 | Execution history dashboard with filters, search and per-node drill-down (command/stdout/stderr) |
| F12 | Rerun only failed DAGs of a past execution |
| F13 | Workflow versioning (snapshot on every save, restore any version) |
| F14 | Export / import workflows as JSON for sharing between environments |
| F15 | Confirmation dialog (with the execution plan) before triggering; ETA from historical run durations; live progress bar; dark & light themes; workflow search |

## 2. Non-functional requirements

- **No external services**: storage is an embedded **SQLite** file
  (`%LOCALAPPDATA%\ComposerFlow\composerflow.db`) — no SQL server, no config
  files to hand-edit.
- **Security**: `subprocess` is always invoked with argument *lists* and
  `shell=False` (no shell injection); no credentials are stored — auth is
  delegated entirely to gcloud's own credential store.
- **Responsiveness**: all gcloud calls run off the UI thread (engine thread +
  bounded worker pool); UI updates via queued Qt signals only.
- **Reliability**: timeouts on every CLI call, bounded retries with backoff on
  transient errors, trigger-reconciliation (see §8), WAL-mode SQLite,
  crash-consistent persist-then-notify ordering.
- **Observability**: rotating logs at `%LOCALAPPDATA%\ComposerFlow\logs`
  containing timestamp, CLI command, DAG, status, duration, error, retry count.
- **Portability**: single-file `.exe` via PyInstaller; per-user data dir means
  the exe can live anywhere.

## 3. System architecture

Layered architecture with strict downward dependencies (UI never touches SQL
or subprocess; services never touch widgets):

```
┌───────────────────────────── UI layer (PySide6) ─────────────────────────────┐
│ MainWindow (controller)                                                      │
│  ├─ WorkflowListPanel   ├─ GraphEditor (QGraphicsScene)  ├─ PropertiesPanel  │
│  ├─ ConsolePanel        ├─ TimelinePanel                 ├─ Dialogs          │
└──────────────▲──────────────────────────────▲────────────────────────────────┘
        Qt signals (queued)             direct calls
┌──────────────┴───────────────┐  ┌───────────┴──────────────────────────────┐
│  Service layer               │  │  Core (pure functions)                   │
│  WorkflowEngine (scheduler)  │─▶│  graph.py: validate, cycles, topo levels,│
│  GcloudClient (CLI wrapper)  │  │  descendants, ready-set                  │
└──────────────▲───────────────┘  └───────────▲──────────────────────────────┘
               │                              │
┌──────────────┴──────────────────────────────┴───────────────────────────────┐
│  Persistence layer (Repository pattern over SQLite/WAL)                     │
│  WorkflowRepository · ExecutionRepository · SettingsRepository · Database   │
└──────────────────────────────────────────────────────────────────────────────┘
│  Models: Workflow, DagNode, Edge, NodeExecution, WorkflowExecution (dataclasses)
│  Utils : logger
```

**Patterns used**: Service layer, Repository pattern, constructor dependency
injection (engine receives `GcloudClient`, `ExecutionRepository`, `EngineConfig`;
`GcloudClient` accepts an injectable `runner` for testing), Observer
(Qt signals), fail-fast scheduler. SOLID: each module has one reason to change;
the CLI wrapper is the single seam to Google Cloud, so swapping to a REST API
later touches exactly one class.

## 4. Component / folder structure

```
DAG/
├── main.py                     # entry point
├── requirements.txt
├── ComposerFlow.spec           # PyInstaller build spec
├── composer_flow/
│   ├── config.py               # paths, defaults, transient-error markers
│   ├── models/                 # framework-free dataclasses
│   │   ├── workflow.py         #   Workflow, DagNode, Edge (+JSON codec)
│   │   └── execution.py        #   NodeStatus, WorkflowStatus, records
│   ├── core/
│   │   └── graph.py            # validation, cycles, topo levels, ready-set
│   ├── services/
│   │   ├── gcloud.py           # CLI wrapper: auth, trigger, poll, retries
│   │   └── engine.py           # workflow execution engine (threads)
│   ├── persistence/
│   │   ├── db.py               # SQLite bootstrap, schema, WAL, migrations
│   │   └── repositories.py     # Workflow/Execution/Settings repositories
│   ├── ui/
│   │   ├── main_window.py      # controller
│   │   ├── graph_editor.py     # node/edge canvas
│   │   ├── panels.py           # list, properties, console, timeline
│   │   ├── dialogs.py          # settings, auth, resume, confirm, history, versions
│   │   └── theme.py            # dark/light QSS + status colors
│   └── utils/logger.py
└── tests/                      # pure unit tests (no gcloud/Qt needed)
```

## 5. Database design (SQLite)

```
workflows(id PK, name, description, created_at, updated_at)
nodes(id PK, workflow_id FK↘cascade, dag_id, run_name, params_json, pos_x, pos_y)
edges(id PK, workflow_id FK↘cascade, source_node_id, target_node_id)
workflow_versions(id PK, workflow_id FK, version, data_json, created_at)   -- last 50 kept
executions(id PK, workflow_id, workflow_name, status, snapshot_json, error,
           started_at, finished_at)
node_executions(id PK, execution_id FK↘cascade, node_id, dag_id, run_name,
                airflow_run_id, status, command, stdout, stderr, error,
                retry_count, started_at, finished_at, duration_seconds)
settings(key PK, value)
```

`executions.snapshot_json` stores the full workflow at run time, so resume and
"rerun failed only" work even if the workflow was edited or deleted afterwards.
`PRAGMA user_version` provides forward schema migration hooks.

## 6. Class diagram (core relationships)

```
MainWindow ──owns──▶ GraphEditor, Panels, Dialogs
MainWindow ──creates per run──▶ WorkflowEngine
WorkflowEngine ──uses──▶ GcloudClient, ExecutionRepository, core.graph
WorkflowEngine ──emits──▶ node_status_changed / log_message / progress /
                          eta_changed / execution_finished  (Qt signals)
GcloudClient ──runs──▶ subprocess (gcloud.cmd), injectable runner for tests
Repositories ──use──▶ Database (context-managed connections)
Workflow 1─* DagNode ; Workflow 1─* Edge ; WorkflowExecution 1─* NodeExecution
```

## 7. Algorithms

**Graph dependency / parallel execution** — event-driven Kahn scheduler
(`core/graph.py` + `services/engine.py`):

```
statuses[n] = PENDING for all n            (SUCCESS for resumed nodes)
loop every 0.5 s:
    ready = { n : PENDING, all predecessors SUCCESS }   ← parallelism emerges here
    submit trigger(n) for each ready n to a pool of max_parallel workers
    every poll_interval s per QUEUED/RUNNING node:
        state = list-runs(dag_id) filtered by our run-id → map to status
    if any FAILED: mark all descendants SKIPPED (BFS), stop launching (fail-fast)
    if user cancel: PENDING → CANCELLED, drain in-flight calls
    exit when all statuses terminal and nothing in flight
```

For the diamond `A → (B,C) → D`: after A succeeds, B and C are both in the
ready set and trigger concurrently; D only becomes ready when *both* report
SUCCESS. No explicit "parallel branches" configuration is ever needed.

**Cycle detection** — iterative 3-color DFS returning the actual cycle path for
the error message; `would_create_cycle()` (BFS reachability) blocks invalid
edges at draw time.

## 8. CLI strategy (compared)

| Task | Options | Chosen & why |
|---|---|---|
| Trigger | `dags trigger` with/without `--run-id` | **With generated `--run-id`** (`cf__<name>__<utc>__<uuid8>`): the only way to deterministically correlate the run afterwards, especially with parallel triggers of the same DAG. |
| Monitor | (a) `dags state <dag> <execution_date>` (b) `dags list-runs -- -d <dag> -o json` (c) task-level polling | **(b)**: (a) needs the exact server-assigned logical date — racy; (c) is overkill. (b) returns `run_id` + `state` as JSON we filter client-side. |
| Auth | `gcloud auth list --filter=status:ACTIVE --format=json` + `gcloud auth login` launcher | Non-interactive check + interactive login in a visible console (browser flow). |

Robustness specifics:
- `gcloud composer environments run` proxies Airflow's CLI through kubectl and
  interleaves noise into stderr — `extract_json()` does string-aware bracket
  balancing over combined stdout+stderr to recover the JSON payload.
- **Trigger reconciliation**: a trigger that times out client-side may still
  have created the run server-side. On trigger failure the engine checks
  whether its run-id exists before declaring failure — this is also why the
  trigger command is *never* blind-retried (idempotency), while read-only
  polls retry freely on transient errors.
- Every call: `shell=False`, arg lists, timeout, `CREATE_NO_WINDOW`.

## 9. State management & resume

Order of operations on every transition: **persist to SQLite first, then emit
the UI signal**. If the process dies at any instant, the DB reflects the last
truth. On startup, executions still marked `running` are surfaced with:

> Previous workflow execution was interrupted. Resume from the last successful
> DAG? **[Resume] [Restart] [Discard]**

Resume rebuilds the engine from `snapshot_json`, seeds previously-successful
nodes as SUCCESS and re-runs the rest. "Rerun failed only" reuses the same
mechanism from the History dashboard.

## 10. UI approach — stdlib web app + Drawflow

| Approach | Verdict |
|---|---|
| **stdlib HTTP server + Drawflow (browser)** ✅ | Zero third-party runtime deps (only Python's `http.server`, `sqlite3`, etc.); real **mouse drag-to-connect** via vendored Drawflow (a small MIT JS file, no CDN, works offline); uses the browser already on the machine; packages to a ~10 MB self-launching `.exe`. |
| Streamlit (previous) | Pure-Python and quick to build, but **cannot host an interactive drag-canvas** — connecting was form/dropdown based, and the bundle was ~165 MB. |
| PySide6 / PyQt6 | Native desktop drag-canvas, but heavier and a Qt/licensing surface. |
| FastAPI + React Flow | Richest canvas, but adds a Node.js build step to every rebuild. |

**Graph editing:** the frontend uses Drawflow — drag DAG nodes on a dotted-grid
canvas, drag from one node's output port to another's input port to create a
dependency, click a node to edit it, press Delete to remove it. A JS cycle check
rejects loop-creating connections as you draw; the backend re-validates on save
and run.

## 11. Run from source

```powershell
py -3.11 -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt      # only pyinstaller + pytest (dev tools)
python run_app.py                    # starts the server and opens the browser
```

First run: click **⚙ Settings** and fill in the environment profiles
(BLD/INT/PRE/PRD) once. If gcloud has no active credential, the top-bar
**Sign in / Switch** button runs `gcloud auth login` in your browser.

## 12. Testing strategy

- **Unit (`pytest tests/`)**: graph algorithms (cycles, waves, descendants,
  ready-set, validation) and the CLI wrapper via an injected fake runner
  (command shape, JSON extraction from noisy output, state mapping, retry
  policy, trigger non-retry).
- **Engine (Qt-free)**: drive the engine with a fake gcloud runner and drain
  its `EngineEvent` queue — verifies diamond parallelism, `--conf` passing,
  fail-fast downstream-skip, and crash-resume.
- **Web layer**: the API is plain JSON over the stdlib server — boot it and
  hit `/api/bootstrap` and `/` to confirm it serves the app and the Drawflow
  canvas loads.
- **Integration (recommended)**: point a profile at a dev Composer env with a
  trivial `sleep-and-succeed` DAG; verify diamond workflow, fail-fast, resume.

## 13. Build the single `.exe`

The repo does **not** contain the built `.exe` (it's git-ignored) — build it
yourself in a few seconds on Windows.

**Prerequisites:** Python 3.11+ on Windows.

**Steps (PowerShell, from the repo root):**

```powershell
# 1. (optional) create and activate a virtual environment
py -3.11 -m venv .venv
.venv\Scripts\Activate.ps1

# 2. install the build tool (PyInstaller). No app runtime deps to install —
#    the app uses only the Python standard library.
pip install pyinstaller

# 3. build the single-file exe from the bundled spec
pyinstaller ComposerFlow.spec

# 4. result:
#    dist\ComposerFlow.exe   (~10 MB, one file)
```

Run it by double-clicking `dist\ComposerFlow.exe` (or `.\dist\ComposerFlow.exe`
from a terminal). It starts a local web server on a free port and opens the app
in your default browser. To quit, close the small console window (or press
Ctrl+C in it).

**Clean rebuild** (if a previous build misbehaves):

```powershell
Remove-Item -Recurse -Force build, dist -ErrorAction SilentlyContinue
pyinstaller ComposerFlow.spec
```

**How it works:** `run_app.py` is the frozen entry point — it picks a free
local port, starts the stdlib HTTP server, and opens the browser once the port
responds. `ComposerFlow.spec` bundles `composer_flow/webapp/static`
(HTML/CSS/JS + the vendored Drawflow node editor) as data files and excludes
Qt/Streamlit/pandas entirely, so the onefile build stays ~10 MB.

**Notes:** `onefile` + `console=True` — a small console window hosts the server
and shows the URL; closing it quits the app. UPX disabled (avoids antivirus
false positives); logs and the SQLite database live under
`%LOCALAPPDATA%\ComposerFlow`, so the exe itself stays read-only and can be
placed anywhere. For corporate distribution, sign the exe (`signtool`) to avoid
SmartScreen warnings. gcloud is *not* bundled — target machines already have
the Cloud SDK.

## 14. Future enhancements (design already accommodates them)

- Airflow REST API backend: implement a second client with `trigger/poll`
  methods and swap it behind the engine (one-class change).
- DAG-ID autocomplete via `dags list` (add a method to `GcloudClient`).
- Per-node retry policies, timeouts and trigger rules (e.g. "run on failure").
- Scheduled workflow runs (Windows Task Scheduler + CLI mode entry point).
- Multi-environment profiles (dev/uat/prd) — extra columns in `settings`.
- Notifications (toast/email/Teams webhook) on completion or failure.
- Gantt-style visual timeline; run-comparison view.

## 15. Recommendations & best practices

- Grant the runtime account only `composer.user` on production if the app is
  used purely for triggering; `environmentAndStorageObjectAdmin` is broader
  than triggering requires.
- Keep `--conf` values environment-agnostic and use export/import to promote
  workflows between environments (values like `environment: PRD` are visible
  in the confirm dialog on purpose).
- Set the poll interval ≥ 15 s in production: each poll spawns a kubectl exec
  in the Composer GKE cluster — polite polling avoids pressure on the
  environment.
- Watch the first `list-runs` after trigger: scheduler lag of 10–60 s before
  the run becomes visible is normal; the engine tolerates it by design.
