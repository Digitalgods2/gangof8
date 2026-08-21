# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Working style in this repo

`AGENTS.md` sets the development mode and takes precedence: implement the requested product
behavior, do **not** add new tests unless the user asks or the change fixes a confirmed
regression, and after implementing run only the smallest existing check needed to catch obvious
breakage. Pre-existing unrelated failures do not block the requested work.

## Commands

The project targets Python 3.12+ and is installed in-place in `.venv`. Windows paths shown;
on macOS/Linux use `.venv/bin/python`.

```powershell
# setup
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"

# full local verification (mirrors .github/workflows/ci.yml + the JS checks)
.\.venv\Scripts\python.exe -m ruff check gangof8 tests
.\.venv\Scripts\python.exe -m pytest tests -q          # ~765 tests
node --check gangof8\static\dashboard-utils.js
node --check gangof8\static\app.js

# one file / one test
.\.venv\Scripts\python.exe -m pytest tests\test_loop_mock.py -q
.\.venv\Scripts\python.exe -m pytest tests\test_goals.py::test_name -q

# run the app (dashboard at http://127.0.0.1:8790/, OpenAPI at /docs)
.\.venv\Scripts\python.exe cli.py serve --backend mock   # offline/deterministic
.\.venv\Scripts\python.exe cli.py serve --backend cli    # real local claude/codex/gemini CLIs
```

CI runs only `ruff check gangof8 tests` and `pytest tests -q`. Ruff is configured with
`select = ["F"]` (correctness only) — it is not a formatter here, so do not reformat code to
satisfy style rules that aren't enabled.

Double-click launchers (`Launch Gang of 8.bat` / `.command`, `launch-gangof8.sh`) all bind
`127.0.0.1:8790`; only one instance can hold the port. Backend is set by the `BACKEND` variable
inside each script.

Session inspection helpers live in `scripts/` (`inspect_session.py`, `show_contribution.py`).

## What this is

A local, single-user coordinator that runs several LLMs as a governed council. Models produce
**text only**; the Python coordinator owns all state, side effects, approvals, and file delivery.
Two collaboration modes with deliberately different logic:

- **Ordinary task** — every enabled seat produces an independent take/candidate, candidates are
  smoke-tested and blindly judged, a winner is selected, delivery goes through a `promote` approval.
- **Build-team goal** (`/goal …`, or auto-routed from a substantial build brief) — an architect
  produces an owned package graph, one model atomically authors each package into shared goal
  staging, packages are verified, then the whole manifest ships through a single `promote_batch`
  approval.

## Architecture

Read `ARCHITECTURE.md` for the full narrative and `ARCHITECTURE-REVIEW.md` §4 for the current
design principles (right-size the roster; frontier seats author/verify; deterministic gates are
the referee; repair at the point of detection; escalate rather than retry the same seat).

Layering, roughly outer to inner:

- `cli.py` — argparse entry point (`submit`, `serve`, `list`, `status`, `log`, `pending`,
  `approve`/`deny`, `inputs`, `answer`/`decline`, `workspace …`). Registered as the `gangof8` script.
- `gangof8/main.py` — FastAPI app + dashboard routes; a `localhost_only` middleware and
  `_require_sensitive_local` guard key-reveal and OS-file-open endpoints even in remote mode.
- `gangof8/service.py` (~7k lines) — `GangOf8Service`: settings application, seat enable/disable
  and role redistribution, adapter registration, model pin resolution, background worker pool,
  worker leases and restart recovery, goals, workspaces, diagnostics.
- `gangof8/loop.py` (~6k lines) — `run_session`: classification, council assembly, bounded panel
  rounds, delegation fan-out, skill-request resolution, artifact capture, best-of-N judging,
  repair, composition. Pure text parsing is factored out into `artifacts.py`.
- `gangof8/rounds.py`, `composer.py`, `assembly.py`, `goals.py`, `workbench.py` — round prompts,
  final synthesis, deterministic HTML assembly, goal planning/packages, and outcome
  contracts/playbooks/evaluations/steering/artifact manifests.
- `gangof8/registry.py` + `gangof8/adapters/` — one `Adapter` protocol behind `AgentRegistry`:
  `MockAdapter` (deterministic, used by tests), `CliAdapter` (local `claude`/`codex`/`gemini`
  subprocesses from a neutral cwd with no native tools), `OpenRouterAdapter` (API seats).
  Every call's outcome feeds `seat_health.py` so scheduling routes around dead seats.
- `gangof8/logstore.py` — SQLite `data/gangof8.db` (serialized session JSON) plus per-session
  `data/sessions/*.jsonl` event trails.
