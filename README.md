# Conclave OS — Type 1: Coordinator OS

A coordination layer that receives a user task, classifies it, assembles an
agent council with explicit roles, runs a **bounded** deliberation loop under
**human authority**, logs everything, and returns a structured final answer.
Full design in [DESIGN.md](DESIGN.md).

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
researcher→gemini, critic→codex, architect/implementer/summarizer→claude.
Any role is remappable in settings (chosen from the local CLIs claude/codex/gemini).

## Setup

```powershell
python -m venv .venv
.venv\Scripts\python -m pip install pydantic fastapi uvicorn pytest httpx
```

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

# API
.venv\Scripts\uvicorn conclave_os.main:app --port 8790
# POST /tasks {"text": "..."} · GET /sessions · GET /sessions/{id} · GET /health
# GET /approvals · POST /sessions/{id}/approvals/{aid} {"approved": true|false}
# GET /inputs · POST /sessions/{id}/inputs/{iid} {"answer": "..."} or {"decline": true}
```

## Test

```powershell
.venv\Scripts\python -m pytest tests -q
```

## Guarantees (enforced by tests)

- Every loop is bounded — sessions always terminate; budget exhaustion yields
  a partial low-confidence answer, never a spin (`test_budgets.py`).
- Default-deny — the only approval-free capability is `generate_text`; risky
  tasks pause in `awaiting_approval` before any agent runs
  (`test_governance.py`).
- Full reasoning trail — every classification, council choice, round,
  contribution, disagreement ruling, and approval is in
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

Hard-won contract lesson encoded throughout: **plain-text output contracts
(`DISAGREEMENT:`, `VERDICT:`, `ARTIFACT:`, labeled sections) parse reliably
from CLI output; JSON-shaped contracts do not.** The composer accepts
substantial prose at medium confidence rather than discarding good answers.
Known cosmetic issue: agent framing prose can leak into artifacts around the
`ARTIFACT:` content — the human approval step is the review gate.

## Known issues / follow-ups

- **Fixed (2026-06-13): summarizer redundantly asked the human for content the
  council already produced.** Root cause was mechanical: `compose_prompt` fed
  the summarizer only the last 5 contributions truncated to 400 chars, so long
  reconciliations were cut off and it paused (`awaiting_input`) to ask for the
  very result already in the transcript. Fix: a fuller compose window
  (`COMPOSER_CONTEXT_CONTRIBUTIONS` × `COMPOSER_CONTEXT_CHARS`) plus an explicit
  "do not ask the user; synthesize from the deliberation" instruction. (Verify
  on the next real `cli` backend run.)
- Agent framing prose can leak into artifacts around `ARTIFACT:` content —
  human approval is the review gate.
- **Multi-file artifacts: resolved by the `cli` backend.** The loop parses every
  `ARTIFACT: <filename>` block into a separate approval-gated `write_file`, and
  when an output task yields no full blocks it materializes each intended file
  with a focused single-file call (`_materialize_artifacts`); resume no longer
  re-runs deliberation after an approval. A real 4-file FastAPI run produced 4
  named, gated, written files. The `cli` backend calls the agent directly in
  plain generation mode (no plan-mode) and returns its raw output, so real file
  *content* now flows through to materialization — the implementer emits actual
  code, not a description. (The earlier symptom — files materialized as
  descriptions, not bodies — came from an indirect planner-style invocation;
  driving the local CLI directly removes that layer.) Separately,
  `_GOVERNANCE_CONTEXT` was added to all role prompts so non-implementer roles
  stop treating `can_write_files=false` as a blocker.
- **Skill loop wired (2026-06-13):** agents may pull a no-approval skill
  mid-deliberation with a plain-text `SKILL: read_file <name>` line; the kernel
  authorizes it (role-gated, no approval for reads) and the result is fed back
  on a single re-call (capped by `MAX_SKILL_REQUESTS_PER_TURN`). The grammar is
  surfaced in the round prompt only when the session sandbox holds a readable
  file. Approval-gated skills (write_file) stay on the `ARTIFACT:` proposal
  path. Behavioral confirmation against real agents is still pending a run.

## Utilities

- `inspect_session.py <sid>` — round/contribution/disagreement overview
- `show_contribution.py <sid> <role>` — full text of a role's contributions
- `demo_phase4.py` — offline propose → approve → execute walkthrough
- `patch_backend.py <sid> <backend>` — backfill the backend field on old sessions

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
      `write_file` is the only action kind — by design.
