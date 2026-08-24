# Gang of Eight Orchestration and Recovery Plan

Status: Implemented and superseded by the August 24 checkpoint/controller cutover

## August 24 release-state follow-up

The later Escoffier run exposed two controller defects after its PDF had already
passed objective validation and independent release review. First, the dashboard
reported a successful council session as if the parent goal and artifact release
had succeeded. Second, God-mode approval reloaded the goal before the new release
session link had been persisted, raised `final-batch release state is incomplete`,
and left the goal falsely running without an active worker.

The repaired path now:

- reports council-session completion separately from parent-goal delivery and
  only labels the goal successful after a verified release is committed;
- persists the release-session link before synchronous God-mode authorization;
- continues the final release against the same leased goal object instead of a
  stale store reload;
- recognizes a prior passing release review only when every staged SHA-256 hash
  still matches, then resumes promotion without another model call;
- replays a failed terminal coordinator transition once without a model call and
  converts a repeated identical fault into an explicit paused/failed state; and
- reconciles terminal package sessions and dependency-ready packages after a
  restart instead of leaving a live goal with no worker.

## August 24 controller cutover

The 52-attempt Escoffier run proved that the August 23 recovery work was not
enough. A 117-page PDF passed strict objective validation, then a release
reviewer returned bold Markdown `PASS` checks. The parser rejected the closing
bold markers, converted the response into a failure, invalidated the successful
package, and replayed the complete council. The replacement attempt then failed.

The controller now closes that entire class of failure:

- one frozen criterion list drives both the reviewer prompt and enforcement;
- the Markdown fallback parser accepts closing emphasis, while review state is
  typed as pass, blocking fail, nonblocking, protocol invalid, or unavailable;
- protocol/transport errors retry only the reviewer and cannot demote an
  objectively verified artifact;
- verified files are copied into immutable SHA-256 blobs with durable
  checkpoint manifests; working staging is only a projection;
- failed producer candidates receive their own checkpoint and never overwrite
  the active verified stage;
- source/edit actions execute before builds regardless of response order;
- Full Council participation is persisted against its baseline and reused by a
  targeted repair, which disables shadow/full-council replay;
- the implementation-lens seat supplies the independent standby during its
  already-required council call, removing a separate duplicate model call;
- every physical provider dispatch atomically consumes the goal budget before
  dispatch and is never refunded for timeout, transport, or protocol failure;
- provider calls default to a 15-minute absolute ceiling and complete packages
  to 45 minutes, in addition to output-stall detection and cancellation;
- package phase/work-item state, owner baseline actions, completed council
  reports, and build recipes survive restart and resume in a clean worker;
- the dashboard separates objective verification from AI review and shows the
  active checkpoint and controller phase.

The intended nominal single-artifact Full Council path is now seven initial
seat calls (owner plus six distinct resource lenses), one owner integration,
and one independent release review. A local defect adds a targeted producer
repair and confirmation, not another seven-seat production cycle.

## August 23 final recovery implementation

The later `s_20260823_15d2374f` audit exposed a full causal chain that the
earlier plan had not closed: an undeclared local source dependency reached a
build, reviewers never executed it, only the original owner was asked to
repair, that call used the wrong role and timed out, and a restarted goal then
died on a transient Gemini research failure. Qwen and Gemini collaboration
errors also used identical retries instead of changed strategies.

The production path now closes that chain:

- producer source is dependency-checked and preflighted before the peer-review
  wave spends calls;
- the implementation-lens council seat authors an independently executable hot
  standby during its required baseline review, and ranked candidates remain
  available after build failure without a separate speculative call;
- build/test/install failures are first-class causal records with exact command
  evidence, while missing output remains a secondary symptom;
- an event-driven recovery-supervisor role diagnoses each real failure, repairs
  or delegates to a distinct capable model, verifies the result, records the
  decision, and stops after a finite candidate set;
- failed producer bytes survive goal restart as a separate unverified,
  hash-recorded recovery-source checkpoint and never replace verified staging;
- Gemini can complete bounded multi-step context requests, Qwen reasoning-only
  exhaustion is diagnosed explicitly, and collaboration falls back to a
  distinct healthy seat after adaptive retry;
