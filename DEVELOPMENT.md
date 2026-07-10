# Development

Gang of 8 targets Python 3.12 or later. Create an isolated environment and
install the project with its development tools:

```powershell
python -m venv .venv
.venv\Scripts\python -m pip install --upgrade pip
.venv\Scripts\python -m pip install -e ".[dev]"
```

Run the local verification commands before committing:

```powershell
.venv\Scripts\python -m ruff check gangof8 tests
.venv\Scripts\python -m pytest tests -q
node --check gangof8\static\dashboard-utils.js
node --check gangof8\static\app.js
```

GitHub Actions runs the Ruff correctness checks and the full test suite for
pull requests and changes to `main`.

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
