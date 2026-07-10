# Gang of 8 — Type 1: Coordinator OS

**One question. Every AI you have. One answer you can trust.**

Ask a single AI something hard and you get one perspective — with that model's
blind spots baked in. Gang of 8 asks **all of them at once**: every AI on
your machine (the claude, codex, and gemini CLIs) plus any API seats you
enable (DeepSeek, GLM, Qwen, Kimi) convenes as a *panel*, each writing its
take **independently and in parallel** — nobody sees anyone else's answer
while writing, so you get genuinely different perspectives instead of an echo
chamber. Then a **lead** model reads them all and does what a good engineering
manager does: verifies claims against the actual evidence before believing
them, adopts what's right, overrules what's wrong *by name*, and **assigns the
substantive work to specialist talents** — the coder codes, the researcher
researches, the critic checks — then integrates what they produce. Every
contribution is tagged with the exact model that wrote it.

It doesn't just talk — it **builds**. Ask for working software and the
council designs it, the lead delegates the authoring to its coder talent
(whose ARTIFACT output is captured directly as real files), runs the tests,
and **fixes its own failures** before anything ships. Deliberation rotates
automatically, pausing to ask *you* only when it wants more rounds; work
happens freely in a sandboxed scratch space; and exactly **one hard gate**
stands between the council and your real files — a diff-carrying approval
that nothing crosses without your click. Everything is bounded (budgets,
wall-clock, depth caps), everything is logged, and a run that fails says so
honestly instead of dressing up a failure as an answer.