- verified research evidence is reused and public web retrieval provides a
  second provider route for transient Gemini/Google failures;
- all physical council seats (`system`, `claude`, `codex`, `gemini`,
  `deepseek`, `glm`, `qwen`, and `kimi`) have persistent `AGENT.md` and
  `MEMORY.md` profiles injected into every call. Only verified recovery lessons
  are added to memory.

This recovery loop is task-type neutral: observe, choose a changed action,
execute, verify deterministically, record evidence, repeat within a finite
budget, or stop with the causal diagnostic. Approval policy controls side
effects; it no longer prevents diagnosis or automatic in-scope recovery.

Implementation result: the coordinator now has a run-scoped manual/God-mode
policy, deterministic one-artifact planning, persisted materialization recipes,
full isolated author working sets, binary build contracts, strict pre-LLM format
validation, hash-sealed lineage/checkpoints, structured failure and defect
records, changed-retry enforcement, bounded owner/frontier recovery, strict
research provenance, global acknowledgement routing, finite buffered-call
deadlines, operator recovery controls, and useful-work/recovery economics.

The existing malformed-artifact and build-path checks were exercised with the
targeted repository smoke suite after implementation. Per the operator's
direction, the historical Escoffier process itself was not resumed or rerun.

The August 23 audit of the later Escoffier test run (`g_6c013334`) found and
closed a final orchestration defect: the coordinator attached
`package_author` provenance to a valid `build_artifact` action while the live
capability contract rejected that coordinator-owned field. Recovery then
misclassified the product bug as an author defect and bought more author calls.
Capability-contract failures now stop at the coordinator layer with their exact
diagnostic, cannot trigger model-authored repair, and deduplicate by fault
signature. The build handler also copies every immutable dependency into the
actual execution sandbox only after verifying its accepted SHA-256, so the
producer and its inputs can no longer land in different directories.

The 44-minute August 23 rerun (`s_20260823_31ba4900`) exposed a separate intake
and recovery regression. Although the operator had selected Planned build on a
prior run, the composer reset Execution to Auto after submit; the frozen record
therefore shows `profile=auto`, `route=focused`, and manual approval. Focused
mode launched research and PDF generation concurrently, received a useful
57,193-byte research dossier, received no generator from Kimi, then waited 30
minutes for a Codex failover that produced no observable output. At the
wall-time gate it waited another 17 minutes for human continuation. Verification
then correctly diagnosed the missing `deliverable.pdf`, but recovery crashed on
`record_failure() got multiple values for argument 'owner'`.

Those defects are now closed. Execution selection persists across submissions;
the exact natural-language quantity phrase “100 of his most notable and popular
recipes” routes to Planned build; an unnamed single-format artifact receives the
canonical path `deliverable.pdf` and bypasses the planning model; one document
gets one accountable authoring package and no arbitrary recipe-range research
fanout; missing-output failures enter the recovery ledger without crashing; and
unexpected coordinator exceptions retain their exact traceback and failure
layer. The live service was restarted and its read-only preview of the exact
prompt returned `selected_route=build_team`, `auto_routed=true`.

This plan is grounded in the August 22 Escoffier run (`g_844c7ae4`) and the
existing architecture overhaul. The earlier principles remain correct: size
the package graph to the artifact while the enabled council participates, let
deterministic gates decide objective facts, repair at the point of detection,
and escalate instead of repeating. The new work is to make
those principles apply to every artifact type, especially content-heavy binary
deliverables.

## Outcome

Gang of Eight must be able to give one or more frontier agents the complete,
authoritative working set; materialize the requested output through the correct
tool path; diagnose failures with evidence; and resume from the last verified
checkpoint with a bounded, changed repair strategy.

For Planned builds, the system assigns every healthy enabled model a distinct
lens against the real baseline. It must not use extra models to compensate for
inputs, tools, or state that the coordinator withheld from the primary author,
and it never buys duplicate full rewrites merely to create activity.

## Confirmed failure modes

The Escoffier run exposed these general defects:

1. A single binary release file was routed through a package prompt that
   demanded a complete text `ARTIFACT` block.
