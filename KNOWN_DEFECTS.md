# Gang of Eight — Confirmed Defects

## September 18 Escoffier PDF benchmark

The live benchmark (goal `g_05c762d2`, God mode, full council, Planned build)
released a verified 112-page PDF only after the defects below were fixed in
`b53fa7d`. Most share one shape: a rule existed but never reached the path
that needed it.

- **GO8-016 - Hot-standby failover never executes the standby**
  - Status: fixed on 2026-09-18.
  - After the primary build failed, `_try_next_candidate` swapped in Kimi's
    standby but re-armed the source write and BUILD as `captured`, a status
    `_execute_actions` skips. The standby never ran, and the failed producer
    was not sealed because its write was no longer `executed`.
  - Resolution: failover re-arms both as `proposed`; a prior approval still
    matches by action id.

- **GO8-017 - A complete repair in a `BEGIN ARTIFACT` envelope is discarded**
  - Status: fixed on 2026-09-18.
  - The recovery supervisor returned a 45KB generator as `BEGIN ARTIFACT
    <path>` ... `END ARTIFACT`. Only the BUILD line parsed, so the unchanged
    broken producer ran again. The repair prompt never showed the envelope.
  - Resolution: the parser accepts the upper-case variant with a space-free
    path; the repair prompt spells out `ARTIFACT:` / `END_ARTIFACT` and the
    surgical `EDIT` form.

- **GO8-018 - Owner retries receive the symptom, not the cause**
  - Status: fixed on 2026-09-18.
  - Recovery fingerprinted the causal `build_command_failed` record, but the
    owner's RETRY CORRECTION said only "no .pdf file was produced".
  - Resolution: the correction leads with up to two distinct causal records
    (traceback tail kept), then the symptom.

- **GO8-019 - Sealed failed producers are never restored (GO8-007 gap)**
  - Status: fixed on 2026-09-18.
  - `_preserve_failed_producer` sealed the bytes, but nothing materialized
    them. Every retry got an empty working set and re-authored an ~80KB
    generator from scratch.
  - Resolution: `_restore_failed_producer` copies the checkpoint into the new
    session sandbox; the correction names it as the unverified repair baseline.

- **GO8-020 - A restart loses the in-flight attempt and parks God mode**
  - Status: fixed on 2026-09-18.
  - A restart reconciled the package as cancelled: its producer and latest
    failure were dropped, and even a God-mode goal paused for manual resume.
  - Resolution: the cancelled branch preserves producer and failure; a
    restart-interrupted God-mode goal is rescheduled automatically.

- **GO8-021 - Session cancel leaves the real CLI running (665 s cancel)**
  - Status: fixed on 2026-09-18.
  - `_kill_procs` used `Popen.kill()`, which stops only the Windows `.cmd`
    launcher; the agent kept stdout open and `communicate()` blocked until it
    finished. Reproduced: still blocked after 45 s.
  - Resolution: session cancel uses `kill_tree`, like per-call cancel (0.7 s).

- **GO8-022 - A supervisor DELEGATE is ignored**
  - Status: fixed on 2026-09-18.
  - A supervisor that delegated a precise fix to the owner also echoed a BUILD
    line, so its reply was accepted as the repair and the delegation dropped;
    the named delegate was also cleared before its turn.
  - Resolution: a DELEGATE with no source change hands off, and the delegate
    is kept until its turn.

- **GO8-023 - Build repairs see a truncated producer and cannot EDIT**
  - Status: fixed on 2026-09-18.
  - The repair prompt cut the producer at 40KB (plus a duplicate scratch copy),
    so CLI seats without file tools saw half the generator; EDIT proposals were
    discarded.
  - Resolution: the complete latest producer is sent once (cap
    `GANGOF8_REPAIR_PRODUCER_MAX_CHARS`, labelled when exceeded) and EDIT is
    accepted.

- **GO8-024 - The approved interpreter is refused when quoted or `.exe`**
  - Status: fixed on 2026-09-18.
  - Build evidence prints `'C:\...\python.exe'`; a repair echoed it and the
    command check rejected it. Approved tools given as a full path also ran the
    typed path.
  - Resolution: one matching outer quote is stripped and `.exe` normalized;
    approved tools always resolve by name on PATH.

- **GO8-025 - Advisory release defects block a verified release**
  - Status: fixed on 2026-09-18.
  - A reviewer passed every check and ended `VERDICT: PASS` with two defects
    it labelled NON-BLOCKING and COSMETIC; every DEFECT was parsed as blocking
    and the PDF was sent back for a full rewrite.
  - Resolution: under a PASS with all checks passing, defects are advisory
    unless labelled BLOCKING; labelled advisory defects never block. The
    prompt asks reviewers to label severity.

