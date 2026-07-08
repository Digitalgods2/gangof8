# Gang of 8 — Type 1: Coordinator OS

**Design document v0.1 — 2026-06-12** *(see "Current model" below for the
2026-07-01 panel-round revision; sections further down describe the original
court flow and are kept for historical reference)*

Type 1 is not a full autonomous AI operating system. It is a coordination layer that
receives a user task, decides which AI agents or tools should participate, assigns
roles, manages the discussion, prevents runaway loops, records the reasoning trail,
and returns a final answer to the user. The human always has final authority.

## Current model (2026-07-01): panel rounds + a single hard gate

Deliberation is an automatic round loop built for **diversity of intelligence**:

- **The panel.** Every enabled seat — the local `claude`/`codex`/`gemini` CLIs
  plus any enabled, keyed OpenRouter seats (DeepSeek, GLM, Qwen, Kimi) — gives
  an independent take **in parallel** each round (`Role.panelist`). The **lead**
  then synthesizes, may still `CONSULT:`/`DELEGATE:` specialist talents
  mid-round, and ends with `ROUND: DONE` or `ROUND: CONTINUE - <what's open>`
  (no marker ⇒ DONE, so a marker-ignoring model can never cause a runaway).
- **Consent-gated rotation.** Rounds proceed automatically; after
  `ROUNDS_PER_CONSENT` (default 3) rounds without DONE the run pauses and asks
  the human: *continue another block, a specific number of rounds, or compose
  now?* Budgets (`max_agent_calls`, `max_wall_seconds`) remain the hard backstop.
- **One hard gate.** The pre-run risk gate and the up-front greenfield
  "where should this go?" pause are gone. Sandbox/workspace writes, staging,
  test runs, and web access are free. The **only approval** is `promote`
  (workspace → established folder — the only write that touches real user
  code), with a diff on the approval card. A `PROMOTE:` with no known target
  asks for the destination **at delivery time** (never up front, and never
  assumed — the no-assumptions-on-greenfield-targets rule is preserved).
  Risk classification is still computed and shown, but it is informational.
- The side-effect surface is unchanged: agents still have no filesystem or
  network access of their own; everything routes through governed skills.
  Note `run_tests` in the sandbox/workspace now runs without any pre-run pause.

## Agent backend (self-contained)

**Gang of 8 owns its agent backend.** It is fully self-contained — there is no
external service, no HTTP, no API-key proxy. Agent invocation lives behind a small
adapter interface with two implementations:

- **`mock`** — a deterministic offline adapter returning canned contributions; the
  default, used by the test suite. Zero cost, zero external dependencies.
- **`cli`** — the real backend. Gang of 8 runs the local `claude`, `codex`, and
  `gemini` CLIs itself, in plain non-interactive generation mode (e.g.
  `claude -p --output-format json --tools ""`). Tools are disabled / read-only in
  those calls, so the agents perform no side effects of their own — every write
  stays governed by Gang of 8. The CLIs manage their own authentication.

Adding another backend later is just another adapter; the coordinator, governance,
and logging are agnostic to which one is active.

## Hard rules

- **Every loop is bounded.** Rounds, turns per round, total agent calls, and wall time
  all have caps stored on the session. Exceeding any cap force-stops with a partial
  answer — the system never spins.
- **Default-deny actions.** The only always-allowed capability is *text generation via
  an adapter*. Files, network, execution, messages, money, settings — anything else
  raises an `ApprovalRequest` and pauses the session for the human.
- **The Coordinator is code, not an agent.** Deterministic Python orchestrates; LLMs
  only fill roles. No fake AGI.
- **No agent-to-agent direct talk.** Everything routes through the coordinator.

## 1. Architecture

One local Python service (FastAPI + SQLite). It is self-contained: it drives the local
agent CLIs directly through the `cli` adapter — no external service to run first.