2. The package author ran in an empty directory and received only an 80,000
   character excerpt of roughly 326,000 characters of accepted dependencies.
3. Research packages completed without web lookup despite an explicit heavy
   research requirement.
4. A failed build command could be recorded as producing a pre-existing output.
5. Binary integrity was deferred to LLM reviewers instead of enforced before
   semantic review.
6. Release repair invalidated the entire package and asked for the complete
   deliverable again instead of preserving and repairing its producing source.
7. Failure feedback was stored mainly as prose, duplicated in retry prompts,
   and was not tied to an artifact hash, validator, or repair strategy.
8. A one-word conversational response such as `agree` could start new council
   work rather than resolve the pending goal action.
9. Buffered CLI calls showed no real progress, had no default hard deadline,
   and could occupy the critical path indefinitely.
10. Clearing the composer silently reset Planned build to Auto, so the UI did
    not preserve the operator's execution choice.
11. Auto routing recognized only an adjacent phrase such as “100 recipes,” not
    the natural equivalent “100 of his most notable and popular recipes.”
12. A generic intake label such as “requested polished content artifact” hid a
    known PDF format from deterministic one-artifact planning and needlessly
    invoked an architect.
13. The planning prompt explicitly encouraged splitting large documents into
    numeric content ranges across models, creating cost without a necessary
    artifact boundary.
14. The missing-deliverable verifier passed `owner` both positionally and by
    keyword to the recovery ledger, turning a diagnosed output defect into an
    orchestrator crash.

## Non-negotiable invariants

1. **Representable output:** no model is asked to type bytes that its response
   channel cannot faithfully represent. Binary output always comes from a
   persisted build or transform recipe.
2. **Complete inputs:** an author or repairer sees every authoritative input as
   a real file in an isolated working directory. Prompt truncation is never an
   input-delivery mechanism.
3. **Last-good checkpoint:** accepted inputs, producing sources, build recipe,
   output hashes, validation evidence, and open defects survive every retry and
   restart.
4. **Evidence before inference:** objective failures are produced by
   deterministic validators before any reviewer call.
5. **Successful build means success:** a build passes only when its process
   exits zero, every declared output is new or changed as expected, and every
   format validator passes.
6. **Repair the producer:** a defect in a generated artifact routes to its
   generator/data/build recipe, not to hand-editing or regenerating opaque
   binary bytes.
7. **Changed retry:** the same fault signature may not run again with the same
   owner, input hashes, prompt/strategy, and build recipe. A retry must change a
   relevant variable or escalate.
8. **Bounded escalation:** owner repair, frontier takeover, then human review.
   Transport outages and coordinator defects do not count against an author.
9. **Independent verification:** the semantic reviewer is not the original
   author and receives the exact verified output plus the evidence bundle.
10. **Conversation continuity:** confirmations and corrections attach to the
    active goal/checkpoint; they do not silently create unrelated sessions.
11. **Autonomy without blindness:** God mode removes human approval and
    continuation pauses for the selected run, but it never disables scope,
    containment, validation, audit, cancellation, or finite stopping rules.
12. **Gang means eight, independent of task type:** every healthy enabled model
    receives a useful, non-overlapping role in every Planned goal. Code, prose,
    research, design, data, and binary/document generation use the same
    participation logic. One accountable owner integrates final bytes; ownership
    may never be used as a reason to reduce the gang to one active model.

## Target architecture

### 1. Materialization plan

Add a persisted materialization plan to each output-producing milestone. It
replaces filename-extension guessing and selects one of four explicit modes:

- `text`: model-authored text files captured directly or imported from an
  isolated author workspace.
- `build`: text source/data plus a governed command producing binary or derived
  files.
- `transform`: coordinator-owned deterministic assembly/copy/conversion.
- `revision`: repair of an existing source artifact followed by rebuild or
  revalidation.

The plan records:

- authoritative input paths and hashes;
- private producing files such as `make_book.py`;
- user-facing release files;
- command, working directory, environment, and declared outputs;
- validator IDs and contract-derived assertions;
- lineage from each release file back to its producing files and inputs.

Private producing files remain in goal staging but are excluded from
`release_files`. A request for one PDF may therefore legitimately keep a
generator and canonical data internally while releasing only the PDF.

