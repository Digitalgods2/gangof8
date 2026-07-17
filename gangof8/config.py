"""Configuration: budgets, role→agent assignments, approval categories."""

from __future__ import annotations

import math
import os
import tempfile
from pathlib import Path

from .models import Budgets, Complexity, Risk, Role

DATA_DIR = Path(os.environ.get("GANGOF8_DATA", str(Path(__file__).resolve().parent.parent / "data")))

# The dashboard can read local files, reveal locally stored API keys, and ask
# the OS to open delivered files. It is therefore localhost-only by default.
# Set GANGOF8_ALLOW_REMOTE=1 only behind an authenticated reverse proxy.
ALLOW_REMOTE_ENV = "GANGOF8_ALLOW_REMOTE"


def _default_sandbox_root() -> Path:
    """A NEUTRAL scratch location, deliberately OUTSIDE any project/source folder
    so the ephemeral sandbox can never sit inside (or corrupt) source material.
    Windows → %LOCALAPPDATA%\\GangOf8\\sandbox; else a temp-dir subfolder.
    Override with GANGOF8_SANDBOX."""
    base = os.environ.get("LOCALAPPDATA") if os.name == "nt" else None
    root = Path(base) / "GangOf8" if base else Path(tempfile.gettempdir()) / "gangof8"
    return root / "sandbox"


# The ephemeral per-session sandbox lives here — NEVER under DATA_DIR or any
# project folder. Each session gets its own subdir (executor.artifacts_dir).
SANDBOX_ROOT = Path(os.environ.get("GANGOF8_SANDBOX", str(_default_sandbox_root())))
# Sandbox folders are scratch, but pile up (one per session, forever). A sweep at
# each session start keeps this many most-recent NON-active sandboxes (so recent
# runs can still be opened/inspected) plus every still-active/paused one, and
# deletes the rest.
SANDBOX_KEEP = int(os.environ.get("GANGOF8_SANDBOX_KEEP", "25"))

# A panel round costs len(panel)+1 calls (every seat + the lead synthesis), so
# the call budgets carry real multi-round headroom. The terminators are
# ROUND: DONE, a declined consent, max_agent_calls, and wall time. Delegation
# depth/fan-out scale with complexity: a trivial question stays flat and fast;
# a complex build earns a lead → specialist → sub-agent tree.
BUDGETS_BY_COMPLEXITY: dict[Complexity, Budgets] = {
    Complexity.trivial: Budgets(max_agent_calls=10, max_wall_seconds=420,
                                max_delegation_depth=1, max_delegations=2),
    Complexity.standard: Budgets(max_agent_calls=48, max_wall_seconds=1800,
                                 max_delegation_depth=2, max_delegations=4),
    Complexity.complex: Budgets(max_agent_calls=80, max_wall_seconds=2700,
                                max_delegation_depth=3, max_delegations=6),
}