```
                 ┌─────────────────────────────────────────────┐
 user ──────────►  INTAKE (API / CLI)                          │
 (task text)     │   └─► SESSION MANAGER (id, context, state)  │
                 │         └─► CLASSIFIER (type/complexity/    │
                 │               risk/skills/approval-needed)  │
                 │               └─► ROLE ENGINE               │
                 │                     (council from registry) │
                 │                                             │
                 │   ┌──────── COORDINATOR LOOP ────────┐      │
                 │   │  bounded rounds of contributions │      │
                 │   │  disagreement → Critic → ruling  │      │
                 │   └──────────────┬───────────────────┘      │
                 │                  │ every step               │
                 │   GOVERNANCE ◄───┤  (approval gates,        │
 user ◄──pause───┤   LAYER          │   tool permissions)      │
 (approve/deny)  │                  ▼                          │
                 │   LOG STORE (SQLite + JSONL trail)          │
                 │                  │                          │
 user ◄──answer──┤   COMPOSER (answer, confidence,             │
                 │             assumptions, risks)             │
                 └─────────────────────────────────────────────┘
                                    │
                        AGENT REGISTRY / ADAPTERS
                                    │
                 ┌──────────────────┴──────────────────┐
                 │ MockAdapter   CliAdapter             │
                 │ (testing)     (local claude/codex/   │
                 │                gemini CLIs)          │
                 └──────────────────────────────────────┘
```

Modules map 1:1 to the brief: Task Intake (`intake.py`), Task Classifier
(`classifier.py`), Session Manager (`sessions.py`), Agent Registry (`registry.py` +
`adapters/`), Role Assignment Engine (`roles.py`), Deliberation Loop (`loop.py`),
Governance Layer + Tool Permission Manager (`governance.py`), Memory/Log Store
(`logstore.py`), Final Response Composer (`composer.py`).

## 2. Data models

All JSON-serializable; stored in SQLite with the JSON mirrored to a per-session JSONL
trail.

```jsonc
// Task — the immutable original request
{
  "task_id": "t_8f2c",
  "session_id": "s_2026-06-12_8f2c",
  "source": "api | cli | ui | webhook | agent",
  "text": "original user request, verbatim",
  "created_at": "2026-06-12T14:00:00Z"
}

// Classification — produced once, user-overridable
{
  "task_type": "question | research | design | code | content | action",
  "complexity": "trivial | standard | complex",
  "risk": "none | low | medium | high",
  "skills_needed": ["research", "architecture"],
  "agents_required": ["claude", "gemini"],
  "tools_allowed": false,             // may this session request external tools at all?
  "human_approval_required": false,   // true forces a gate before round 1
  "rationale": "matched rules: no external actions, multi-perspective question"
}

// Council — explicit roles; inactive roles listed so the log shows the choice
{
  "members": [
    {"role": "coordinator", "agent": "system",  "active": true},
    {"role": "researcher",  "agent": "gemini",  "active": true},
    {"role": "critic",      "agent": "codex",   "active": true},
    {"role": "architect",   "agent": "claude",  "active": false},
    {"role": "implementer", "agent": null,      "active": false},
    {"role": "governance",  "agent": "system",  "active": true},
    {"role": "summarizer",  "agent": "claude",  "active": true}
  ]
}

// RoundSpec — every round declared before it runs
{
  "round": 1,
  "goal": "gather relevant facts and constraints",
  "agents": ["researcher"],
  "max_turns": 1,
  "stop_condition": "all assigned agents returned, or timeout 120s",
  "output_requirement": "bullet list of facts with sources/uncertainty"
}

// Contribution
{
  "round": 1, "role": "researcher", "agent": "gemini",
  "content": "...", "tokens": 812, "duration_ms": 9400,
  "ts": "2026-06-12T14:01:31Z"
}

// Disagreement + Resolution
{
  "topic": "storage backend",
  "positions": [{"role": "architect", "claim": "..."}, {"role": "critic", "claim": "..."}],
  "critic_test": "critic's evaluation of both positions",
  "ruling": "position A",
  "ruling_basis": "evidence | constraint | user_goal",
  "rationale": "why the coordinator chose this path"
}

// ApprovalRequest — the human-authority primitive
{
  "approval_id": "a_01", "session_id": "s_...",
  "action": "write file C:\\...\\out.md",
  "category": "file_write",   // file_delete, code_exec, send_message, spend, settings, external
  "risk": "medium",
  "status": "pending | approved | denied",
  "requested_at": "...", "resolved_at": null, "resolved_by": null
}

// Session — the state machine + log container
{
  "session_id": "s_...",
  "status": "received | classified | awaiting_approval | deliberating | resolving | composing | done | failed | cancelled",
  "budgets": {"max_rounds": 4, "max_turns_per_round": 2, "max_agent_calls": 12, "max_wall_seconds": 600},
  "task": {...}, "classification": {...}, "council": {...},
  "rounds": [...], "contributions": [...], "disagreements": [...],
  "approvals": [...], "tools_called": [], "files_changed": [],
  "unresolved": [], "final": {...}
}

// FinalAnswer
{
  "answer": "...",
  "confidence": "high | medium | low",
  "assumptions": ["..."],
  "risks_unresolved": ["..."],
  "next_action": null   // only populated if genuinely useful
}
```

