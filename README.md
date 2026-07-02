# Conclave OS — Type 1: Coordinator OS

A coordination layer that receives a user task, convenes a **panel of
different AI models** that deliberate in parallel rounds under a single lead,
runs the whole thing **bounded** and under **human authority**, logs
everything, and returns a structured final answer. See
[How a deliberation works](#how-a-deliberation-works-the-panel-model) below;
full design in [DESIGN.md](DESIGN.md).

Conclave OS is **fully self-contained**: it runs the local agent CLIs itself.
Two backends sit behind one adapter interface — `mock` (offline, deterministic,
default) and `cli` (the real backend: Conclave OS invokes the local `claude` /
`codex` / `gemini` CLIs directly, in plain non-interactive generation mode,
e.g. `claude -p --output-format json --tools ""`). Tools are disabled /
read-only in those calls, so agents perform **no** side effects themselves —
Conclave OS governance remains the only path to side effects.

```powershell
# real agents — nothing else to start; the CLIs manage their own auth
.venv\Scripts\python cli.py serve --backend cli
# or one-shot: .venv\Scripts\python cli.py submit "your task" --backend cli
# or: $env:CONCLAVE_OS_BACKEND = "cli"
```

Default role mapping (edit `conclave_os/config.py` → `ROLE_AGENTS_CLI`):
lead→claude, researcher→gemini, critic→codex, summarizer→claude. Any role is
remappable in settings. The **panel** roster is derived automatically: the
installed CLI agents plus every OpenRouter seat you enable in Settings
(DeepSeek, GLM, Qwen, Kimi — needs an API key).

It ships a **web dashboard** with a chat composer, governs real-code delivery
through the approval-gated **promote** skill, can operate on a real project
directory (**workspaces**), and **reads attached images** (text, diagrams,
screenshots) — all detailed below.

## How a deliberation works (the panel model)

When a session starts, the council roster shows one chip per seat — and each
seat is a **different origin model**. Here is what each is doing:

- **The panelists** (orange chips) are the heart of the design. Every round,
  ALL of them — the local claude/codex/gemini CLIs plus every enabled
  OpenRouter seat — get the task (plus the pre-read project context) and write
  their take **independently and in parallel**. None of them sees the others'
  answers while writing, deliberately: you get N genuinely different
  perspectives instead of N models agreeing with whoever spoke first. That is
  the diversity-of-intelligence bet the whole system is built on.
- **The lead** (yellow chip) is the synthesizer and decision-maker. After the
  panel fan-out finishes, it gets all the takes and does the real work: weighs
  them, adopts what's right, pushes back on what's weak, pulls in specialist
  talents mid-round when it needs one (`CONSULT:` / `DELEGATE:`), writes any
  files, and ends with `ROUND: DONE` or `ROUND: CONTINUE`.
- **Why the lead's model also appears as a panelist:** same model, two
  different jobs. As a panelist it is one independent voice among many; as
  the lead it is the judge, not a witness — a separate call with a separate
  charter, so the synthesis weighs its own panel take like any other input.
  (Remap the lead in Settings if you'd rather have a different model
  arbitrate.)
- **The summarizer** (purple chip) composes the final answer card for
  question/research tasks. Build tasks usually skip it entirely — they get a
  fast, deterministic file-manifest summary instead.
- **The specialist talents** (critic, red_team, fact_validator, …) exist but
  sit inactive off the roster; one lights up only when the lead actually pulls
  it in.

**Rotation is automatic but consent-gated.** Rounds proceed on their own; if
the lead declares CONTINUE three times (`ROUNDS_PER_CONSENT`, settable), the
run pauses and asks you: *continue another block, a specific number of rounds,
or compose the final answer from the work so far?* Budgets (agent calls, wall
time) remain the hard backstop underneath. A round costs `len(panel) + 1`
model calls (every seat + the lead synthesis) — which is exactly why the
consent gate exists.

**One hard gate.** Work happens freely in the council's own sandbox/workspace
(writes, edits, test runs, staging, web lookups — no approvals). The single
approval in the whole pipeline is **promote**: copying a finished file into
your real folder, with a diff on the approval card. A promote with no known
destination asks you *where* at delivery time — never up front, never assumed.
On any approval you can pick **Approve all** to grant that category for the
whole session — one deliberate decision instead of N identical clicks.

**Builds close their own loop.** When a build's `RUNTESTS:` command fails, the
lead is shown the real failure output and repairs the code (surgical `EDIT:`
blocks or file re-writes), and the tests re-run — up to 3 attempts, all
*before* anything is promoted. A build ships passing its own tests, or the
final answer says exactly why it couldn't.

## Setup

```powershell
python -m venv .venv
.venv\Scripts\python -m pip install pydantic fastapi uvicorn pytest httpx pypdf google-genai Pillow
```

- `pypdf` — extract text from attached PDFs.
- `google-genai` — gemini image vision (inline-image inference; needs `GEMINI_API_KEY`).
- `Pillow` — used only by the test suite to generate images.

The `cli` backend uses your locally-installed agent CLIs (`claude`, `codex`,
`gemini` on PATH), each with its own auth — there is no external service.

### How Conclave OS detects your CLIs

There is no configuration file listing which AIs you have — detection is the
same PATH lookup your terminal does when you type `claude` and press Enter:
`shutil.which("claude")` (and likewise for `codex` and `gemini`). Anything
installed and on PATH is found; anything absent is simply skipped. That one
mechanism drives three behaviors:

- **Settings → seats** shows each CLI with a live "ready ✓" / unavailable
  badge — that's the `which` check, re-run on every load.
- **The panel roster degrades gracefully**: only installed CLIs are convened,
  so uninstalling one shrinks the panel instead of breaking runs (OpenRouter
  seats join only when enabled *and* an API key is present).
- **Windows gotcha, already handled**: npm installs `codex` and `gemini` as
  `.cmd`/`.ps1` shims, not `.exe` files, and spawning them by bare name fails
  with `WinError 2`. Conclave OS resolves the shim's real path via
  `shutil.which` before launching — if you ever add another npm-installed CLI,
  that's the pattern to copy.

To see for yourself what would be detected, run the same probe by hand:

```powershell
Get-Command claude, codex, gemini -ErrorAction SilentlyContinue
```

Each CLI manages its own login (`claude`, `codex`, and `gemini` all have their
own auth flows) — Conclave OS never sees or stores those credentials. Which
underlying *model* each CLI runs is a separate question: pick it per seat in
Settings → Local CLI models, or leave it empty to inherit that CLI's own
default (every contribution displays the model that actually produced it).

### Which API keys do you need?

Short answer: **none, to start.** The CLIs authenticate themselves, and the
Settings model dropdowns are fed by a **public, no-key model catalog**
(refreshed live, newest models first) — so the dynamic model list works out of
the box. Keys only *add* capability, and both can be pasted in
**Settings → API keys** (stored locally in gitignored `data/secrets.json`;
an env var of the same name always wins) — no environment variables required:

| Key | Required? | What it unlocks |
|-----|-----------|-----------------|
| *(none)* | — | Full core app: CLI seats, deliberation, builds, promote gate, model dropdowns via the public catalog |
| **Gemini** (`GEMINI_API_KEY` / Settings) | Optional | The gemini seat runs through Google's SDK instead of its flaky headless CLI (faster, reliable on Windows); the gemini dropdown switches to **Google's own authoritative model list**; gemini image vision; `web_search` with Google Search grounding. Free at aistudio.google.com |
| **OpenRouter** (`OPENROUTER_API_KEY` / Settings) | Optional | The pay-per-token OpenRouter panel seats (DeepSeek, GLM, Qwen, Kimi, …) |

If a key is absent, the related features degrade gracefully rather than error:
no Gemini key ⇒ gemini uses its CLI and the dropdown uses the public catalog;
no OpenRouter key ⇒ the panel is simply CLI-only.

## Use

```powershell
# CLI
.venv\Scripts\python cli.py submit "Compare SQLite vs. plain JSON files for storing session logs in a local service, and recommend one."
.venv\Scripts\python cli.py list
.venv\Scripts\python cli.py status <session_id>
.venv\Scripts\python cli.py log <session_id>          # JSONL reasoning trail
.venv\Scripts\python cli.py pending                   # approvals waiting on you
.venv\Scripts\python cli.py approve <session_id> <approval_id>   # resumes the session
.venv\Scripts\python cli.py deny <session_id> <approval_id>      # cancels the session
.venv\Scripts\python cli.py inputs                    # questions agents asked you
.venv\Scripts\python cli.py answer <session_id> <input_id> your answer text
.venv\Scripts\python cli.py decline <session_id> <input_id>      # cancels the session

# Workspaces (allowed work areas — see below)
.venv\Scripts\python cli.py workspace list
.venv\Scripts\python cli.py workspace add <name> <absolute_path>   # registers + activates
.venv\Scripts\python cli.py workspace use <workspace_id>
.venv\Scripts\python cli.py workspace none                         # back to the sandbox

# API
.venv\Scripts\uvicorn conclave_os.main:app --port 8790
# POST /tasks {"text": "...", "attachments": ["up_..."]} · GET /sessions · GET /sessions/{id}
# POST /uploads {"name": "...", "content_base64": "..."}  → {id, kind, ...}
# GET /approvals · POST /sessions/{id}/approvals/{aid} {"approved": true|false}
# GET /inputs · POST /sessions/{id}/inputs/{iid} {"answer": "..."} or {"decline": true}
# GET/PUT /settings · GET /settings/seats · GET/POST /workspaces · PUT /workspaces/active
```

## Web dashboard

`python cli.py serve --backend cli` (or `--backend mock`) opens a single-page
dashboard at `http://127.0.0.1:8790/`:

- **Bottom-center chat composer** — auto-growing text box (Enter sends,
  Shift+Enter for a newline), a **Clear** button, and a **+** menu to attach a
  **document (text / PDF)** or **image**. Attachments show as removable chips.
- **Live progress** — each session shows a pulsing status banner (current round
  goal, the seat being awaited, a ticking elapsed timer) and a council roster
  that green-checks each agent as it contributes.
- **Rollups** — the final answer plus a one-line stats summary; contributions
  and disagreements collapse to informative one-line summaries (expandable),
  with open/closed state preserved across the 3 s refresh.
- **Approvals & questions** — file-write gates surface **Approve / Deny**
  buttons (naming the exact target path); agent questions surface an
  **Answer / Decline** box.
- **Settings gear** — backend, per-role agent mapping, workspaces (add /
  activate / use-sandbox), governance / composer tunables, and UI prefs, each
  with a hover tooltip explaining its purpose.

## Test

```powershell
.venv\Scripts\python -m pytest tests -q
```

## Skills & permission kernel

Side effects are governed by a data-driven **skill registry** (`skills.py`) and
a **permission kernel** (`governance.authorize_action`). Each `Skill` declares
its category, risk, `requires_approval`, allowed roles, and inputs; the kernel
role-gates every action and decides on that metadata — never on hardcoded
behaviour.

| Skill | Approval | What it does |
|-------|----------|--------------|
| `write_file` / `edit_file` | none | Write/edit files in the council's own sandbox or workspace |
| `run_tests` | none | Run a test command inside the council's spaces (time/output-bounded) |
| `read_file` / `search_project` / `list_dir` | none (read) | Read, grep, and list — sandbox, workspace, or the established folder |
| `web_search` / `web_fetch` | none (read) | Governed live-web lookups via the coordinator |
| `stage` | none | Move a file up from sandbox into the permanent workspace |
| `promote` | **required** (human, with diff) | **The ONE gate** — copy a council file into your real (established) folder |

- **Producing files**: the lead heads a block with `ARTIFACT: <filename>`
  (one per file) followed by the full contents; these write freely into the
  sandbox. A `PROMOTE: <filename>` line is what proposes real delivery — that
  is where the approval (and, if unset, the "where should this go?" question)
  happens. If an output task names files but emits no full blocks, the
  coordinator **materializes** each with a focused single-file call.
- **Reading mid-deliberation**: any allowed role may emit a plain-text
  `SKILL: read_file <path>` or `SKILL: search_project <query>` line; the kernel
  authorizes it (reads need no approval) and the result is fed back on a single
  re-call. The grammar is advertised to a role only when it's useful.
- **No agent does I/O itself** — every write flows through the executor +
  human approval; the only ungated capability is `generate_text`.

## Workspaces (allowed work areas)

By default file skills are confined to a throwaway per-session sandbox
(`data/artifacts/<session_id>/`). Register a **workspace** to let the council
read and (with approval) write into a real project directory instead:

- `cli.py workspace add <name> <path>` (or the dashboard Workspace panel)
  registers + activates it; new sessions capture the active workspace.
- Paths resolve **inside** the root — subdirectories like `src/main.py` are
  allowed, but `..`, absolute, and drive-qualified paths are rejected
  (`executor.resolve_in_workspace`). Approvals name the absolute target.

## Multi-modal input & vision

Attach **text**, **PDF**, or **image** files in the dashboard composer (stored
under `data/uploads/`):

- **Text / PDF** — text is extracted (PDF via `pypdf`) and folded into the task
  the council reads.
- **Images** — **really seen** by the agents (reads text in screenshots/scans,
  interprets diagrams), with no tools or filesystem access:
  - **claude** — base64 image content blocks via `--input-format stream-json`.
  - **codex** — `codex exec --image=<path>`.
  - **gemini** — google-genai SDK inline image (`Part.from_bytes`), used when an
    image is attached and `GEMINI_API_KEY` is set; gemini text calls stay on the
    CLI. (The gemini CLI has no clean headless image input — see
    [gemini-cli#3311](https://github.com/google-gemini/gemini-cli/issues/3311).)

## Guarantees (enforced by tests)

- Every loop is bounded — sessions always terminate; budget exhaustion yields
  a partial low-confidence answer, never a spin (`test_budgets.py`); a lead
  that declares CONTINUE past the consent block pauses for your go-ahead
  (`test_rounds.py`).
- One hard gate — work in the council's own spaces is free; nothing reaches
  your real folder without an explicit, diff-carrying `promote` approval
  (`test_actions.py`, `test_established.py`). Risk classification is
  informational and never blocks a run (`test_governance.py`).
- Full reasoning trail — every classification, council choice, round,
  panel contribution, synthesis decision, and approval is in
  `data/sessions/<id>.jsonl`, with session state in `data/conclave_os.db`
  (`test_loop_mock.py`).

## Validated against real agents

The full pipeline has been proven with real agents, not just mocks:

- **Deliberation run** (`s_20260613_142ffb8d`): gemini researched, codex raised
  disagreements that were critic-tested and ruled on evidence (one correctly
  narrowed an overclaim), gemini reconciled, claude composed — ~3.5 min,
  6 agent calls, 3 bounded rounds.
- **Artifact run** (`s_20260613_689ece67`): three agent-question pause/resume
  cycles (gemini and claude both asked clarifying questions answered via
  `cli.py answer`), then claude proposed `ARTIFACT: README.md`, the
  session paused on a `file_write` approval, and the file was written into the
  session sandbox only after explicit `cli.py approve`.

Proven on the self-contained `cli` backend (2026-06-13):

- **Build into a real workspace** — a "build a tiny FastAPI app (main.py,
  README.md, requirements.txt, test_main.py)" task: classified as code, the
  implementer drafted, each file paused on a `file_write` gate naming the
  workspace path, and on approval **real, runnable code** was written into the
  workspace — the generated `test_main.py` passes under pytest.
- **Image vision, all three seats** — an attached PNG whose text existed only as
  pixels was read correctly end to end by claude, by codex, and (with the whole
  council mapped to it) by gemini.

Hard-won contract lesson encoded throughout: **plain-text output contracts
(`DISAGREEMENT:`, `VERDICT:`, `ARTIFACT:`, labeled sections) parse reliably from
CLI output; JSON-shaped contracts do not.** The composer accepts substantial
prose at medium confidence rather than discarding good answers, and uses a wide
compose window so the summarizer never pauses to ask for a result already in the
transcript. `_GOVERNANCE_CONTEXT` is prepended to every role prompt so agents
ignore `can_write_files` flags and never ask the human to "enable writes" —
files are produced via `ARTIFACT:` + approval.

## Known limitations

- Agent framing prose can occasionally leak into an artifact around the
  `ARTIFACT:` content — the human approval step is the review gate.
- **gemini image vision needs `GEMINI_API_KEY`** (it uses the google-genai SDK,
  since the gemini CLI lacks clean headless image input). Without a key, gemini
  stays text-only and sees only the attachment note; claude/codex vision use the
  local CLIs and need no key.
- Each agent call re-sends an attached image, so vision adds token cost per call.
- A session paused mid-round for an agent question skips that round's step 8 on
  resume (a deliberate simplification — actions are governed Conclave-OS-side).

## Utilities

- `scripts/inspect_session.py <sid>` — round/contribution overview
- `scripts/show_contribution.py <sid> <role>` — full text of a role's contributions

## Status

- [x] Phase 0 — skeleton + MockAdapter
- [x] Phase 1 — CliAdapter (Conclave OS drives the local claude/codex/gemini
      CLIs itself); agent failures degrade to a partial answer, never a crash.
      Live integration tests auto-skip when a CLI is not on PATH.
- [x] Phase 2 — approval resolution (API + CLI) with session resume: approving
      the gate continues deliberation from where it paused (state reloaded
      from SQLite); denying cancels the session before any agent runs.
- [x] Phase 3 — agent-question passthrough: when an agent asks a clarifying
      question it becomes a Conclave OS input request (`awaiting_input` status);
      the human's answer resumes the session and deliberation continues. Plus
      richer disagreement detection (bullets, any case, multi-line claims, PASS,
      claim-role attribution) and composer polish (one strict retry on
      unparseable JSON, graceful fallbacks). Known simplification: step 8 of a
      round paused mid-call is skipped on resume — actions are governed only on
      the Conclave OS side.
- [x] Phase 4 — governed tool execution: the implementer can head its draft
      with `ARTIFACT: <filename>` to propose saving it as a file. The proposal
      becomes a `file_write` approval; only an explicit human approval executes
      the write, confined to `data/artifacts/<session_id>/` (sanitized
      filenames, no path escape). Denying the action skips the artifact but
      the session still completes; denying a session gate still cancels.
- [x] Phase 5 — service mode + web dashboard (`cli.py serve`): background
      workers run long real-agent sessions while the dashboard submits-and-polls;
      live progress, rollups, approvals/inputs in the browser.
- [x] Milestone 6 — skill registry + permission kernel (`write_file`,
      `read_file`, `search_project`), the `SKILL:` request grammar, per-file
      artifact materialization, persisted **settings**, and dashboard rollups.
- [x] Workspaces — operate on a real project directory (allowed work area) with
      hard path containment; the first Type-2 module.
- [x] Multi-modal input — text / PDF attachments folded in as context; **image
      vision** for claude, codex, and gemini (no tools, governed).