# The lead (and, one level down, consulted specialists) pull in other talents ON
# DEMAND via CONSULT:/DELEGATE: lines. Depth and per-scan fan-out live on
# Budgets (scaled by complexity, above); this bounds how much of a specialist's
# reply is fed back — the RESULT: block survives whole, the preamble is what
# gets truncated (see rounds.split_result_block).
DELEGATION_RESULT_MAX_CHARS = 2500
# Independent sibling consults (a seat emitting several CONSULT: lines at once) run
# concurrently — each is a blocking CLI call, so this is the real wall-clock win.
# This caps how many agent subprocesses run at once MACHINE-WIDE (the CLIs are
# heavy local processes; unbounded fan-out would thrash the host). The budget lock
# keeps max_agent_calls exact under this concurrency.
MAX_PARALLEL_AGENTS = int(os.environ.get("GANGOF8_MAX_PARALLEL_AGENTS", "4"))
# API-backed seats (OpenRouter et al.) are plain HTTP requests, not local
# subprocesses — gating them behind the CLI bound made a 7-seat panel run in two
# waves when only 3 seats actually load the machine. They get their own, larger
# bound (adapters declare local_process; unknown adapters count as local, the
# conservative side).
MAX_PARALLEL_API_AGENTS = int(os.environ.get("GANGOF8_MAX_PARALLEL_API_AGENTS", "8"))
# A large single-file artifact can exceed one model response. When a written file
# looks cut off (e.g. HTML missing </html>), the lead is asked to CONTINUE it from
# where it stopped — appending, never re-drafting. Bound how many continuations.
MAX_ARTIFACT_CONTINUATIONS = 3
ARTIFACT_CONTINUATION_TAIL_CHARS = 1200  # how much of the file tail the lead sees to continue
# The lead authors whole files in one shot, so give it markedly more headroom than
# a quick specialist call before timing out.
LEAD_TIMEOUT = 600
# Code authors are user-cancellable and API authors have a no-output stall
# watchdog, so productive generation is not stopped by a guessed wall clock.
# Set either environment value above zero only when an installation explicitly
# wants a hard authoring deadline; zero means no coordinator deadline.
PANEL_AUTHOR_TIMEOUT = max(
    0, int(os.environ.get("GANGOF8_PANEL_AUTHOR_TIMEOUT", "0"))
)
PANEL_RETRY_TIMEOUT = max(
    0, int(os.environ.get("GANGOF8_PANEL_RETRY_TIMEOUT", "0"))
)
FRONTIER_AUTHOR_SEATS = tuple(
    s.strip() for s in os.environ.get("GANGOF8_FRONTIER_AUTHOR_SEATS", "claude,codex").split(",")
    if s.strip()
)
FRONTIER_AUTHOR_TIMEOUT = max(
    0, int(os.environ.get("GANGOF8_FRONTIER_AUTHOR_TIMEOUT", "0"))
)
# An optional wall-clock deadline can cover a whole package. It is disabled by
# default because a productive owner must be allowed to finish; cancellation and
# the OpenRouter no-output stall watchdog remain active. Positive values opt in.
PACKAGE_AUTHOR_DEADLINE = max(
    0, int(os.environ.get("GANGOF8_PACKAGE_AUTHOR_DEADLINE", "0"))
)
# When an operator opts into a package deadline, preserve recovery headroom by
# dividing it across the three author/correction waves. Zero stays unlimited.
PACKAGE_AUTHOR_WAVE_TIMEOUT = (
    max(1, math.ceil(PACKAGE_AUTHOR_DEADLINE / 3))
    if PACKAGE_AUTHOR_DEADLINE > 0 else 0
)
FRONTIER_AUTHOR_RECOVERY_ATTEMPTS = int(
    os.environ.get("GANGOF8_FRONTIER_AUTHOR_RECOVERY_ATTEMPTS", "1")
)
# Semantic release review is implementation work performed by a frontier model,
# so it follows the same default policy as frontier authoring: no coordinator
# wall-clock deadline. A positive environment value remains an explicit operator
# opt-in; zero keeps the call user-cancellable and lets provider-stall handling
# remain authoritative.
FRONTIER_VERIFY_TIMEOUT = max(
    0, int(os.environ.get("GANGOF8_FRONTIER_VERIFY_TIMEOUT", "0"))
)
OPENROUTER_OUTPUT_STALL_TIMEOUT = max(
    1, int(os.environ.get("GANGOF8_OPENROUTER_OUTPUT_STALL_TIMEOUT", "180"))
)
FRONTIER_VERIFY_ATTEMPTS = int(os.environ.get("GANGOF8_FRONTIER_VERIFY_ATTEMPTS", "2"))
# Cap on consecutive times deterministic-assembly failure attribution may
# blame the SAME upstream package for the SAME fault before the goal pauses
# for a human instead of rebuilding-and-retrying forever. A build that hit
# this cap once genuinely needed a human: a real Frogger build looped 133
# times relaunching the same "fix main.js" retry for a bug that actually
# lived in an already-accepted renderer.js dependency.
ASSEMBLY_FAULT_STREAK_LIMIT = int(
    os.environ.get("GANGOF8_ASSEMBLY_FAULT_STREAK_LIMIT", "3")
)
# The strong CODIFIER (summarizer seat) that examines/finishes the panel's output
# — best-of-N selection/review/fix/recover, authoring described files, finishing
# cut-offs, fixing tests — is expected to think hard, so give it more headroom
# than the lead's fast coordination path. (Raised with the removal of the
# judging char caps: the chair now reads both finalists genuinely in full.)
CODIFIER_TIMEOUT = int(os.environ.get("GANGOF8_CODIFIER_TIMEOUT", "600"))