## 3. Coordinator loop (pseudocode, v0.2 — 10-step session loop)

The canonical session loop. Conflict checks and approval gates run **inside every
round**, not as one-shot phases; producing a result is an explicit
draft → critique → verdict cycle; stop conditions are first-class.

```python
def run_session(task_text, source):
    # 1. Receive task — save original request, create session ID, init state
    session = SessionManager.create(task_text, source)          # status=received
    log(session, "task_received")

    # 2. Classify — category, complexity, skills, risk, tools_allowed
    cls = Classifier.classify(task_text)
    session.classification = cls; session.status = "classified"

    # 3. Select agents — Coordinator always; others strictly by need
    council = RoleEngine.build_council(cls)
    #   researcher   if cls.needs_facts
    #   architect    if cls.needs_design
    #   implementer  if cls.produces_output
    #   critic       if cls.quality_or_risk_matters
    #   governance   if cls.touches(safety, money, files, external_actions, private_data)
    # inactive roles are recorded in the log with active=false

    session.status = "deliberating"
    verdict = None
    for round_no in range(session.budgets.max_rounds):
        # 4. Create round plan — objective, speakers, max turns, stop condition, output format
        spec = RoleEngine.plan_round(session, round_no)
        log(session, "round_start", spec)

        # 5. Run agent round — coordinator asks; each agent answers only from its role
        for role in spec.agents:
            prompt = build_prompt(session, spec, role)          # objective + context + output format
            contribution = AgentRegistry.call(council[role], prompt, timeout=spec.timeout)
            session.contributions.append(contribution)          # every contribution logged

        # 6. Conflict check — isolate, Critic tests it, Coordinator chooses, log decision
        for d in detect_disagreements(session, spec):
            d.critic_test = AgentRegistry.call(council["critic"], test_both_sides_prompt(d))
            d.ruling, d.rationale = coordinator_decide(d, cls, task_text)
            session.disagreements.append(d)                     # evidence > constraints > user goal

        # 7. Approval gate — pause, explain proposed action, wait for the human
        for action in pending_actions(session):
            if Governance.requires_approval(action):            # default-deny: anything beyond text
                gate = Governance.request_approval(session, action)
                if not await_human(gate):                       # status=awaiting_approval while paused
                    return finish(session, "cancelled")

        # 8. Produce working result — draft → critique → coordinator verdict
        if council["implementer"].active:
            draft   = AgentRegistry.call(council["implementer"], draft_prompt(session))
            review  = AgentRegistry.call(council["critic"], review_prompt(draft))
            verdict = coordinator_verdict(draft, review)        # accept | revise | one_more_round

        # 9. Stop condition — stop when ANY is true
        if (verdict == "accept"                                 # answer is good enough
            or round_no + 1 >= session.budgets.max_rounds       # max rounds reached
            or session.has_pending_approval                     # human approval needed
            or session.blocked_on_missing_info                  # progress impossible
            or session.risk_exceeds_boundary):                  # risk above allowed level
            break

    # 10. Final response — result + assumptions + risks + unresolved; save full log
    session.status = "composing"
    session.final = Composer.compose(session)                   # FinalAnswer schema (section 2)
    return finish(session, "done")                              # persists session log

# Governance.requires_approval / request_approval is the ONLY path to side effects.
# await_human() pauses the session and returns on POST /sessions/{id}/approvals/{aid}.
# Every loop above is bounded by session.budgets; exceeding any cap force-stops
# with a partial answer.
```

