# Development

Gang of 8 targets Python 3.12 or later and runs on Windows, macOS, and Linux.
Create an isolated environment and install the project with its development
tools:

**Windows (PowerShell)**

```powershell
python -m venv .venv
.venv\Scripts\python -m pip install --upgrade pip
.venv\Scripts\python -m pip install -e ".[dev]"
```

**macOS / Linux**

```bash
python3.12 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -e ".[dev]"
```

Run the local verification commands before committing:

**Windows (PowerShell)**

```powershell
.venv\Scripts\python -m ruff check gangof8 tests
.venv\Scripts\python -m pytest tests -q
node --check gangof8\static\dashboard-utils.js
node --check gangof8\static\app.js
```

**macOS / Linux**

```bash
.venv/bin/python -m ruff check gangof8 tests
.venv/bin/python -m pytest tests -q
node --check gangof8/static/dashboard-utils.js
node --check gangof8/static/app.js
```

GitHub Actions runs the Ruff correctness checks and the full test suite for
pull requests and changes to `main`.

## Running the dashboard

Double-click launchers are provided for all three platforms (all default to
`--backend cli`; edit the `BACKEND` variable in the script to switch to
`mock` for free/offline use):

- **Windows**: `Launch Gang of 8.bat` (visible console) or
  `Launch Gang of 8 (no window).vbs` (hidden). Stop with `Stop Gang of 8.bat`.
- **macOS**: `Launch Gang of 8.command` (Terminal window) or
  `Launch Gang of 8 (no window).command` (detached). Stop with
  `Stop Gang of 8.command`.
- **Linux**: `./launch-gangof8.sh` (foreground) or
  `./launch-gangof8-background.sh` (detached). Stop with `./stop-gangof8.sh`.
  Run these from a terminal — most file managers don't execute `.sh` files on
  double-click by default.

All variants bind to `127.0.0.1:8790` and only one instance can hold that
port at a time; the background/no-window variants free the port from any
previous instance before starting.

### Cross-platform path handling

Backslash (`\`) is a path separator only on Windows — on macOS/Linux it's an
ordinary filename character. A pasted Windows-style path (or a typo like
`/Users/me\project`) will silently resolve to a bogus single directory named
literally `me\project` instead of failing loudly, unless normalized first.
`WorkspaceStore.add()` (`gangof8/workspaces.py`) normalizes stray backslashes
to `/` on non-Windows platforms before resolving; keep this in mind if you add
another entry point that accepts a raw filesystem path from user input.

## Reliability follow-up: bound failed model calls

An implementation-owner call that runs for many minutes and then returns a
refusal or no required artifacts is a failed call, even when its subprocess
exits successfully. Do not report that outcome only as `agent_call_finished`
or silently allow an unlimited retry.

Mitigation/fix requirements:

- Replace `timeout_s: 0` for coding calls with a configurable, finite hard
  deadline. The default should stop a single call well before 19 minutes
  (target: at most 10 minutes), while preserving immediate user cancellation.
- Where the backend exposes streaming activity, enforce a separate no-output
  stall deadline and persist meaningful progress timestamps. Do not treat a
  buffered CLI's permanent `progress_chars: 0` as proof of healthy progress.
- Separate transport completion from semantic success. If a package requires
  artifacts and the response is a refusal, malformed artifact contract, or
  contains none of the required files, record a failed attempt with an explicit
  reason such as `missing_required_artifacts`.
- Keep recovery bounded. A targeted retry may be attempted, but repeated
  timeout/refusal/no-artifact results must fail or pause the package instead of
  consuming unbounded wall time.
- Expose elapsed time, deadline, last real progress, attempt number, and failure
  reason through the session/timeline read APIs and dashboard.

Regression coverage should include a slow adapter that exceeds the hard
deadline, a successful subprocess that returns only a refusal, a response that
omits required artifact markers, and a bounded retry that reaches a clear
terminal package state.

## Module boundaries

- `service.py` wires storage, configuration, adapter registration, background
  work, and session APIs.
- `loop.py` owns bounded panel rounds, delegation, artifact delivery, and the
  repair loop. Parsing model-authored artifact contracts is isolated in
  `artifacts.py`.
- `runtime_diagnostics.py` derives redacted setup information without coupling
  diagnostics to the service implementation.
- `reporting.py` produces user-facing timeline, council-health, and run-summary
  views from persisted sessions.
- `static/dashboard-utils.js` provides shared escaping, diff rendering, model
  labels, and JSON request helpers. `static/app.js` owns screen behavior.

## Local-only service

The service is intentionally local by default. It can browse local folders,
open delivered files with the host OS, and reveal locally stored keys. Binding
to any non-loopback host requires both `--allow-remote` and
`GANGOF8_ALLOW_REMOTE=1`; use that only behind an authenticated reverse proxy.
Even in remote-enabled mode, key reveal and OS file-open operations require a
local request.
