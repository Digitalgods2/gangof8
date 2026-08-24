# Gang of Eight — Confirmed Defects

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
