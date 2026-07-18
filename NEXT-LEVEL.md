# Gang of 8 — Next-Level Assessment

*2026-07-18. A full-codebase examination (~22,400 lines: service, loop,
rounds, goals, assembly, smoke, browser acceptance, adapters, reporting,
logstore, diagnostics, settings, governance, skills, dashboard) conducted
after the architecture overhaul (Phases 1–3) shipped and two games released
at 4 and 14 calls. This is not a bug list — the overhaul closed the failure
loops. It answers one question: what would lift the program to a new height,
and nothing else.*

---

## 1. Where the system stands

The hard parts are now genuinely good, and the recommendations below build
on them rather than replacing anything:

- **A deterministic verification stack that has never been wrong** — Node
  smoke (advisory), real-Chrome acceptance (authoritative), style contract,
  export probes, load-order analysis — with harness false-rejections now
  structurally impossible.
- **Right-sized planning with hard validation** — one artifact, one owner;
  measured at 283 → 4 calls on the identical brief.
- **Self-healing failure routing** — fault attribution with constraints
  stated, escalation ladder, repair-first releases, stale-assembly
  invalidation, budget caps with resume-as-review.
- **A complete event vocabulary that nobody can see.** `log_event` is a
  single choke point; `reporting.py` maintains **105 human-labeled event
  types with icons**; a per-session timeline endpoint exists. This is a
  fully built nervous system with no face.

## 2. The two flagship gaps

### Gap A — The app is a black hole while it runs

Everything the operator learns during a run comes from polling snapshots
(3s active / 20s idle) of session/goal state, rendered as status pills.
The *narrative* — what the system is doing, why, and what just changed —
exists only in per-session JSONL timelines behind a click. During this
week's builds, every meaningful moment (fault attribution firing, a
package reopening, browser acceptance passing, codex taking over a dead
seat) was invisible until someone queried the API by hand.

### Gap B — A seat outage is indistinguishable from a fatal error

Case study, from the Asteroids build: claude's CLI hit its **monthly spend
limit** mid-authoring. What actually happened was healthy — the panel
dropped the dead seat, helper codex authored the game, every gate passed,
the goal released. What the operator *saw* was: a failed session, a goal
paused with `"artifact verification failed; no file was delivered"`, and
attempts silently burned against a seat that could not possibly answer.
The truth ("claude is quota-capped until reset; codex covered") existed
only inside one session's `unresolved` list. There is no seat-health
model: `auth_status()` exists but runs once in a settings path;
`/diagnostics` reports whether a CLI is *installed*, not whether it can
*answer*; no error classification distinguishes quota, capacity, auth
expiry, or crash; scheduling never consults seat health.

These two gaps are one theme: **the system makes good decisions and cannot
explain itself.**

## 3. Recommendations

### R1 — Live activity feed (SSE) · *the black-hole fix*

`log_event` already funnels every event through one method. Add an
in-process ring-buffer publisher there and one endpoint:

- `GET /events/stream` — Server-Sent Events; each event rendered through
  the existing `reporting.format_timeline` vocabulary (icon, label,
  detail, session/goal refs). Include a `?since=` cursor for reconnects.
- Dashboard: a persistent **live feed pane** (EventSource, no polling)
  showing the last ~50 events across all sessions/goals:
  `▶ claude authoring index.html · 41k chars` ·
  `🔁 reopening package 1 — load-order fault in renderer.js` ·
  `✓ real-browser acceptance passed (chrome)` ·
  `🔴 claude dropped: monthly spend limit`.

Nearly all cost is already sunk — the events exist, the labels exist, the
formatter exists. This is the highest leverage-to-effort item in the
codebase.

### R2 — Goal-level timeline & auto-postmortem

Events are logged per-session (goal events under `"-"`), so a goal's story
is scattered across files. Add `GET /goals/{id}/timeline` aggregating the
goal's sessions + goal-scoped events into one ordered narrative, rendered
in an expandable panel on the goal card. On any terminal state (released /
paused / cancelled), derive a **postmortem block** at the top: packages
built, faults attributed and to whom, escalations, calls spent per seat,
gates passed. Every forensic reconstruction performed by hand this week —
sqlite queries, jsonl spelunking — becomes one click. This also compounds
R1: the feed is *now*, the timeline is *what happened*.