- **GO8-026 - Release-review findings never reach the owner**
  - Status: fixed on 2026-09-18.
  - A rejected release reopened the owner with only "frontier final-batch
    verification failed".
  - Resolution: the retry text carries the reviewer's blocking findings.

- **GO8-027 - Operator `retry_verifier` strands a reopened package**
  - Status: fixed on 2026-09-18.
  - When the re-run review reopened the producer, the recover path never
    scheduled it; the goal sat `running` with no worker for 30 minutes.
  - Resolution: the recover path starts pending packages like `resume_goal`.

- **GO8-028 - A quota-exhausted release verifier fails the goal**
  - Status: fixed on 2026-09-18.
  - "You've hit your session limit" was not classified as quota, the only
    independent frontier seat was retried three times, and the God-mode goal
    failed while other seats were idle.
  - Resolution: the wording is classified as quota; a dead seat is tried once;
    the release falls back to any enabled non-author seat; a batch nobody can
    review pauses instead of failing.

- **GO8-029 - Document briefs never auto-route to Planned build**
  - Status: fixed on 2026-09-18.
  - "compile a ... PDF of 100 ... recipes" stayed focused: only software verbs
    counted as an action and a PDF was not treated as a delivered file.
  - Resolution: compile/produce/generate/assemble and named document formats
    count; a measurable quantity is still required.

- **GO8-030 - The release fallback never reaches seats off the panel**
  - Status: fixed on 2026-09-18.
  - Benchmark item 7 (goal `g_78f2c0bf`): the GO8-028 fallback drew only from
    the panel, which is `[claude, codex]` in duo mode. With Claude at its
    session limit and Codex the author, a healthy registered Gemini, which had
    just reviewed the package, was never asked, and the release paused.
  - Resolution: the fallback draws from every registered (enabled) seat, as
    the deliverable review already did; the regression test uses the duo panel.

- **GO8-031 - No deterministic check of index order; a misordered index shipped**
  - Status: fixed on 2026-09-18.
  - The released Escoffier PDF lists Béarnaise and Béchamel after Braised Ham
    (a raw code-point sort); two model reviewers passed it. Only a reviewer
    could have caught item 7's index sorted on the French title.
  - Resolution: `contract.index_order` checks a PDF's alphabetical index
    (letter headings ascending, entries under their letter, entries in order,
    word-by-word or letter-by-letter, accent-folded). It runs when the contract
    or the document calls the index alphabetical, and ignores a category index.

- **GO8-032 - The mother-sauce order check uses a fixed order and prose**
  - Status: fixed on 2026-09-18.
  - The required order was hard-coded, so a contract naming all five would
    reject Escoffier's own order (tomato before hollandaise), and the first
    mention anywhere counted, so an introduction's "stocks, veloutés" came
    first.
  - Resolution: the order is the one the contract names them in, measured from
    the first mention of the first sauce.

- **GO8-033 - "agree" with nothing pending blames the user's choice**
  - Status: fixed on 2026-09-18.
  - With nothing waiting, the refusal said the acknowledgment "does not
    identify exactly one pending action", as if one had to be picked.
  - Resolution: separate messages for nothing waiting and several waiting.

## August 24 goal-release reconciliation incident

- **GO8-014 - A completed council session is labeled as a successful goal**
  - Status: fixed on 2026-08-24.
  - Session `s_20260824_479b9eaf` completed its model work while parent goal
    `g_f333455f` had not completed its release transaction. The detail panel
    nevertheless said “This run finished successfully.”
  - Resolution: dashboard status is derived from both lifecycles. A goal-linked
    turn is called successful only when the parent goal is `completed` and its
    verified release is `released`; council completion and delivery are shown
    separately.

- **GO8-015 - God mode releases before its durable goal link exists**
  - Status: fixed on 2026-08-24.
  - The package build and release review passed, but synchronous God-mode
    approval called final promotion before `release_session_id` was persisted.
    Reloading the stale goal raised `final-batch release state is incomplete`,
    and the exception handler left the goal falsely running with no worker.
  - Resolution: release linkage and phase are persisted before verification and
    authorization; auto-release operates on the linked leased goal. A verified
    interrupted release resumes its deterministic promotion without another
    reviewer call. Coordinator transition exceptions receive one bounded replay
    and then become an explicit paused/failed state.

## August 24 verified-PDF incident

- **GO8-009 - Markdown PASS is parsed as artifact failure**
  - Status: fixed in the working tree on 2026-08-24.
  - A reviewer returned `**CHECK R1: PASS**` and `**VERDICT: PASS**`; closing
    emphasis made every check unparsable and the controller invented a failure.
  - Resolution: one canonical criterion set, Markdown-tolerant parsing, and a
    typed protocol-invalid state that cannot change artifact validity.