# Talent menu advertised to the lead: each specialist role and what it is good
# for, so the lead knows what it can reach for (its origin model is filled in
# from the role→agent map at prompt-build time).
# The delegable talent menu (orchestrator model: the lead assigns, these DO).
TALENTS: dict[Role, str] = {
    Role.knowledge_retriever: "gather sourced evidence (file:line / URLs)",
    Role.researcher: "do the research: interpret evidence, current/web research",
    Role.architect: "produce the system design and structural tradeoffs",
    Role.code_generator: "author the implementation: complete files / algorithms",
    Role.api_integrator: "external API contracts (endpoint/auth/errors)",
    Role.critic: "rigorous review of a risky claim or implementation",
    Role.red_team: "adversarial/security/abuse failure modes",
    Role.fact_validator: "independently verify specific claims",
    Role.implementer: "draft a complete written deliverable (docs, reports, prose)",
}


def budgets_for(complexity: Complexity) -> Budgets:
    return BUDGETS_BY_COMPLEXITY[complexity].model_copy()


# Backend selection: "mock" (offline, default for tests) or "cli" (Gang of 8
# runs the local claude/codex/gemini CLIs itself, in plain generation mode).
BACKEND = os.environ.get("GANGOF8_BACKEND", "mock")

ROLE_AGENTS_MOCK: dict[Role, str] = {
    Role.lead: "mock",
    Role.knowledge_retriever: "mock",
    Role.researcher: "mock",
    Role.architect: "mock",
    Role.code_generator: "mock",
    Role.api_integrator: "mock",
    Role.critic: "mock",
    Role.red_team: "mock",
    Role.fact_validator: "mock",
    Role.implementer: "mock",
    Role.summarizer: "mock",
}

# Direct local-CLI backend: Gang of 8 invokes the agent CLIs itself in plain
# generation mode (no plan-mode), so the implementer emits real file bodies.
# Multi-model gangof8 via the local CLIs — gemini researches, codex critiques,
# claude designs/implements/summarizes. Remap any role in settings.
ROLE_AGENTS_CLI: dict[Role, str] = {
    # The fixed lead drives every task and pulls in the talents below on demand.
    Role.lead: "claude",
    Role.knowledge_retriever: "gemini",
    Role.researcher: "gemini",
    Role.architect: "claude",
    Role.code_generator: "claude",
    Role.api_integrator: "codex",
    Role.critic: "codex",
    Role.red_team: "gemini",
    Role.fact_validator: "codex",
    Role.implementer: "claude",
    Role.summarizer: "claude",
}

ROLE_AGENTS_BY_BACKEND: dict[str, dict[Role, str]] = {
    "mock": ROLE_AGENTS_MOCK,
    "cli": ROLE_AGENTS_CLI,
}

# The Settings dropdown of models per local CLI seat is fetched LIVE (see
# service.cli_model_catalog): OpenRouter's PUBLIC no-key model catalog grouped
# by vendor (newest first — a model released yesterday appears without a code
# change), plus the gemini SDK's own list when a GEMINI_API_KEY is present.
# This static list is only the OFFLINE FALLBACK, plus the claude tier aliases
# (sonnet/opus/haiku), which the CLI always resolves to its current best.
MODEL_CATALOG_URL = os.environ.get(
    "GANGOF8_MODEL_CATALOG_URL", "https://openrouter.ai/api/v1/models")
MODEL_CATALOG_TTL = 900      # seconds the fetched catalog is cached
MODEL_CATALOG_TIMEOUT = 6    # seconds before the fetch gives up (fallback wins)
CLI_MODEL_CATALOG: dict[str, list[str]] = {
    "claude": ["opus", "sonnet", "haiku",
               "claude-fable-5", "claude-opus-4-8", "claude-sonnet-5"],
    "codex": ["gpt-5.1-codex-max", "gpt-5.1-codex-mini", "gpt-5.1",
              "gpt-5-codex", "gpt-5"],
    "gemini": ["gemini-3-pro-preview", "gemini-2.5-pro",
               "gemini-2.5-flash", "gemini-2.5-flash-lite"],
}

