"""Configuration: budgets, role→agent assignments, approval categories."""

from __future__ import annotations

import os
from pathlib import Path

from .models import Budgets, Complexity, Risk, Role

DATA_DIR = Path(os.environ.get("CONCLAVE_OS_DATA", str(Path(__file__).resolve().parent.parent / "data")))

BUDGETS_BY_COMPLEXITY: dict[Complexity, Budgets] = {
    Complexity.trivial: Budgets(max_rounds=1, max_turns_per_round=1, max_agent_calls=4, max_wall_seconds=180),
    Complexity.standard: Budgets(max_rounds=3, max_turns_per_round=2, max_agent_calls=12, max_wall_seconds=600),
    Complexity.complex: Budgets(max_rounds=4, max_turns_per_round=2, max_agent_calls=16, max_wall_seconds=900),
}


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
    "file_delete",
    "code_exec",
    "send_message",
    "spend",
    "settings",
    "external",
    "read",
]

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
