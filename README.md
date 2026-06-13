# Conclave OS — Type 1: Coordinator OS

A coordination layer that receives a user task, classifies it, assembles an
agent council with explicit roles, runs a **bounded** deliberation loop under
**human authority**, logs everything, and returns a structured final answer.
Full design in [DESIGN.md](DESIGN.md).

**Phase 1** (current): two backends behind one adapter interface —
`mock` (offline, deterministic, default) and `switchboard` (Conclave AI at
`127.0.0.1:8787` driving the real codex / gemini / claude-code CLIs).
Each role contribution becomes one single-agent `resolve` task on the
Switchboard with **all Switchboard permissions denied** — Conclave OS
governance remains the only path to side effects.

```powershell
# real agents (Switchboard must be running: cd "..\Conclave AI"; uvicorn app.main:app --port 8787)
.venv\Scripts\python cli.py submit "your task" --backend switchboard
# or: $env:CONCLAVE_OS_BACKEND = "switchboard"
```

Default role mapping (edit `conclave_os/config.py` → `ROLE_AGENTS_SWITCHBOARD`):
researcher→gemini, critic→codex, architect/implementer/summarizer→claude-code.

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

The full pipeline has been proven with real agents through the Switchboard
(2026-06-13), not just mocks:

- **Deliberation run** (`s_20260613_142ffb8d`): gemini researched, codex raised
  disagreements that were critic-tested and ruled on evidence (one correctly
  narrowed an overclaim), gemini reconciled, claude-code composed — ~3.5 min,
  6 agent calls, 3 bounded rounds.
- **Artifact run** (`s_20260613_689ece67`): three agent-question pause/resume
  cycles (gemini and claude-code both asked clarifying questions answered via
  `cli.py answer`), then claude-code proposed `ARTIFACT: README.md`, the
  session paused on a `file_write` approval, and the file was written into the
  session sandbox only after explicit `cli.py approve`.

Hard-won protocol lesson encoded throughout: **plain-text output contracts
(`DISAGREEMENT:`, `VERDICT:`, `ARTIFACT:`, labeled sections) survive the
Switchboard's protocol envelope; JSON-shaped contracts do not.** The composer
accepts substantial prose at medium confidence rather than discarding good
answers. Known cosmetic issue: agent framing prose can leak into artifacts
around the `ARTIFACT:` content — the human approval step is the review gate.

## Utilities

- `inspect_session.py <sid>` — round/contribution/disagreement overview
- `show_contribution.py <sid> <role>` — full text of a role's contributions
- `demo_phase4.py` — offline propose → approve → execute walkthrough
- `patch_backend.py <sid> <backend>` — backfill the backend field on old sessions

## Status

- [x] Phase 0 — skeleton + MockAdapter
- [x] Phase 1 — SwitchboardAdapter (Conclave AI as agent backend); agent
      failures degrade to a partial answer, never a crash. Live integration
      tests use the Switchboard's zero-cost `fake` agent and auto-skip when
      the service is down.
- [x] Phase 2 — approval resolution (API + CLI) with session resume: approving
      the gate continues deliberation from where it paused (state reloaded
      from SQLite); denying cancels the session before any agent runs.
- [x] Phase 3 — agent-question passthrough: a Switchboard `awaiting_user_input`
      pause becomes a Conclave OS input request (`awaiting_input` status); the
      human's answer resumes the same Switchboard task and the session
      continues. Plus richer disagreement detection (bullets, any case,
      multi-line claims, PASS, claim-role attribution) and composer polish
      (one strict retry on unparseable JSON, graceful fallbacks). Known
      simplification: step 8 of a round paused mid-call is skipped on resume;
      remote *action* approvals (`waiting_for_user`) are still cancelled —
      actions are governed only on the Conclave OS side.
- [x] Phase 4 — governed tool execution: the implementer can head its draft
      with `ARTIFACT: <filename>` to propose saving it as a file. The proposal
      becomes a `file_write` approval; only an explicit human approval executes
      the write, confined to `data/artifacts/<session_id>/` (sanitized
      filenames, no path escape). Denying the action skips the artifact but
      the session still completes; denying a session gate still cancels.
      `write_file` is the only action kind — by design.