## 4. MVP implementation plan

- **Phase 0 — skeleton (no AI at all).** FastAPI app, SQLite schema, session state
  machine, `MockAdapter` returning canned text. Prove: intake → classify (rules only)
  → council → bounded rounds → compose → log → answer. Fully testable offline, free.
- **Phase 1 — one real backend.** `CliAdapter` running the local claude/codex/gemini
  CLIs per role, in plain non-interactive generation mode with tools disabled. The
  adapter owns the CLI invocation, timeouts, and output parsing.
- **Phase 2 — human authority surface.** Approval endpoints + a tiny CLI
  (`gangof8 approve <id>`); pending approvals block the session. Rule-based
  classifier flags any task mentioning files/exec/network/money as
  `human_approval_required`.
- **Phase 3 — disagreement resolution + composer polish.** Critic pass, ruling
  records, FinalAnswer schema enforcement.

**Deliberately NOT in the MVP:** web UI (use curl/CLI), LLM-based classification,
dynamic role invention, any tool execution, multi-session concurrency, agent-to-agent
direct talk.

## 5. Files and folders

```
Gang of 8/
├── DESIGN.md                  # this document
├── README.md
├── pyproject.toml
├── gangof8/
│   ├── __init__.py
│   ├── main.py                # FastAPI app + endpoints
│   ├── config.py              # budgets, agent config, approval categories (YAML/env)
│   ├── intake.py              # Task Intake
│   ├── classifier.py          # rule table → Classification
│   ├── sessions.py            # Session Manager + state machine
│   ├── registry.py            # Agent Registry + adapter protocol
│   ├── adapters/
│   │   ├── mock.py            # canned responses for testing
│   │   └── cli.py             # runs local claude/codex/gemini CLIs directly
│   ├── roles.py               # Role Assignment Engine + round planner
│   ├── loop.py                # Deliberation Loop
│   ├── governance.py          # Governance Layer + Tool Permission Manager
│   ├── logstore.py            # SQLite + per-session JSONL trail
│   └── composer.py            # Final Response Composer
├── cli.py                     # submit / status / approve / show-log
├── data/                      # gangof8.db + sessions/<id>.jsonl  (gitignored)
└── tests/
    ├── test_loop_mock.py      # full pipeline with MockAdapter
    ├── test_budgets.py        # loops always terminate
    └── test_governance.py     # side effects always blocked without approval
```

## 6. Safest first test case

Phase 0, MockAdapter, zero external calls, zero cost:

> Task: "Compare SQLite vs. plain JSON files for storing session logs in a local
> service, and recommend one."

Pure text in/out, no tools, no files, no network, `risk: none`. The mock adapter
returns deterministic canned contributions (researcher facts, a critic objection, a
summary), so the test asserts the entire pipeline:

1. Session created with unique ID, status walks the full state machine.
2. Classified as `question / standard / none`, `human_approval_required: false`.
3. Council activates researcher + critic + summarizer; architect/implementer
   explicitly inactive and logged as such.
4. Exactly 3 bounded rounds run; budgets never exceeded.
5. One scripted disagreement gets a recorded ruling with rationale.
6. JSONL log contains every step.
7. FinalAnswer has all four required fields.

Then re-run the identical task through `CliAdapter` as the first live test —
same harness, real local CLIs, still nothing but text.