Planning validation must reject:

- a binary `required_file` with no build or transform recipe;
- an `ARTIFACT` assignment whose path is binary;
- a build recipe whose inputs are absent or unowned;
- a package that cannot access all hard dependencies;
- a research package without an enabled source-retrieval capability when the
  outcome contract requires research or citations.

For obvious single-artifact jobs, derive the one-package materialization plan
deterministically from the outcome contract. Do not spend a planner call unless
natural package boundaries actually need model judgment.

### 2. Isolated agent working sets

Replace large inline dependency injection with a per-attempt working set:

- seed exact accepted dependencies and producing sources into a disposable
  directory;
- expose a manifest containing paths, hashes, ownership, and read/write policy;
- allow native read/search tools against that directory;
- for author/repair roles, permit writes only inside the disposable directory;
- capture the final diff and import only declared paths into the governed
  session sandbox;
- never mount the established project as writable;
- keep network access separately capability-gated and auditable.

The existing neutral-directory quarantine remains the fallback for unsupported
CLIs. It should no longer be the primary authoring workflow. Marker-based text
artifacts remain a compatibility path for API models that cannot operate on a
workspace.

### 3. Structured failure and defect ledger

Introduce durable `FailureRecord`, `Defect`, and `RepairAttempt` records rather
than passing unstructured `last_error` strings between subsystems.

A failure record contains:

- stable ID and normalized fault signature;
- stage: planning, input, authoring, build, validation, semantic review,
  governance, transport, or orchestration;
- class: unavailable capability, missing/stale input, command failure, malformed
  protocol, invalid artifact, contract violation, semantic defect, or internal
  invariant failure;
- artifact and producing-source paths;
- observed and expected hashes;
- validator/command, exit code, bounded stdout/stderr, and reproduction steps;
- severity and whether it blocks release;
- accountable owner only when evidence supports ownership;
- proposed repair scope and current resolution state.

Defects are deduplicated by signature. Their evidence is append-only. Retry
prompts reference defect IDs once and include only current evidence, not copied
and duplicated prose.

### 4. Recovery controller

Replace ad hoc package reopen branches with one persisted recovery state
machine:

1. `diagnose`: classify the failure and reproduce deterministic failures.
2. `select`: choose the narrowest valid repair target and capable seat.
3. `prepare`: materialize the last-good checkpoint into an isolated repair
   workspace.
4. `apply`: edit producing text/data/config or the build recipe.
5. `verify`: rebuild if needed, run deterministic gates, then semantic review.
6. `close`: seal new hashes, resolve defects, and continue release.
7. `escalate`: transfer to the strongest eligible frontier seat or pause for the
   operator with the evidence bundle.

Recovery policy by failure class:

| Failure class | Recovery |
|---|---|
| Transport/capacity | retry or rotate the seat; do not blame or rebuild |
| Missing capability/input | repair orchestration or request the missing capability; no author call |
| Build command failure | preserve sources; return exact command, exit code, and stderr to the owner |
| Invalid generated artifact | repair its generator/data/build recipe, then rebuild |
| Text/source validation | apply a surgical edit to the current bytes |
| Semantic defect with known fix | verifier or owner applies the fix at the producing source |
| Semantic defect without known fix | owner repair, then frontier takeover |
| Internal invariant failure | stop the goal and surface a product bug; never spend another model call |

Every repair attempt stores before/after hashes, chosen strategy, seat, result,
and validator evidence. The controller refuses an identical attempt tuple of
`fault signature + owner + input hashes + strategy + build recipe`.

### 5. Deterministic validation registry

Make validation format-aware and structured. `validation.run` must return a
result object containing argv, return code, timing, stdout, and stderr; non-zero
is always failure.

`build_artifact` must snapshot declared outputs before execution and require:

- exit code zero;
- each output to be created or changed by this build, unless the recipe
  explicitly declares a cache hit and proves the expected hash;
- a non-empty output;
- successful format validation;
- recorded output hashes and source lineage.

Add validators behind a registry. The initial PDF validator should check:

- header, EOF, cross-reference/trailer consistency, and strict parser success;
- non-zero page count and extractable text;
- required metadata;
- outline/bookmark presence when requested;
- contract assertions such as expected recipe IDs/count, mother-sauce ordering,
  forbidden placeholders, and index coverage;
- optional rendered-page inspection for layout evidence.

Only after deterministic validation passes should a frontier reviewer assess
editorial quality, factual quality, usability, and visual polish.

### 6. Useful multi-model collaboration

Multiple seats should share the same artifact graph and contribute distinct
work:

- researchers produce canonical records with source URLs, quotations bounded by
  policy, and claim-level provenance;
- the accountable author owns the producing sources and final materialization;
- peer models inspect the exact baseline and return findings or patches against
  producing text files, not rival complete binaries;
- the fact validator checks claims against provenance;
- the release engineer reviews the deterministic evidence and rendered result;
- the coordinator resolves ownership, scheduling, and state without rewriting
  model output.

Extend the existing artifact-aware collaboration wave beyond code packages.
Content and document packages need format-specific review views: canonical
records, extracted PDF text, metadata, page renders, and build evidence.

Full-council mode schedules all healthy enabled seats, each with a named,
non-duplicative assignment. The coordinator derives an appropriate lens from
the artifact and its acceptance contract. Seven complete rewrites of the same
artifact are not collaboration.

### 7. Conversation and operator feedback

Before submitting a follow-up as a new task, resolve it against pending state:

- `yes`, `agree`, `continue`, `resume`, and `approve` act on the pending goal,
  approval, or input request;
- corrective language creates a revision child anchored to exact delivered or
  verified artifacts and their producing sources;
- ambiguous acknowledgments produce a cheap UI confirmation, not a model run;
- a goal-aware follow-up never enters an ordinary council route by default.

The dashboard should show:

- current recovery state and defect ID;
- last-good checkpoint and changed files;
- build command/result and deterministic validator evidence;
- why a specific seat was selected;
- attempt number and what changed from the prior attempt;
- elapsed time, hard deadline, last real progress, and stop/takeover controls.

Use streaming/event output where the backend supports it. Buffered backends
still need a finite hard deadline and explicit `no observable progress` state;
`progress_chars = 0` must not be presented as healthy activity.

### 8. God mode: run-scoped autonomous approvals

Add a `God mode` checkbox beside the existing `Advanced` checkbox in the chat
composer. Checking it is explicit advance authorization for Gang of Eight to
approve and execute every otherwise approval-gated action that is both needed
by the submitted outcome contract and allowed by the runtime. It is not a
request to skip verification.

The policy is deliberately run-scoped:

- default it to off for every new submission;
- snapshot `approval_policy: manual | god_mode` into the new goal and its root
  session at submit time;
- inherit that snapshot in all child sessions, repair attempts, restarts, and
  release actions belonging to the goal;
- do not save it as a browser preference, playbook default, or imported value;
- show an always-visible warning badge on an active God-mode goal and keep a
  normal `Stop` control available.

The checkbox itself is the authorization. Do not add another confirmation
modal after it is selected. Its inline explanation should say: `Automatically
approve in-scope actions for this run. Safety checks and validation still
apply; missing information may still require you.`

When God mode is active:

1. Governance still creates the same exact, risk-classified approval record,
   but immediately resolves it as approved with `resolved_by: god_mode`, the
   goal/run policy, timestamp, action arguments, target, and artifact hashes.
2. The caller executes the action directly instead of transitioning the
   session or goal to `awaiting_approval`.
3. Install, build, test, workspace-write, stage, promote, retry, takeover, and
   release actions may auto-approve when they are inside the frozen outcome
   contract. Final promotion may proceed once the destination is explicit and
   the approved action is bound to the validated artifact hashes.
4. Round-consent prompts, `continue` prompts, and recoverable resume decisions
   become controller decisions. The controller advances through owner repair,
   changed retry, independent confirmation, and frontier takeover without
   waiting for a human semaphore.
5. Every automatic approval and continuation appears in the event log and UI,
   including what was authorized, why it was in scope, which evidence gate ran,
   and the result.

God mode bypasses approval waits, not enforcement. The following remain hard
runtime rules:

- path and workspace containment;
- parsed command/capability allowlists and shell-metacharacter rejection;
- the frozen outcome contract and explicit delivery root;
- output hashes, successful build exit, changed-output checks, and all format
  and contract validators;
- recovery budgets, no-progress detection, changed-retry rules, cancellation,
  and terminal failure states;
- provenance and independent verification requirements.

An action outside the frozen outcome contract is a scope violation, not a new
approval prompt. Missing credentials, an unspecified delivery destination, or
genuinely missing task information may transition to `awaiting_input`; God mode
must not invent the missing value. Exhausted budgets, repeated fault signatures,
or a capability gap end in an evidenced `manual_intervention_required` terminal
state instead of repeatedly asking to continue or silently expanding the run.

This preserves the useful meaning of human control: the operator authorizes an
autonomous run once, can observe or stop it at any time, and receives a complete
audit trail. The operator does not have to babysit each mechanical step.

## Implementation sequence

### Phase 0: Stop known bad paths

Production changes:

1. Make command execution return structured status and make non-zero build exit
   unconditionally fail.
2. Require declared build outputs to change and pass a validator before they are
   recorded as produced.
3. Reject binary `ARTIFACT` blocks in package adoption.
4. Route binary package authoring through generator/build instructions instead
   of `package_output_prompt`'s complete-file envelope.
5. Add a strict PDF structural validator.
6. Prevent acknowledgments such as `agree` from creating a new ordinary run
   when a goal has a pending action.
7. Add finite author deadlines and honest buffered-call liveness, incorporating
   the existing requirements in `DEVELOPMENT.md`.
8. Add `approval_policy` to submission, goal, and session state; implement the
   governance auto-resolution path so God-mode runs never enter
   `awaiting_approval`.
9. Add the run-scoped `God mode` checkbox beside `Advanced`, default off, with
   its inline warning and active-goal badge.

Acceptance gate: the existing malformed Escoffier PDF and the exit-2 build are
rejected before any LLM reviewer call. With God mode selected, every in-scope
approval is audit-recorded and resolved without a human pause.

### Phase 1: Persist materialization and failure state

Production changes:

1. Add materialization, build recipe, lineage, failure, defect, and repair models.
2. Bump session/goal schema versions with backward-compatible migrations.
3. Convert current `release_defects`, `acceptance_detail`, and quality-gate data
   into compatibility views backed by structured records.
4. Seal last-good checkpoints after package acceptance and successful build.
5. Persist the root goal's immutable approval-policy snapshot and inherit it in
   every child or resumed session.

Acceptance gate: restart the service at every recovery state and resume without
losing accepted inputs, source files, defect evidence, or attempt counts.

### Phase 2: Give authors real working sets

Production changes:

1. Build and hash per-attempt workspaces containing complete dependencies.
2. Add role-aware CLI modes: read-only reviewer and isolated workspace-write
   author/repairer.
3. Capture and govern declared workspace diffs.
4. Remove dependency bodies from author prompts; provide a manifest and concise
   contract instead.

Acceptance gate: the Escoffier integrator can read all four JSON files directly,
and no accepted dependency is truncated into the prompt.

### Phase 3: Unified recovery controller

Production changes:

1. Implement failure classification and fault signatures.
2. Replace direct-owner/assembly-specific reopen branches with adapters into the
   recovery controller.
3. Repair producing sources in place and rebuild only affected outputs.
4. Enforce changed-retry and bounded escalation rules.
5. Keep release review read-only. Map a blocking defect to its accountable
   producing source, rebuild through the owner/supervisor path, and rerun all
   gates against a newly sealed checkpoint.
6. In God mode, auto-advance every recoverable transition without round consent;
   terminate with evidence when recovery is exhausted instead of asking whether
   to continue.

Acceptance gate: inject a broken PDF xref and one index-order defect; recovery
patches the generator or canonical data, rebuilds once, and closes both defects
without regenerating research packages or hand-authoring PDF bytes.

### Phase 4: Capability-aware routing and research truth

Production changes:

1. Deterministically create a solo materialization plan for obvious
   single-artifact goals.
2. Treat user profiles as collaboration preferences, not permission to violate
   natural artifact boundaries.