The result: the diversity of a committee, the decisiveness of a single owner,
and the receipts of an audit trail — running entirely on your desk. See
[How a deliberation works](#how-a-deliberation-works-the-panel-model) below;
full design in [DESIGN.md](DESIGN.md).

Gang of 8 is **fully self-contained**: it runs the local agent CLIs itself.
Two backends sit behind one adapter interface — `mock` (offline, deterministic,
default) and `cli` (the real backend: Gang of 8 invokes the local `claude` /
`codex` / `gemini` CLIs directly, in plain non-interactive generation mode,
e.g. `claude -p --output-format json --tools ""`). Tools are disabled /
read-only in those calls, so agents perform **no** side effects themselves —
Gang of 8 governance remains the only path to side effects.

```powershell
# real agents — nothing else to start; the CLIs manage their own auth
.venv\Scripts\python cli.py serve --backend cli
# or one-shot: .venv\Scripts\python cli.py submit "your task" --backend cli
# or: $env:GANGOF8_BACKEND = "cli"
```

Default role mapping (edit `gangof8/config.py` → `ROLE_AGENTS_CLI`):
lead→claude, researcher→gemini, critic→codex, summarizer→claude. Any role is
remappable in settings. The **panel** roster is derived automatically: the
installed CLI agents plus every OpenRouter seat you enable in Settings
(DeepSeek, GLM, Qwen, Kimi — needs an API key).

It ships a **web dashboard** with a chat composer, governs real-code delivery
through the approval-gated **promote** skill, can operate on a real project
directory (**workspaces**), and **reads attached images** (text, diagrams,
screenshots) — all detailed below.

## How a deliberation works (the panel model)

For output tasks, **Council integration review** is available in Settings. After
the blind best-of-N vote, the codifier examines every scored candidate for
concrete complementary strengths. It may offer a separately runtime-validated
integrated candidate, but that proposal never replaces the voted winner
automatically: the dashboard shows its full content and the human chooses **Use
integration** or **Keep voted winner**.

When a session starts, the council roster shows one chip per seat — and each
seat is a **different origin model**. Here is what each is doing:

- **The panelists** (orange chips) are the heart of the design. Every round,
  ALL of them — the local claude/codex/gemini CLIs plus every enabled
  OpenRouter seat — get the task (plus the pre-read project context) and write
  their take **independently and in parallel**. None of them sees the others'
  answers while writing, deliberately: you get N genuinely different
  perspectives instead of N models agreeing with whoever spoke first. That is
  the diversity-of-intelligence bet the whole system is built on. Panel seats
  have real hands, not just voices: they pull the same governed discovery
  skills as the lead (`SKILL: read_file/search_project/list_dir/web_search`,
  resolved mid-fan-out) so takes are grounded in the actual files. **On a file
  build, each seat authors its own COMPLETE candidate implementation** — saved
  to the sandbox immediately, namespaced per seat (`codex__index.html`), never
  clobbering each other.
- **Best-of-N: the winning file is a real model's code, not a re-write.** On
  any task that produces a file, every candidate is first **executed headless
  and any that crash on load are disqualified** — because judging by *reading*
  is blind to runtime failure (a file can read as complete and correct and
  still show a black screen). Only files that actually run are then **scored
  blindly by independent judge seats** (author identity stripped — each judge
  sees "Candidate 1…N", scores every one on completeness / correctness /
  fidelity / robustness, and names a winner). The highest-scoring file is
  shipped as the default, credited to the model that wrote it — with
  an optional pass of *surgical* fixes for concrete defects the judges flagged
  (re-executed afterward; if a fix breaks the file, the original winner ships
  instead). When **Council integration review** is enabled in Settings, the
  codifier also evaluates every scored candidate for concrete complementary
  strengths. It can offer a separately runtime-validated integrated candidate,
  but the proposal never replaces the vote winner automatically: the human
  chooses **Use integration** or **Keep voted winner** in the dashboard.
  If only one candidate runs, it wins without a vote; if none run,
  no winner is declared. The dashboard shows it: 🗳️ candidates, 💥 crashers
  rejected, ⚖️ each scored, 🏆 the winner. Filename disagreements never cost a
  candidate: when each seat wrote a single file — even under different names (a
  task may invite an author-chosen title) — **all** of them are judged together
  and the winner ships under its own name; only a genuine multi-file build
  groups by the most-agreed name. Judging is scored purely on the content for a
  prose deliverable — the "does it run / animate under play" weighting applies
  only to candidates that actually carry headless-runtime evidence, never to a
  `.txt` story.
- **The lead** (yellow chip) is the **orchestrator** — it kicks the task off,
  feeds the jobs to the panel, and while they work can pull in talents
  (`CONSULT:` for advice, `DELEGATE:` for production, captured as real files) or
  a critic as needed. It stays on a **fast** model on purpose: it's on the
  serial critical path, so a slow, heavy model there stalls or times out the
  whole run. It ends each round with `ROUND: DONE` or `ROUND: CONTINUE`, and
  delivery (`PROMOTE:`) is always gated by your approval.
- **The codifier does the strong post-panel work.** Once the panel returns its
  candidates, *examining and finishing* them wants a **strong** model — the
  opposite of the lead's fast one. That job belongs to the **Summarizer seat**:
  set it to a strong model in Settings → Role mapping and it becomes the
  council's codifier. On a file build it **chairs best-of-N** — ratifies or
  overrides the blind vote (reading the top two in full, since judges score by
  reading and can miss a real bug), **finishes** the winner with surgical fixes,
  and when *every* candidate crashes it **recovers** the most complete attempt
  rather than discarding the panel's work. On a plain-answer task it composes the
  answer. Same seat, both jobs — and it runs with a longer timeout because the
  examine-and-finish step is meant to think hard. So a run reads as: a fast lead
  orchestrating, then the strong summarizer codifying.
- **Why the lead's model also appears as a panelist:** same model, two
  different jobs. As a panelist it authors one candidate among many; as the
  lead it judges/orchestrates — a separate call with a separate charter. (Remap
  the lead in Settings if you'd rather a different model arbitrate.)
- **The summarizer** (purple chip) composes the final answer card for
  question/research tasks. Build tasks usually skip it entirely — they get a
  fast, deterministic file-manifest summary instead.
- **The specialist talents** (critic, red_team, fact_validator, …) sit dormant
  until the lead recruits one — then the dashboard shows it live: the status
  banner reads `🤝 code_generator ← claude (delegate): …` the moment the
  assignment is made, a dashed 🤝 chip joins the roster, and a plain-language
  recruitment row appears under the Council card ("claude · opus called in as
  Code Generator — answered in round 2"). Each role can pin its own model
  (Settings → Role mapping → agent · model; role pin › seat pin › CLI
  default), so a rarely-called coder talent can run a heavyweight model while
  the seat's everyday default stays fast. A delegation that fails is retried
  once before the lead falls back to doing the work itself.

**Rotation is automatic but consent-gated.** Rounds proceed on their own; if
the lead declares CONTINUE three times (`ROUNDS_PER_CONSENT`, settable), the
run pauses and asks you: *continue another block, a specific number of rounds,
or compose the final answer from the work so far?* Budgets (agent calls, wall
time) remain the hard backstop underneath. A round costs `len(panel) + 1`
model calls (every seat + the lead synthesis) — which is exactly why the
consent gate exists.

**One hard gate.** Work happens freely in the council's own sandbox/workspace
(writes, edits, test runs, staging, web lookups — no approvals), and
council-space skills are open to **every seat**: a role being unable to land
its work in the sandbox is treated as a design failure, so role-gating never
blocks council work. The single approval in the whole pipeline — and the one
place that stays role-gated (the lead decides delivery) — is **promote**:
copying a finished file into your real folder, with a diff on the approval
card that says whether it **creates a new file or OVERWRITES an existing one**.
It lands where the task said to save it — an explicit *"save it in ‹folder›"*
target is honored as the destination even when the task *reads* from a different
folder, so a "read from A, save to B" job delivers to B and never overwrites the
source A. A promote with no known destination asks you *where* at delivery time
— never up front, never assumed.
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
.venv\Scripts\python -m pip install --upgrade pip
.venv\Scripts\python -m pip install -e ".[dev]"
```

- `pypdf` — extract text from attached PDFs.
- `google-genai` — gemini image vision (inline-image inference; needs `GEMINI_API_KEY`).
- `Pillow` — used only by the test suite to generate images.

The `cli` backend uses your locally-installed agent CLIs (`claude`, `codex`,
`gemini` on PATH), each with its own auth — there is no external service.

The editable install provides the `gangof8` command as an alternative to
`python cli.py`. Development commands and module ownership are documented in
[DEVELOPMENT.md](DEVELOPMENT.md).

### How Gang of 8 detects your CLIs

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
  with `WinError 2`. Gang of 8 resolves the shim's real path via
  `shutil.which` before launching — if you ever add another npm-installed CLI,
  that's the pattern to copy.

To see for yourself what would be detected, run the same probe by hand:

```powershell
Get-Command claude, codex, gemini -ErrorAction SilentlyContinue
```

Each CLI manages its own login (`claude`, `codex`, and `gemini` all have their
own auth flows) — Gang of 8 never sees or stores those credentials. Which
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

# Selected API routes (the dashboard uses additional internal routes)
.venv\Scripts\uvicorn gangof8.main:app --port 8790
# POST /tasks {"text": "...", "attachments": ["up_..."]} · GET /sessions · GET /sessions/{id}
# POST /uploads {"name": "...", "content_base64": "..."}  → {id, kind, ...}
# GET /approvals · POST /sessions/{id}/approvals/{aid} {"approved": true|false}
# GET /inputs · POST /sessions/{id}/inputs/{iid} {"answer": "..."} or {"decline": true}
# GET/PUT /settings · GET /settings/seats · GET/POST /workspaces · PUT /workspaces/active
```

## Web dashboard

`python cli.py serve --backend cli` (or `--backend mock`) starts the single-page
dashboard service and prints its address: `http://127.0.0.1:8790/`.

- **Bottom-center chat composer** — auto-growing text box (Enter sends,
  Shift+Enter for a newline), a **Clear** button, and a **+** menu to attach a
  **document (text / PDF)** or **image**. Attachments show as removable chips.
- **Live progress** — the session state persists at every step (each landed
  contribution, round start, talent recruitment), so the poll sees work as it
  happens: a pulsing status banner (current round goal, the seat being
  awaited, in-flight talent pulls, a ticking elapsed timer) and a council
  roster that green-checks each agent — with its exact model — as it
  contributes, plus a plain-language recruitment feed under the roster.
- **Rollups** — the final answer plus a one-line stats summary; contributions
  and disagreements collapse to informative one-line summaries (expandable),
  with open/closed state preserved across the 3 s refresh. Finished sessions
  grow a **Respond to the council** box — multi-modal like the task composer
  (attach documents/images) — that re-deliberates with the whole thread.
- **Approvals & questions** — file-write gates surface **Approve / Deny**
  buttons (naming the exact target path); agent questions surface an
  **Answer / Decline** box.
- **Settings gear** — backend, per-role agent mapping, workspaces (add /
  activate / use-sandbox), governance / composer tunables, and UI prefs, each
  with a hover tooltip explaining its purpose.

## Operations and Audit

Each session response includes a compact run audit: accumulated model execution
time, contribution counts by agent and model, repair attempts, action statuses,
and SHA-256 fingerprints for files written during the run. The dashboard summary
shows the key execution numbers; `GET /sessions/{id}` returns the full view.

The header's `Diag` control opens a redacted setup report with storage/sandbox
availability, configured seats and timeouts, the active workspace, API-key
presence (never key values), web capability, and remote-access mode.

## Local-only service

Gang of 8 is a desktop-local service by design. It can browse local folders,
open a delivered file with the OS, and reveal locally stored API keys, so it
binds and accepts requests only from loopback addresses by default.

To bind beyond localhost, pass `--allow-remote` and set
`GANGOF8_ALLOW_REMOTE=1`; do this only behind an authenticated reverse proxy.
Key reveal and OS file-open requests remain local-only even in that mode.

## Test

```powershell
.venv\Scripts\python -m ruff check gangof8 tests
.venv\Scripts\python -m pytest tests -q
```

GitHub Actions runs the same checks for pull requests and pushes to `main`.

## Skills & permission kernel

Side effects are governed by a data-driven **skill registry** (`skills.py`) and
a **permission kernel** (`governance.authorize_action`). Each `Skill` declares
its category, risk, `requires_approval`, allowed roles, and inputs; the kernel
role-gates every action and decides on that metadata — never on hardcoded
behaviour.

| Skill | Approval | Roles | What it does |
|-------|----------|-------|--------------|
| `write_file` / `edit_file` | none | **every seat** | Write/edit files in the council's own sandbox or workspace |
| `run_tests` | none | lead, implementer, critic, code_generator | Run a test command inside the council's spaces (time/output-bounded) |
| `read_file` / `search_project` / `list_dir` | none (read) | **every seat** | Read, grep, and list — sandbox, workspace, or the established folder |
| `web_search` / `web_fetch` | none (read) | **every seat** | Governed live-web lookups via the coordinator |
| `stage` | none | lead, implementer | Move a file up from sandbox into the permanent workspace |
| `promote` | **required** (human, with diff) | lead, implementer | **The ONE gate** — copy a council file into your real (established) folder |

- **Producing files**: an `ARTIFACT: <filename>` block (full contents after
  the header) writes freely into the sandbox — from the lead, from a
  **delegated talent** (captured directly, attributed to that talent and its
  exact model), or from a **panel seat** (saved namespaced per seat as an
  advisory draft). A `PROMOTE: <filename>` line — the lead's call — is what
  proposes real delivery; that is where the approval (and, if unset, the
  "where should this go?" question) happens. If an output task names files but
  emits no full blocks, the coordinator **materializes** each with a focused
  single-file call.
- **Reading mid-deliberation**: any seat — panelists included — may emit a plain-text
  `SKILL: read_file <path>` or `SKILL: search_project <query>` line; the kernel
  authorizes it (reads need no approval) and the results are fed back on a
  re-call. A follow-up reply may open a NEW request (read one file → the next
  read depends on what it said) — those resolve too, as a bounded chain
  (`MAX_SKILL_CHAIN_TURNS`, default 3 re-calls) with every result accumulated
  and repeats never re-executed. A reply that is nothing but an unresolved
  request is a stub, never a synthesis. The grammar is advertised to a seat
  only when it's useful.
- **No agent does I/O itself** — every write flows through the executor +
  human approval; the only ungated capability is `generate_text`.

## Delivery resilience (the safety-net ladder)

A file-producing run must end in exactly one of two states: a real, complete
file behind the promote gate, or an honest failure that says so. Between a
flaky seat and an empty delivery stands a ladder of recoveries — each rung
tried only when the one above produced nothing:

1. **The lead's own `ARTIFACT:` blocks** — the normal path.
2. **Materialization** — the draft only *described* the files: each intended
   file is fetched with its own focused single-file call (the lead's long
   timeout, since it authors whole files). Intended names come from the draft
   and the task text; when a revision follow-up ("slow the ghosts down")
   names no file at all, the fallback is the established folder's files the
   panel discussed by name (two-plus mentions, so a stray reference to an
   unrelated file doesn't qualify).
3. **Panel salvage** — the lead shipped nothing (timed out, errored, or
   stubbed): the best **complete** file a panelist pasted inline is recovered
   and proposed for write + promote, attributed to that seat. Snippets and
   patches are rejected — an HTML target must be a whole document — so
   revision *advice* is never shipped *as* a file (`test_salvage.py`).
4. **Honest failure** — still nothing real on disk ⇒ the final answer reports
   artifact verification failed, at low confidence, with the next action —
   never a success card over a missing file.

Three more nets around the ladder:

- **Truncation continuation** — a large single-file artifact cut off
  mid-generation is finished from where it stopped (append), not re-drafted.
- **Redelivery auto-promote** — a follow-up that revises an already-delivered
  file but omits its `PROMOTE:` line gets the promote synthesized
  automatically (behind the same approval gate), so "modify this" follow-ups
  land in your folder instead of stranding the update in the sandbox. Whenever a
  destination is declared — a folder the task referenced, or an explicit *"save
  it in ‹folder›"* target — every authored deliverable is proposed for promote,
  brand-new files included, so the run never reports success while your named
  folder stays empty. With no declared destination, nothing is force-shipped.
- **Stub detection** — a synthesis that merely announces the work, is blocked
  tool-call debris, or ends on a dangling unresolved `SKILL:` request is never
  accepted as a round's result: the lead is re-called once demanding the
  result now, and if it stubs again the composer synthesizes from the panel
  views instead.

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
- Cancel always lands — every blocking wait (panel fan-out, the up-front
  context build, API-seat HTTP calls) polls the cancel flag instead of
  blocking on the slowest seat, so "Cancel run" finalizes promptly even with
  a stuck seat in flight (`test_cancel.py`).
- No fabricated deliveries — a file-producing run either puts a real, complete
  file behind the promote gate or reports the failure honestly; fragments and
  advice snippets are never shipped as files (`test_salvage.py`,
  `test_materialize.py`).
- **Nothing that doesn't run ships** — every produced web file is executed
  headless (stubbed DOM, via Node) *before* the promote, and a file that throws
  on load fails verification: its promote is stripped and the run reports honest
  low-confidence failure, never a false success. Judging a file by reading is
  banned as the sole criterion (`test_smoke.py`, `test_best_of_n.py`). Scope: a
  smoke test catches load/first-frame crashes — the "dead on arrival" class —
  not post-gameplay logic bugs.
- Full reasoning trail — every classification, council choice, round,
  panel contribution, synthesis decision, and approval is in
  `data/sessions/<id>.jsonl`, with session state in `data/gangof8.db`
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
  resume (a deliberate simplification — actions are governed Gang of 8-side).

## Utilities

- `scripts/inspect_session.py <sid>` — round/contribution overview
- `scripts/show_contribution.py <sid> <role>` — full text of a role's contributions

## Status

- [x] Phase 0 — skeleton + MockAdapter
- [x] Phase 1 — CliAdapter (Gang of 8 drives the local claude/codex/gemini
      CLIs itself); agent failures degrade to a partial answer, never a crash.
      Live integration tests auto-skip when a CLI is not on PATH.
- [x] Phase 2 — approval resolution (API + CLI) with session resume: approving
      the gate continues deliberation from where it paused (state reloaded
      from SQLite); denying cancels the session before any agent runs.
- [x] Phase 3 — agent-question passthrough: when an agent asks a clarifying
      question it becomes a Gang of 8 input request (`awaiting_input` status);
      the human's answer resumes the session and deliberation continues. Plus
      richer disagreement detection (bullets, any case, multi-line claims, PASS,
      claim-role attribution) and composer polish (one strict retry on
      unparseable JSON, graceful fallbacks). Known simplification: step 8 of a
      round paused mid-call is skipped on resume — actions are governed only on
      the Gang of 8 side.
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
- [x] Orchestrator model — the lead organizes and integrates, never does the
      substantive work itself: it assigns via `CONSULT:`/`DELEGATE:`, a
      delegated talent's `ARTIFACT:` blocks are captured directly as real
      files (attributed to that talent + its per-role pinned model), failed
      delegations retry once, and delivery (`PROMOTE:`) stays the lead's call
      behind the one human gate.
- [x] Council-space access for every seat — read/write/discovery skills open
      to ALL roles (a role unable to land its work in the sandbox is a design
      failure); panel seats resolve `SKILL:` reads mid-fan-out and their
      complete files save immediately, namespaced per seat, as advisory
      drafts. Mid-run state persists at every step, so the dashboard shows
      contributions, recruitments, and drafts live.
- [x] Delivery resilience — the safety-net ladder (materialization with an
      established-folder fallback for revision follow-ups, panel artifact
      salvage, honest failure reporting), redelivery auto-promote, chained
      `SKILL:` resolution with dangling-request stub detection, and
      cancel-aware waits throughout the round loop.
- [x] Best-of-N selection — every panel seat authors a complete candidate;
      candidates are executed headless and crashers disqualified; survivors are
      scored blindly by independent judges; the winning file ships byte-for-byte
      (credited to its model), with an optional re-verified surgical fix pass.
- [x] Runtime delivery gate — every produced web file is executed (stubbed-DOM
      Node smoke test) *before* the promote; a file that throws on load is
      blocked from delivery and the run reports honest failure. Verification was
      moved ahead of the promote so nothing ships unverified.
