# Gang of 8 — Architecture Review & Overhaul Plan

*2026-07-17. Written after two days of live failure forensics on goals g_2d482c7e
and g_8f31dff7 (both: "build a single-file HTML5 Frogger with 7 LLMs").*

---

## 1. What the data says

Straight from the session store:

| Goal | Sessions | Model calls | Call distribution | Outcome |
|---|---|---|---|---|
| g_2d482c7e (Frogger #1) | **272** | **283** | claude 142, glm 136, all five other seats **1 each** | Failed. Human hand-assembled and delivered the file. |
| g_8f31dff7 (Frogger #2) | **46** | **74+** | claude 28, gemini 25, codex 9, qwen 7, deepseek 3, kimi 1, glm 1 | Cancelled by the operator after 2 days. |
| **g_864401cc (Frogger #3, post-Phase-1 benchmark)** | **4** | **4** | claude 1 (authored the complete game), codex 3 (verification; 2 attempts lost to infra bugs fixed en route) | **Passed browser acceptance and independent verification; released for approval in under a day.** |

*Benchmark verdict (2026-07-18): the identical brief under right-sized
planning produced one package, one frontier author, a working game on the
first authoring call, and a clean independent PASS — 4 calls total against
283. The two lost verifier calls found and fixed two latent release-gate
bugs (capacity outage recorded as rejection; verifier CLI judging an empty
sandbox), which the old architecture's noise had always masked.*

For reference: a single frontier model produces a complete, working
single-file Frogger in **one call**, and one verify pass makes it three.

Two findings jump out of the distribution:

1. **The "7-model team" is an illusion.** Goal 1 was actually a two-model
   retry death spiral (claude + glm burned 98% of 283 calls rebuilding the
   same file against a misattributed fault). Five of the seven seats
   contributed one call each — they were paid extras.
2. **Every failure was a seam failure.** Not one defect in either goal was
   "a model couldn't write Frogger." Every single one was two owners
   disagreeing about a boundary:
   - renderer.js read `Frogger.World` at load time; the template loaded
     world.js after it (owner A vs owner B's load order)
   - the template's 33 CSS classes vs a stylesheet that styled 4 of them
     (one owner's two files disagreeing with each other)
   - a Google-Fonts `@import` vs the self-contained-file release contract
   - world.js putting the river where the road belongs (owner's data vs
     the entry point's gameplay expectations)

   A single model holding the whole file in one context cannot make any of
   these mistakes. **The architecture manufactures the failure class it
   then spends hundreds of calls trying to repair.**

Standard (non-goal) jobs have the same economics baked in:

- `config.py` policy comment: *"Full roster policy: the configured panel is
  the council. A Gang of 8 run convenes every configured panel seat; never
  silently tier or auto-bench seats."* Every seat contributes **every
  round**, three rounds per consent block.
- Best-of-N: every panel seat authors a **complete candidate**; blind
  judges vote; one ships. For 7 seats that is 6 discarded full
  implementations per job, plus judge calls — and the app's own
  `smoke.py` docstring records why judged reading is unreliable: a
  Centipede that crashed on frame 1 won a unanimous 5-judge vote.

## 2. Diagnosis — five structural verdicts

**V1. The unit of work is backwards: the task is sized to feed the roster.**
A one-file game was split into 9 staged files across 8 packages so that
every seat had something to own. That is Conway's law as a cost center —
the deliverable's structure mirrors the org chart, and every added seam is
a place two models can disagree. Seams grow roughly with the square of
participants; capability grows far slower than that.

**V2. The full-roster policy converts capability into supervision burden.**
The budget seats (qwen, glm, kimi, deepseek via OpenRouter) are slower
(10–20 min/package with stall-watchdogs) and produced most of the defects.
Every defect they introduce consumes *frontier* calls to detect, attribute,
review, and re-verify. This is the precise mechanism behind "the app
performs worse with 3 frontier models than with one": the frontier models
are demoted from authors to janitors.

**V3. Redundant inference without a decisive selector.**
N complete candidates are only worth their cost if selection is cheap and
correct. Model judges reading code are neither (see Centipede). The
deterministic verification stack (smoke, export probe, browser acceptance,
style contract) is now the *actual* referee — which makes the blind vote
ceremony, and makes N>2 candidates almost pure waste.

**V4. Repair is routed away from the point of detection.**
When the release verifier (codex) found the inverted world layout, it knew
the fix. The ownership model forbade it from fixing anything beyond narrow
inline edits, so the app's best case was: fail → map critique → reopen
deepseek's package → full rebuild → reassembly → full re-verification.
Minutes of fix, hours of process.

**V5. Failure feedback was symptom-only and stateless.** *(Largely fixed
this week, listed for completeness.)* Retry prompts stated symptoms without
constraints; gates disagreed about what "passing" meant; fault streaks
reset on every rebuild; stale assemblies and stale defect registers
poisoned later cycles. Fifteen commits now close these — but they reduce
the *cost of each loop*, not the *number of seams that cause loops*. That
requires the structural changes below.

## 3. What is genuinely good (keep, unchanged)

- Durable goal/session store, worker leases, event log — solid.
- Deterministic assembly with accepted hashes and provenance.
- The verification stack: Node smoke harness, export probe, real-browser
  acceptance, style contract — now mutually consistent, and the only
  referee that has never been wrong in two days of forensics.
- Fault attribution: stack mapping, load-order analysis, semantic critique
  routing, streak breaker with resume-as-review.
- The author/verifier separation for release (independence is right; the
  *powers* of the verifier are wrong — see P4).

## 4. Overhaul principles

**P1. The roster serves the task; the task is never shaped to serve the
roster.** Team size is an output of triage, not a constant.

**P2. Tier the seats.** Frontier seats (claude, codex, gemini) author and
verify. Budget seats are optional specialists for genuinely parallel,
low-coupling work — or benched. Benching a seat is success, not failure.

**P3. Deterministic verification is the referee.** Models author and
repair; gates decide. Blind votes only where gates cannot reach (taste),
and never with more than 2–3 candidates.

**P4. Repair at the point of detection.** Whoever holds the defect and a
concrete fix applies the fix (with provenance recorded). Package rebuild is
the fallback for defects *without* a known fix, not the default for all.

**P5. Escalate, don't repeat.** A failed retry never goes back to the same
seat with the same brief a third time. Ladder: owner retry (with
constraint) → strongest frontier seat takes the package over → human.

**P6. Make the economics visible.** Every goal shows calls spent, per seat,
against a budget. Runaway loops become impossible to miss and impossible
to silently continue.

## 5. The plan

### Phase 1 — Triage & right-sizing *(implemented 2026-07-17)*

1. **Complexity triage** at submission and at goal planning. Three modes:
   - **solo** — one frontier author + gates + one independent verify pass.
     Default for single-artifact deliverables (any one-file app, script,
     document).
   - **pair** — author + independent frontier reviewer/verifier.
     Default for small multi-file work.
   - **team** — the current build-team machinery. Only for genuinely
     decomposable systems (multi-service, large codebases), and only with
     packages that map to *natural* artifact boundaries.
2. **Hard planning rule:** a goal may not have more packages than the
   deliverable has natural artifacts. One file = one owner. The planner
   prompt is rewritten accordingly; the plan validator rejects violations.
3. **Kill the full-roster default** for standard jobs: lead + one reviewer
   unless the user opts into the council (`--council`). Best-of-N becomes
   opt-in with N ≤ 3, judged by gates first and models second.
4. **Goal roster defaults to frontier seats.** Budget seats join only when
   the plan contains parallelizable packages whose failure cannot block
   the critical path (assets, fixtures, docs, tests).

*Expected effect: the Frogger brief becomes 1 author call + gates + 1
verify + 1 release ≈ **3–6 calls total**, versus 74 and 283.*

### Phase 2 — Repair-first release loop *(implemented 2026-07-18)*

5. **Verifier repair mandate.** On FAIL with a concrete fix, the verifier
   must ship the fix — including whole-file rewrites, not just `edit_file`
   patches. Applied repairs re-run the deterministic gates immediately;
   provenance records the repairing agent. Only defects the verifier
   cannot fix route to package reopen (the semantic mapping built this
   week stays as that fallback).
6. **Escalation ladder replaces pause-only breaker.** Streak 1: owner
   retries with the constraint stated. Streak 2: the package transfers to
   the strongest frontier seat. Streak 3: human, with the full critique.

### Phase 3 — Economics

7. **Per-goal call budget** (default configurable, e.g. 40): exceeding it
   pauses for human review with a cost report. No more silent 283s.
8. **Cost telemetry**: calls per seat per session recorded and shown in
   the dashboard goal header and `/goals` listing.

### Phase 4 — Solo fast path as the front door

9. Implement **solo mode** end-to-end: one frontier session authors the
   complete deliverable in its own context (no packages, no assembly),
   the existing gates verify (smoke → browser → style), one independent
   frontier verify, release. Benchmark: re-run the identical Frogger brief
   and publish calls/wall-clock/quality against goal 2's numbers.

### Phase 5 — Validation & migration

10. Keep team mode available and unchanged for real decomposable work; the
    two Frogger goals become regression benchmarks. Acceptance criterion
    for the overhaul: **the solo path ships a verified single-file game in
    ≤ 6 calls**, and team mode is never auto-selected for a single-file
    deliverable again.

## 6. Cut list (explicit)

- Best-of-N as a default behavior.
- Multi-judge blind voting on code where deterministic gates exist.
- Full-panel participation every round.
- Auto-appending every keyed OpenRouter seat to every panel.
- The policy comment forbidding tiering/benching of seats — replaced by
  triage policy (P1/P2).

## 7. Direct answers to the operator's questions

**"Should all 7 LLMs be called every job?"** No. Default is one frontier
author plus one independent frontier verifier. More seats only when the
work genuinely decomposes, or when the user explicitly wants candidate
diversity — and then at most 2–3 candidates, selected by gates.

**"Is the work distributed logically?"** No. It is distributed *socially* —
sized to give every seat a share — which manufactures interface seams, and
every observed failure in two days lived on a seam.

**"Why does it perform worse with 3 frontier models than with one?"**
Because the frontier models are spent supervising, verifying, and repairing
the seams and the budget seats' output instead of authoring. Coordination
cost rose faster than capability. The fix is not better coordination — it
is less mandatory coordination.