# OpenRouter council seats (pay-per-token API models, no CLI). Each is a friendly
# seat name → OpenRouter model slug; opt-in per seat in Settings, needs an
# OPENROUTER_API_KEY. Mixed freely with the local CLI agents in the role map.
OPENROUTER_ENDPOINT = os.environ.get("GANGOF8_OPENROUTER_ENDPOINT", "https://openrouter.ai/api/v1")
OPENROUTER_DATA_COLLECTION = os.environ.get("GANGOF8_OPENROUTER_DATA", "deny")  # deny | allow
# Each seat is a generic VENDOR (label) + its OpenRouter namespace prefix
# (vendor) + a default model_slug. The Settings UI offers that vendor's live
# models (with capability badges) in a dropdown, plus a custom-slug field.
OPENROUTER_SEATS: dict[str, dict[str, str]] = {
    "deepseek": {"vendor": "deepseek",   "model_slug": "deepseek/deepseek-v4-pro", "label": "DeepSeek"},
    "glm":      {"vendor": "z-ai",       "model_slug": "z-ai/glm-4.6",             "label": "z.ai"},
    "qwen":     {"vendor": "qwen",       "model_slug": "qwen/qwen3.6-plus",        "label": "Alibaba"},
    "kimi":     {"vendor": "moonshotai", "model_slug": "moonshotai/kimi-k2.6",     "label": "Moonshot AI"},
}

# Default mapping for the configured backend (modules that need a specific
# session's mapping receive it explicitly instead of reading this).
ROLE_AGENTS = ROLE_AGENTS_BY_BACKEND.get(BACKEND, ROLE_AGENTS_MOCK)

# The PANEL: seats that contribute in parallel before the lead synthesizes.
# Enabled OpenRouter seats are appended at runtime (service._effective_panel)
# only in council mode.
PANEL_SEATS_BY_BACKEND: dict[str, list[str]] = {
    "mock": ["mock"],
    "cli": ["claude", "codex", "gemini"],
}
# Right-sizing policy (ARCHITECTURE-REVIEW.md, Phase 1): the roster serves the
# task. Default panel mode is "duo" — a lead author plus one independent
# frontier reviewer — because measured runs showed the full council burning
# hundreds of calls on seam defects the extra seats introduced (283 calls for
# one file; five of seven seats contributed one call each). "council" convenes
# every configured seat plus enabled OpenRouter seats for users who explicitly
# want candidate diversity. An explicit settings.panel_seats roster always
# wins over either mode.
PANEL_MODE = os.environ.get("GANGOF8_PANEL_MODE", "duo").strip().lower()
# How many seats a duo panel convenes (lead + reviewers).
DUO_PANEL_SIZE = 2
# Goals default to a frontier-only build roster; set to 1 to let enabled
# budget (OpenRouter) seats join every goal as before.
GOAL_FULL_ROSTER = os.environ.get(
    "GANGOF8_GOAL_FULL_ROSTER", "").strip().lower() in {"1", "true", "yes"}
# Rounds proceed automatically; after this many without ROUND: DONE the run
# pauses and asks the human whether to go another block of rounds.
ROUNDS_PER_CONSENT = 3
PANEL_TO_LEAD_CHARS = 2500       # per-seat text folded into the lead synthesis prompt
PANEL_CARRYOVER_CHARS = 900      # a seat's own prior-round text carried into its next prompt
PEER_CARRYOVER_CHARS = 500       # a peer seat's prior-round text carried into others' prompts
SYNTHESIS_CARRYOVER_CHARS = 2000  # last round's lead synthesis carried forward
ROUND_SUMMARY_CHARS = 300        # per-round digest shown in the consent question
# A lead synthesis that only ANNOUNCES work ("I'll read the files, then deliver
# my analysis") is a stub, not a result — seen in a live run where the marker
# default then accepted 100 chars of intent as DONE. Below this size, with no
# deliverable/marker lines and deferral phrasing, the lead is re-called once
# demanding the work now; a second stub degrades to composing from the panel
# views (the composer rescue path proven in that same run).
SYNTHESIS_STUB_CHARS = 250
# For a pure-answer task, a lead synthesis at least this substantial IS the
# final answer — it already weighed every panel view, and re-compressing it
# through the summarizer costs a call and loses content (live run: a 5.9KB
# synthesis shrank to 2.7KB, dropping whole sections). Thin or CONTINUE
# syntheses still get real composition.
SYNTHESIS_FINAL_MIN_CHARS = 1500

