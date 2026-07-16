# Gang of 8 Architecture

Gang of 8 is a local-first coordinator for multi-agent work. Agents provide text
only; the Python coordinator owns session state, side effects, approvals, and
delivery into user folders.

## Request Flow

1. A task enters through the dashboard (`POST /tasks`) or `cli.py submit`.
2. `GangOf8Service` creates a session, applies current settings, binds the
   active workspace, folds in attachments, and selects the backend.
3. `loop.run_session` classifies the task, builds the council, runs panel
   rounds, resolves skill requests, captures artifacts, verifies outputs, and
   composes the final answer.
4. The dashboard polls `GET /sessions/{id}` and `GET /sessions/{id}/timeline`
   for state, contributions, actions, approvals, files, and health.

## Session Lifecycle

Session state is explicit in `SessionStatus`:

- `received` -> task accepted.
- `classified` -> rule-based task classification and budgets are set.
- `deliberating` -> panel, lead, specialists, skills, artifacts, tests.
- `awaiting_input` -> the system or an agent needs a human answer.
- `awaiting_approval` -> a proposed action needs human approval.
- `composing` -> final answer is being synthesized.
- `done`, `failed`, `cancelled` -> terminal states.

Running sessions are bounded by agent-call and wall-clock budgets. Cancellation is
cooperative at agent-call boundaries and can kill registered CLI subprocesses.

## Agent Backends

All backends implement the same registry interface:

- `MockAdapter`: deterministic offline behavior for tests.
- `CliAdapter`: invokes local `claude`, `codex`, and `gemini` CLIs from a neutral
  working directory with no native tools.
- `OpenRouterAdapter`: optional API-backed seats configured in Settings.

Model selection precedence is role pin, then seat pin, then backend default.

## Governance Boundary

Agents never write files, run commands, fetch URLs, or open local paths directly.
They emit plain-text contracts such as `SKILL:`, `ARTIFACT:`, `EDIT:`,
`RUNTESTS:`, and `PROMOTE:`.

`skills.py` is the permission catalogue. `executor.py` performs the actual side
effect after governance authorizes it.

Spaces:

- `sandbox`: per-session scratch area outside the project.
- `workspace`: optional user-selected council work area.
- `established`: real source/delivery folder, read-only except through promote.

The one hard user-file boundary is `promote`: copying council output into the
delivery folder requires an approval with a diff preview.

## Artifact Pipeline

For output-producing tasks, panel seats can author candidates independently.
The coordinator captures namespaced candidate files, smoke-tests runnable web
artifacts, asks judges to score candidates blindly, and lets the codifier ratify
or repair the winner. With Council integration review enabled, the codifier may
also produce a complete merged candidate when it finds concrete complementary
strengths across the scored outputs. The merge is runtime-validated and shown to
the human beside the voted winner; only an explicit "Use integration" decision
replaces the default winner before write/promote actions are executed.

`gangof8/artifacts.py` owns marker parsing and cleanup of model-emitted file
bodies. `gangof8/smoke.py` owns the fast headless runtime check for web files.
`gangof8/browser_acceptance.py` owns the fail-closed real-browser release gate
for interactive HTML. Final-batch approval carries the verified staging hashes;
rollback-protected promotion rechecks source, prepared, and destination bytes.

## Persistence

Runtime state lives under `data/`, which is gitignored:

- `gangof8.db`: SQLite session table with serialized session JSON.
- `sessions/*.jsonl`: per-session event trails.
- `settings.json`: editable app settings, stamped with `settings_version`.
- `secrets.json`: local API keys, overridden by environment variables.
- `workspaces.json`: registered workspaces and active workspace id.
- `uploads/`: uploaded blobs and extracted text sidecars.

Session records include `schema_version`. Migration hooks live in
`settings.py` and `sessions.py`.

## Operations and Safety

`runtime_diagnostics.py` produces a redacted diagnostics payload for
`GET /diagnostics`: writable paths, active workspace, registered/present seats,
timeouts, API-key presence, web capability, and remote-access mode. It never
returns stored key material.

The FastAPI service is localhost-only by default. `cli.py serve` rejects a
non-loopback bind host unless the operator supplies `--allow-remote` and sets
`GANGOF8_ALLOW_REMOTE=1`. Local-machine actions that reveal a key or ask the OS
to open a file remain restricted to local requests even in remote-enabled mode.

`reporting.run_summary()` turns persisted session data into a compact audit
view: call counts, accumulated model time, contribution counts by agent/model,
action statuses, repair attempts, and SHA-256 file fingerprints.

## Frontend

The dashboard is served by FastAPI from `gangof8/static/`:

- `index.html`: markup shell.
- `app.css`: dashboard styling.
- `dashboard-utils.js`: shared escaping, diff formatting, model labels, and API helper.
- `app.js`: polling, rendering, settings, approvals, uploads, and workspace UI.