### R3 — Seat health as a first-class state machine · *the outage fix*

- **Classify adapter failures** at the one place they all pass through
  (`_agent_call` / adapter error normalization) into a small taxonomy:
  `quota_exhausted` ("spend limit", "usage limit"), `capacity` ("at
  capacity", 429/529), `auth_expired` ("login", 401), `offline` (not on
  PATH, spawn failure), `timeout`, `error`.
- **Maintain per-seat health** in the service: state + reason + since +
  last-verified, updated on every classified failure and every success;
  `auth_status()` runs at startup and on demand.
- **Expose it**: seat badges in the dashboard header (🟢 healthy ·
  🟡 degraded · 🔴 capped/offline, with the reason and, for quota, the
  provider's own message: "raise at claude.ai/settings/usage"), plus a
  `seats` block in `/diagnostics`.
- **Consult it**: scheduling (owner assignment, escalation targets,
  verifier pool, panel composition) skips seats in a red state instead of
  burning attempts; a capped seat's packages transfer via the existing
  Phase-2 ladder immediately rather than after failed retries; goal
  `last_error` states the seat truth ("claude quota-capped; codex took
  over") instead of the downstream symptom ("no file was delivered").

This converts the Asteroids incident from "looked fatal, was lucky" into
deliberate, visible, boring behavior.

### R4 — Live output tail for active calls

The progress plumbing (`register_progress(chars, detail)`) already ticks
per chunk; adapters stream (OpenRouter chunked; claude CLI stream-json).
Extend the callback to carry a rolling tail (~last 400 chars) into
`active_agent_calls`, and render it in the session/goal view as a live
"what the model is writing right now" strip. Watching a 20-minute
authoring call produce visible code is the difference between trust and
anxiety; it also makes a stalled call obvious to a human long before the
stall watchdog fires.

### R5 — Plain-language "now" line

Derive a single sentence per session/goal from its latest event and phase
map — "authoring index.html (claude, 4m 10s)", "verifying in real
Chrome", "waiting for your approval", "paused: budget reached (14/40)" —
and show it on every card. The internal states (`deliberating`,
`composing`, `draining`) are coordinator jargon; the operator should never
need to translate. Purely derived; no state added.

### R6 — Attempt classification in the cost ledger

Phase 3 counts spend; it does not say *why* attempts burned. Tag each
counted attempt with its outcome class (completed / seat-outage /
timeout / interrupted) so the goal card can render "14 calls: 9 completed,
3 seat-outage, 2 interrupted" and the postmortem (R2) can separate model
cost from infrastructure cost honestly. This week that distinction — 4
calls, "three burned by app bugs, not the model" — had to be reconstructed
manually to be fair to the models.

## 4. Considered and rejected (so they are not re-litigated)

- **WebSockets / rich frontend framework** — SSE + the existing vanilla
  dashboard reach the same outcome with a fraction of the surface.
- **Splitting loop.py / service.py** (5,151 / 4,678 lines) — real, but it
  is enabling work with zero operator-visible lift; do it opportunistically
  as R1–R6 touch those files, not as a project.
- **New model providers (Ollama Cloud etc.)** — orthogonal to the current
  ceiling; seat health (R3) is the prerequisite that would make any new
  seat safe to add anyway.
- **More deterministic gates** — the stack is strong and now correctly
  humble (advisory emulator, authoritative browser). Adding gates now
  adds friction, not quality.
- **Config/typing/docs/CI hygiene passes** — obligatory-review filler; the
  suite (700+ tests) and ruff already hold the line.

## 5. Sequencing

| Order | Item | Why first |
|---|---|---|
| 1 | R3 seat health | Prevents the worst operator experience (fake-fatal outages); other items display its output |
| 2 | R1 live feed | Highest leverage-to-effort; everything else renders into it |
| 3 | R5 now-line + R2 goal timeline/postmortem | Completes the story layer |
| 4 | R4 output tail | Adapter-by-adapter; claude/OpenRouter first |
| 5 | R6 attempt classes | Small; rides on R2/R3 plumbing |

The through-line: the machine already thinks clearly — these six items let
it *speak*.
