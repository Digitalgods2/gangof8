"""Configuration: budgets, role→agent assignments, approval categories."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from .models import Budgets, Complexity, Risk, Role

DATA_DIR = Path(os.environ.get("CONCLAVE_OS_DATA", str(Path(__file__).resolve().parent.parent / "data")))


def _default_sandbox_root() -> Path:
    """A NEUTRAL scratch location, deliberately OUTSIDE any project/source folder
    so the ephemeral sandbox can never sit inside (or corrupt) source material.
    Windows → %LOCALAPPDATA%\\ConclaveOS\\sandbox; else a temp-dir subfolder.
    Override with CONCLAVE_OS_SANDBOX."""
    base = os.environ.get("LOCALAPPDATA") if os.name == "nt" else None
    root = Path(base) / "ConclaveOS" if base else Path(tempfile.gettempdir()) / "conclave_os"
    return root / "sandbox"


# The ephemeral per-session sandbox lives here — NEVER under DATA_DIR or any
# project folder. Each session gets its own subdir (executor.artifacts_dir).
SANDBOX_ROOT = Path(os.environ.get("CONCLAVE_OS_SANDBOX", str(_default_sandbox_root())))

BUDGETS_BY_COMPLEXITY: dict[Complexity, Budgets] = {
    Complexity.trivial: Budgets(max_rounds=1, max_turns_per_round=1, max_agent_calls=4, max_wall_seconds=180),
    # Headroom raised so convergence (refine-until-accepted) has room to iterate;
    # easy tasks still finish fast via early acceptance.
    Complexity.standard: Budgets(max_rounds=3, max_turns_per_round=2, max_agent_calls=24, max_wall_seconds=1200),
    Complexity.complex: Budgets(max_rounds=4, max_turns_per_round=2, max_agent_calls=40, max_wall_seconds=1800),
}

# Convergence-driven deliberation: after the planned phases, the implementer
# revises against the critic's objections and is re-reviewed, repeating UNTIL the
# critic accepts. This is the safety backstop on that loop (alongside the
# agent-call budget and wall-time) — NOT the normal terminator.
MAX_REFINE_ITERATIONS = 6


def budgets_for(complexity: Complexity) -> Budgets:
    return BUDGETS_BY_COMPLEXITY[complexity].model_copy()


# Backend selection: "mock" (offline, default for tests) or "cli" (Conclave OS
# runs the local claude/codex/gemini CLIs itself, in plain generation mode).
BACKEND = os.environ.get("CONCLAVE_OS_BACKEND", "mock")

ROLE_AGENTS_MOCK: dict[Role, str] = {
    Role.researcher: "mock",
    Role.architect: "mock",
    Role.critic: "mock",
    Role.implementer: "mock",
    Role.summarizer: "mock",
}

# Direct local-CLI backend: Conclave OS invokes the agent CLIs itself in plain
# generation mode (no plan-mode), so the implementer emits real file bodies.
# Multi-model conclave via the local CLIs — gemini researches, codex critiques,
# claude designs/implements/summarizes. Remap any role in settings.
ROLE_AGENTS_CLI: dict[Role, str] = {
    Role.researcher: "gemini",
    Role.architect: "claude",
    Role.critic: "codex",
    Role.implementer: "claude",
    Role.summarizer: "claude",
}

ROLE_AGENTS_BY_BACKEND: dict[str, dict[Role, str]] = {
    "mock": ROLE_AGENTS_MOCK,
    "cli": ROLE_AGENTS_CLI,
}

# Default mapping for the configured backend (modules that need a specific
# session's mapping receive it explicitly instead of reading this).
ROLE_AGENTS = ROLE_AGENTS_BY_BACKEND.get(BACKEND, ROLE_AGENTS_MOCK)

# Any classified risk ABOVE this boundary forces a human approval gate
# before round 1 (loop step 7 / DESIGN hard rules).
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

# run_tests (code execution) bounds — the command runs in the workspace only
# after explicit human approval; keep it time- and output-bounded.
RUN_TESTS_TIMEOUT = 300
RUN_TESTS_OUTPUT_MAX_CHARS = 4000

# Per-agent CLI timeouts (seconds). The gemini CLI in headless plan mode is
# markedly slower than claude/codex and prone to stalling, so give it more room
# before timing out. A seat that still times out is dropped gracefully (the
# round continues without it) rather than aborting the whole deliberation.
AGENT_TIMEOUT_DEFAULT = 120
# gemini headless either answers reasonably fast or hangs; a long timeout just
# makes a hang painful (you wait the whole time for nothing). Keep it short so a
# stall surfaces quickly and the seat-drop / composer fallback can take over.
AGENT_TIMEOUTS: dict[str, int] = {
    "gemini": 150,
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
# balloon the prompt or the budget.
MAX_SKILL_REQUESTS_PER_TURN = 2
SKILL_RESULT_MAX_CHARS = 2000

# Per-file artifact materialization: an agent sometimes describes multi-file
# output in one draft instead of emitting it. When an output task yields no full
# ARTIFACT blocks, the coordinator fetches each intended file with its own
# focused call (nothing to summarize). Cap how many files one task may produce.
MAX_ARTIFACT_FILES = 8

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
# leave the machine. Disable by setting CONCLAVE_OS_WEB=0.
WEB_ENABLED = os.environ.get("CONCLAVE_OS_WEB", "1") != "0"
WEB_SEARCH_MODEL = os.environ.get("CONCLAVE_OS_WEB_MODEL", "gemini-2.5-flash")
WEB_SEARCH_MAX_CHARS = 4000     # cap a search result fed back to the agent
WEB_FETCH_TIMEOUT = 20          # seconds
WEB_FETCH_MAX_BYTES = 2_000_000  # cap the download
WEB_FETCH_MAX_CHARS = 6000      # cap the extracted text fed back to the agent