# Retained for settings back-compat; classification still reports risk but it
# is informational — the loop no longer pauses on it. The one hard gate is the
# promote approval (workspace → established folder).
RISK_BOUNDARY = Risk.low

APPROVAL_CATEGORIES = [
    "file_write",
    "file_edit",
    "file_delete",
    "code_exec",
    "send_message",
    "spend",
    "settings",
    "external",
    "read",
    "web",
    "stage",
    "promote",
]

# promote (workspace → established folder) is the ONE approval-gated boundary
# that touches real user code; cap the diff shown in its approval card.
PROMOTE_DIFF_MAX_CHARS = 6000
BATCH_PROMOTE_DIFF_MAX_CHARS = int(
    os.environ.get("GANGOF8_BATCH_PROMOTE_DIFF_MAX_CHARS", "60000"))

# run_tests (code execution) bounds — the command runs FREELY in the council's
# own sandbox/workspace (the spaces model gates only promote); keep it time-
# and output-bounded.
RUN_TESTS_TIMEOUT = 300
# The goal loop: when a build's test run fails, the lead is shown the failure
# and asked to repair the code (EDIT/ARTIFACT blocks), then the tests re-run —
# up to this many attempts, all BEFORE anything is promoted. The count persists
# on the session, so an approval pause can't reset the clock. Exhausted
# attempts compose honestly: "tests still failing after N fix attempts".
MAX_TEST_FIX_ATTEMPTS = 3
# Runtime/acceptance verification failures are repaired separately from a model
# supplied RUNTESTS command. The error is coordinator-generated, so it must
# always enter this bounded loop instead of jumping straight to a false done.
MAX_ARTIFACT_REPAIR_ATTEMPTS = 2
RUN_TESTS_OUTPUT_MAX_CHARS = 4000
# Existing-file revisions are authored as compact patches, not as several
# competing whole-file rewrites.  The primary author gets the exact source up
# to this cap so it can make a grounded edit without a chain of rediscovery
# reads; larger files retain the normal read/search fallback.
REVISION_SOURCE_MAX_CHARS = int(os.environ.get("GANGOF8_REVISION_SOURCE_MAX_CHARS", "80000"))

# Per-agent CLI timeouts (seconds). The gemini CLI in headless plan mode is
# markedly slower than claude/codex and prone to stalling, so give it more room
# before timing out. A seat that still times out is dropped gracefully (the
# round continues without it) rather than aborting the whole deliberation.
AGENT_TIMEOUT_DEFAULT = 120
# gemini headless either answers reasonably fast or hangs; a long timeout just
# makes a hang painful (you wait the whole time for nothing). Keep it short so a
# stall surfaces quickly and the seat-drop / composer fallback can take over.
# codex, by contrast, does genuine work per call (e.g. live web research in the
# researcher seat, deep review as critic/fact_validator) that legitimately runs
# past the 120s default — its invocation is non-interactive and doesn't hang, so
# the extra time is real reasoning, not a stall. Give it real headroom.
AGENT_TIMEOUTS: dict[str, int] = {
    "gemini": 150,
    "codex": 300,
    # the claude CLI writes long, careful panel takes — observed >120s on real
    # runs (two seats dropped at the old default); it works, it's just thorough
    "claude": 240,
}


def agent_timeout(agent: str) -> int:
    return AGENT_TIMEOUTS.get(agent, AGENT_TIMEOUT_DEFAULT)

# The ONLY capability that never needs approval (default-deny everything else).
ALWAYS_ALLOWED_CAPABILITIES = {"generate_text"}

# Deliberation may not spend the last N agent calls — they are reserved so the
# composer (and its one retry) can always run. Learned from the first real run,
# where 8 critic tests starved the composer's retry.
COMPOSER_RESERVED_CALLS = 2