3. Select seats by required capability, health, context/tool access, and prior
   attempt history.
4. Refuse to label recall-only generation as research; require retrieval and
   provenance or obtain explicit scope downgrade.
5. Generalize artifact-aware peer collaboration to content/document work.

Acceptance gate: the original Escoffier brief either runs with real research
capability and claim provenance or pauses before spending author calls with an
explicit capability gap.

### Phase 5: Operator experience and economics

Production changes:

1. Stream call/build/validation progress into the existing timeline.
2. Add defect, checkpoint, repair, and escalation cards to the goal view.
3. Show calls and wall time by useful work, retry, review, transport, and
   orchestration overhead.
4. Add one-click `retry verifier`, `repair with owner`, `frontier takeover`, and
   `stop` controls, each acting on the persisted recovery state.
5. Show whether the run uses manual approval or God mode, and distinguish
   auto-approved actions from actions that required no approval.

Acceptance gate: an operator can explain exactly what is running, what it is
waiting for, what changed since the last attempt, and how to stop or redirect it
without reading JSONL logs.

## Escoffier replay benchmark

Use the same outcome contract and four accepted recipe datasets as the primary
end-to-end benchmark.

Required results:

1. No model response contains raw PDF bytes or PDF object syntax.
2. The integrator reads all four datasets from its working set.
3. Every research claim is marked retrieved/provenanced or explicitly
   recall-only; a heavy-research run cannot silently be recall-only.
4. The PDF is generated by a recorded build recipe.
5. A non-zero build exit cannot pass because an older PDF exists.
6. Strict PDF validation, text extraction, metadata, bookmarks, 100 unique IDs,
   mother-sauce ordering, placeholder rejection, and index checks pass before
   semantic review.
7. Injected xref and index defects recover from the existing generator/data in
   no more than two additional model calls: one repair and one independent
   confirmation.
8. A server restart after any checkpoint resumes without repeating completed
   research or losing the generator.
9. Replying `agree` to a pending action creates zero new council sessions.
10. Target economics after research inputs exist: one author call, deterministic
    build/validation, one semantic review, and at most one confirmation after a
    repair. Starting from scratch keeps research with that accountable author by
    default and may add at most one independently checkpointed source corpus,
    but never arbitrary range-based fanout or duplicate full-artifact generations.
11. With God mode enabled, the complete build, validation, repair, confirmation,
    and promotion path contains zero `awaiting_approval` or round-consent pauses;
    every automatically authorized action remains visible in the audit trail.
12. A missing release destination still produces one precise input request, and
    an out-of-contract action is blocked rather than auto-approved.

## Production file map

- `gangof8/models.py`: durable materialization, lineage, defect, repair, and
  approval-policy models.
- `gangof8/sessions.py`, `gangof8/goals.py`: migration, planning contracts, and
  checkpoint and run-policy persistence.
- `gangof8/governance.py`: auditable God-mode auto-resolution without weakening
  capability, scope, or containment checks.
- `gangof8/main.py`: accept the run policy in submit/preflight APIs and expose
  it in goal/session views.
- `gangof8/rounds.py`: mode-specific author/reviewer/repair prompts; remove
  contradictory binary artifact instructions.
- `gangof8/adapters/cli.py`: role-aware isolated working directories, writes,
  and streaming/liveness.
- `gangof8/loop.py`: package materialization, validator dispatch, and recovery
  controller integration.
- `gangof8/validation.py`: structured command result and build success rules.
- `gangof8/skills.py`: governed build execution and output lineage.
- `gangof8/service.py`: goal checkpoint/recovery orchestration, goal-aware
  follow-ups, and autonomous recover-or-stop transitions.
- `gangof8/workbench.py`: artifact/lineage manifest views.
- `gangof8/reporting.py`: useful-work and recovery economics.
- `gangof8/static/index.html`, `app.js`, `dashboard-utils.js`, `app.css`: the
  God-mode submit control, active-run warning, recovery, approval audit, and
  progress UI.

The current uncommitted changes in the worktree are preserved. They mostly
address cleanup, persistence robustness, and one package filename-contract
regression; implementation of this plan should rebase around them rather than
overwrite them.