- **GO8-010 - Semantic review destroys an objectively verified package**
  - Status: fixed in the working tree on 2026-08-24.
  - Resolution: immutable content-addressed checkpoints remain active during
    review and repair. Only a typed blocking defect mapped to a producing source
    may open a repair branch.

- **GO8-011 - Repair replays Full Council and exceeds the goal budget**
  - Status: fixed in the working tree on 2026-08-24.
  - Resolution: participation is baseline-scoped and reused on targeted repair;
    every physical dispatch reserves budget atomically before provider contact.

- **GO8-012 - Failed producer overwrites the last-good stage**
  - Status: fixed in the working tree on 2026-08-24.
  - Resolution: failed candidates are sealed separately; staging is never the
    canonical checkpoint and a repair cannot replace verified bytes before it
    passes objective validation.

- **GO8-013 - Model response order runs BUILD before source creation**
  - Status: fixed in the working tree on 2026-08-24.
  - Resolution: the coordinator executes a stable source-to-stage-to-install-to-
    build-to-validate-to-release dependency order.

## Recovery and collaboration

- **GO8-001 — Collaboration context resolution stops too early**
  - Status: fixed in the working tree on 2026-08-23.
  - Confirmed in session `s_20260823_15d2374f` on 2026-08-23.
  - Gemini requested another safe project search after receiving a search result. The collaboration path resolved only one request, then treated the next `SKILL:` request as a malformed final review. An identical retry repeated the failure.
  - Required behavior: continue resolving successive authorized context requests until the model returns the required final review, reaches a bounded tool-iteration limit, or produces a genuine error. A context request is an intermediate state, not a protocol failure.

- **GO8-002 — Empty-final OpenRouter recovery is a blind identical retry**
  - Status: fixed in the working tree on 2026-08-23.
  - Confirmed for Qwen in session `s_20260823_15d2374f` on 2026-08-23.
  - OpenRouter streamed extensive reasoning but returned no final `content`. The application discarded the reasoning and repeated the same model/prompt without adapting the response budget, prompting, or provider/model route.
  - Required behavior: record the provider finish reason, distinguish reasoning-only exhaustion from a genuinely empty response, and retry with an adaptive final-answer constraint or healthy fallback route.

- **GO8-003 — Primary build failure is hidden by a secondary missing-output error**
  - Status: fixed in the working tree on 2026-08-23.
  - Confirmed in session `s_20260823_15d2374f`: the producer invoked a nonexistent local Python source, but the durable record emphasized only the absent PDF.
  - Resolution: build, test, and dependency-install failures are recorded immediately with command evidence; recovery prefers the causal execution record over downstream symptoms.

- **GO8-004 — Repair retries the same owner without executable evidence**
  - Status: fixed in the working tree on 2026-08-23.
  - Confirmed when Codex was called for repair under a non-authoring role and timed out after 1,800 seconds without producing replacement source.
  - Resolution: a bounded recovery supervisor receives the exact causal failure, may repair directly or delegate, and reseats work to distinct healthy agents. Identical prior attempts are rejected across the full repair history.

- **GO8-005 — Reviews create apparent redundancy but no executable fallback**
  - Status: fixed in the working tree on 2026-08-23.
  - Five peers reviewed one unexecuted producer; none produced an independent buildable alternative.
  - Resolution: the implementation-lens council seat emits a complete hot-standby
    candidate during its already-required participation call. The coordinator
    retains ranked candidates and can execute the next candidate without buying
    a separate speculative shadow-producer call.

- **GO8-006 — Research has a single-provider failure point**
  - Status: fixed in the working tree on 2026-08-23.
  - Gemini/Google web lookup failures, including transient DNS failures, could terminate the goal despite already retrieved evidence or an available public provider.
  - Resolution: verified research is cached and reused; transient errors are recoverable; public web search is a bounded provider fallback with auditable source URLs.

- **GO8-007 — Goal recovery restarts without the failed producer**
  - Status: fixed in the working tree on 2026-08-23.
  - Resolution: exact failed producer bytes and hashes are sealed in a separate,
    explicitly unverified candidate checkpoint for the next owner. Failed bytes
    never overwrite staging or the active verified checkpoint.

- **GO8-008 — Council seats have no persistent operating instructions or memory**
  - Status: fixed in the working tree on 2026-08-23.
  - Resolution: all eight council identities have packaged `AGENT.md` and `MEMORY.md` profiles. Runtime copies are seeded under `data/seats/<seat>/` and injected into every registry call; verified recovery lessons append to seat memory.