# A real critic can raise many conflicts per round; test the first N with the
# critic and rule the rest on constraints, instead of burning the budget.
MAX_CRITIC_TESTS_PER_ROUND = 3

# A disagreement ruling is a verdict + a sentence or two — cap it so a verbose
# model can't bloat the record (and the UI) with a full essay per conflict.
CRITIC_TEST_MAX_CHARS = 600

# Agents often return the final answer as plain prose, not the labeled format.
# Substantial prose (>= this many chars) is accepted as the answer at medium
# confidence; shorter unparseable output gets one retry.
COMPOSER_PROSE_MIN_CHARS = 200

# How much deliberation the summarizer sees when composing. The earlier
# 5×400-char window truncated long reconciliations mid-sentence, so the
# summarizer would pause to ask the human for the very result the council had
# already produced. Give it the tail of the deliberation in full-enough form.
COMPOSER_CONTEXT_CONTRIBUTIONS = 6
COMPOSER_CONTEXT_CHARS = 1400

# In-deliberation skill requests: an agent may emit 'SKILL: <name> <arg>' to
# pull a no-approval capability (e.g. read_file) mid-round. Bound how many it
# can request per turn and how much result text is fed back, so a turn can't
# balloon the prompt or the budget. Analysis tasks (research/question/design)
# get more headroom — reading the material IS the job there; the tight cap
# starved "examine this codebase" runs (a lead asked for 11 reads, got 2).
MAX_SKILL_REQUESTS_PER_TURN = 2
MAX_SKILL_REQUESTS_ANALYSIS = 6
# The re-called reply may itself open with NEW skill requests (read one file →
# the next read depends on what it said). Resolve those too, chained, up to this
# many re-calls per turn. A live run ended a round on the bare line
# 'SKILL: search_project …' because the single-cycle resolver handed the second
# request back unresolved and it was accepted as the round's synthesis.
MAX_SKILL_CHAIN_TURNS = 3
SKILL_RESULT_MAX_CHARS = 2000
# Analysis tasks also get DEEPER reads: 2000 chars of a 75KB file is ~3%, and a
# live run showed the lead reasoning to a wrong conclusion from exactly that
# truncation (it "read" a test file whose decisive test sat past the cap).
SKILL_RESULT_ANALYSIS_MAX_CHARS = 8000
# A user who asks to match a named source needs the whole source whenever it
# fits a normal model prompt. This is larger than a discovery read but bounded.
MATCHED_SOURCE_MAX_CHARS = 40000
# Council-authored SANDBOX files (panel drafts, delegate artifacts) read whole:
# a live run generated eight complete ~25KB game drafts and then starved the
# lead with the 2000-char window — it paid to produce them and couldn't review
# a single one ("every draft is truncated mid-file", looping on re-reads).
SKILL_RESULT_SANDBOX_MAX_CHARS = 40000
MAX_ESCALATION_REQUESTS_PER_TURN = 2
ESCALATION_RESULT_MAX_CHARS = 2500

# The round-0 overview head-caps each source file, which cut a 37KB shell.html
# off right at the engine namespace — every seat then burned SKILL chains
# re-reading the file just to find the contract it had to bind to. For any file
# the cap truncates, an API SURFACE (class/function/registration declarations
# extracted from the WHOLE body) is appended, bounded per file by this.
OVERVIEW_API_SURFACE_MAX_CHARS = 2500