- `gangof8/static/` — `index.html`, `app.css`, `dashboard-utils.js` (shared escaping, diff
  rendering, model labels, JSON fetch helper), `app.js` (all screen behavior; ~180KB).

### The governance boundary — the invariant that shapes everything

Agents never write files, run commands, fetch URLs, or open paths. They emit plain-text contracts
(`ARTIFACT:`/`END_ARTIFACT`, `EDIT:` with OLD/NEW blocks, `RUNTESTS:`, `PROMOTE:`, `SKILL:`)
which are parsed in `artifacts.py`, authorized in `governance.py` against the permission
catalogue in `skills.py`, and only then executed in `executor.py`. `governance.py` is
default-deny: `generate_text` is the only capability that never needs approval, and
`authorize_action` re-checks the skill's `allowed_roles` and permitted spaces on every dispatch,
including when resuming older persisted sessions.

File spaces are distinct and enforced by `paths.py`: per-session **sandbox** (neutral location,
never inside the repo), optional active **workspace**, per-goal **staging**, and the user's
**established/delivery** folder which is read-only except through an approved `promote` /
`promote_batch` with a diff preview and hash rechecks.

### Verification stack

`smoke.py` (fast headless Node check for runnable web files) → `browser_acceptance.py`
(fail-closed real-browser gate for interactive HTML, via Playwright) → independent frontier
release inspection (best-of-N only — its candidates come from panelists, so a single-author run
never reaches it). Deterministic gates decide; models only author and repair. For file-producing
tasks, prose is never accepted as proof — success requires an executed write/edit plus artifact
verification, otherwise the run ends `failed_verification`. A binary deliverable (`.pdf`,
`.docx`, …) counts only when an executed `BUILD` produced it: seats emit text, so a hand-typed
file merely *named* `.pdf` is prose wearing an extension.

On top of that, every route has a mandatory independent check (`_review_deliverable`): one
enabled seat that did not author the result reads it before delivery — the file for build tasks,
the composed answer for answer-only ones. A FAIL is confirmed by a second, different seat, and
only a confirmed FAIL refuses delivery (`GANGOF8_REVIEW_CONFIRM`, `GANGOF8_REVIEW_BLOCKS`).

### Session lifecycle

`SessionStatus` in `models.py`: `received → classified → deliberating → (awaiting_input |
awaiting_approval) → composing → done | failed | cancelled`. Runs are bounded by agent-call and
wall-clock budgets; cancellation (`cancellation.py`) is cooperative at call boundaries and can
kill registered CLI subprocesses. Sessions and settings carry `schema_version` /
`settings_version` with migration hooks in `sessions.py` and `settings.py`.

## Configuration

Runtime state lives in gitignored `data/` (`gangof8.db`, `sessions/`, `settings.json`,
`secrets.json`, `workspaces.json`, `uploads/`). Tunables are `GANGOF8_*` env vars read at import
time in `gangof8/config.py` — most notably `GANGOF8_BACKEND` (`mock`|`cli`, default `mock`),
`GANGOF8_DATA`, `GANGOF8_SANDBOX`, `GANGOF8_PANEL_MODE` (`duo` default, `council` for the full
panel), `GANGOF8_GOAL_FULL_ROSTER`, the various `*_TIMEOUT` deadlines (`0` = disabled), and
`GANGOF8_ALLOW_REMOTE`. `gangof8/default-settings.json` is the packaged profile used on a fresh
install; the Settings UI/`/settings` API is the normal way to change seats, model pins, roles,
and budgets.

Non-loopback binding requires **both** `--allow-remote` and `GANGOF8_ALLOW_REMOTE=1`
(`security.validate_bind_host`); this is deliberate — the service can browse local folders,
open files with the host OS, and reveal stored keys.

## Testing notes

`tests/conftest.py` has an autouse fixture that repoints `config.SANDBOX_ROOT` at a fresh temp
dir per test and forces `config.WEB_ENABLED = False`, so tests never touch shared scratch state
or make real network calls — tests that need web access re-enable it explicitly. Because config
values are module-level constants read at import, tests override them with
`monkeypatch.setattr(config, …)` rather than environment variables. `MockAdapter` is how the
loop is exercised end-to-end offline. pytest uses its own default basetemp, which rotates per
run and self-cleans; do not pin `--basetemp` at a fixed path inside the repo, because a single
damaged ACL there makes every later run fail at setup with `WinError 5` and no way back without
an elevated `takeown`.

## Cross-platform gotcha

Backslash is a path separator only on Windows. `WorkspaceStore.add()` (`workspaces.py`)
normalizes stray backslashes to `/` on non-Windows before resolving, so a pasted Windows path
fails loudly instead of creating a directory literally named `me\project`. Any new entry point
that accepts a raw filesystem path from user input needs the same normalization.