# Best-of-N selection: on a file-producing build, every panel seat authors a
# complete candidate implementation, independent judges SCORE all candidates
# blindly (author identity stripped), and the highest-scoring file is the one
# shipped — a real model's code, not a lead re-author. Owner directive
# 2026-07-05 ("I want true best-of-N selection").
BEST_OF_N_MIN_CANDIDATES = 2      # fewer than this ⇒ fall back to author path
MAX_JUDGES = int(os.environ.get("GANGOF8_MAX_JUDGES", "3"))
# Judges and the chair see every candidate IN FULL — no per-candidate char cap.
# The old 24000-char window made the vote measure "which file fit under the
# cap": every larger candidate read as cut off mid-file, and the prompt orders
# judges to score truncation LOW (live: a 23KB game unanimously beat three
# richer 38-53KB ones whose code the judges saw only 45-63% of). Owner
# directive 2026-07-12: judges examine each member's complete output. A judge
# whose context can't hold the material drops (judge_dropped) — an honest
# abstention instead of a systematically biased score.
JUDGE_SCORE_MAX = 10              # score scale a judge gives each candidate
# Scoring calls carry every candidate's full body, so give judges reading
# headroom over the quick per-seat default (claude 240s would be the binding
# timeout otherwise, with far more to read than before).
JUDGE_TIMEOUT = int(os.environ.get("GANGOF8_JUDGE_TIMEOUT", "480"))
# Judges run in PARALLEL waves: the first wave votes, and only a SPLIT vote
# convenes the rest — a unanimous first wave with at least
# JUDGE_EARLY_STOP_MIN_VOTES real votes decides the winner outright (live: a
# 5-judge vote went 5/5 first-place; judges 4 and 5 each re-read the entire
# multi-hundred-KB candidate corpus to add zero information).
JUDGE_FIRST_WAVE = int(os.environ.get("GANGOF8_JUDGE_FIRST_WAVE", "2"))
JUDGE_EARLY_STOP_MIN_VOTES = 2
# Smoke probes are independent subprocesses. Parallelism shortens a multi-seat
# best-of-N runtime gate without increasing model cost.
MAX_PARALLEL_SMOKE = int(os.environ.get("GANGOF8_MAX_PARALLEL_SMOKE", "4"))

# Per-file artifact materialization: an agent sometimes describes multi-file
# output in one draft instead of emitting it. When an output task yields no full
# ARTIFACT blocks, the coordinator fetches each intended file with its own
# focused call (nothing to summarize). Cap how many files one task may produce.
MAX_ARTIFACT_FILES = 8

# Goal layer (/goal): a long-horizon objective decomposed by the architect into
# milestone-sized deliverables, each run as a normal session. Invalid plans are
# returned to that architect with the deterministic contract errors instead of
# making the human manually retry an unchanged goal. Keep repair bounded so a
# provider that repeatedly ignores the contract cannot loop forever.
GOAL_MAX_MILESTONES = 8
GOAL_PLAN_TIMEOUT = 600       # s per planning/repair call (architect thinks hard)
GOAL_PLAN_REPAIR_ATTEMPTS = max(
    0, int(os.environ.get("GANGOF8_GOAL_PLAN_REPAIR_ATTEMPTS", "2"))
)
GOAL_SUMMARY_MAX_CHARS = 700  # per completed milestone folded into the next one's task

# search_project skill bounds: keep a search cheap and the result feed-back small.
SEARCH_MAX_FILES = 400          # files whose contents are scanned
SEARCH_MAX_MATCHES = 60         # content-match lines returned
SEARCH_MAX_FILE_BYTES = 500_000  # skip files larger than this
SEARCH_RESULT_MAX_CHARS = 4000  # cap the formatted result fed back to the agent

# list_dir skill bounds: a bounded directory listing so agents can DISCOVER what
# exists in the workspace before reading/writing. Kept cheap and small.
LIST_DIR_MAX_ENTRIES = 300      # files/folders listed before truncating
LIST_DIR_MAX_DEPTH = 6          # nesting depth walked
LIST_DIR_RESULT_MAX_CHARS = 4000  # cap the formatted listing fed back to the agent

# Web access: the coordinator reaches the internet for the council (web_search /
# web_fetch skills). Read-only, no side effects on the host. NOTE: queries/URLs
# leave the machine. Disable by setting GANGOF8_WEB=0.
WEB_ENABLED = os.environ.get("GANGOF8_WEB", "1") != "0"
WEB_SEARCH_MODEL = os.environ.get("GANGOF8_WEB_MODEL", "gemini-2.5-flash")
WEB_SEARCH_MAX_CHARS = 4000     # cap a search result fed back to the agent
WEB_FETCH_TIMEOUT = 20          # seconds
WEB_FETCH_MAX_BYTES = 2_000_000  # cap the download
WEB_FETCH_MAX_CHARS = 6000      # cap the extracted text fed back to the agent
