"""Task Classifier — rule-based (Phase 0). LLM-assist can be added later
behind the same classify() signature."""

from __future__ import annotations

import re

from . import config
from .models import Classification, Complexity, Risk, TaskType, risk_gt

ACTION_WORDS = [
    "delete", "remove", "send", "email", "post", "publish", "buy",
    "purchase", "pay", "spend", "deploy", "install", "upload", "message",
]
CODE_WORDS = [
    "code", "script", "function", "implement", "refactor", "compile",
    "program", "bug", "fix", "build", "app", "application", "api",
    "module", "package", "library", "cli", "endpoint", "backend",
    "frontend", "class",
]
# A filename with a known code/text extension (main.py, requirements.txt) is a
# strong signal the task produces files even when no code verb is present.
_FILE_ARTIFACT = re.compile(
    r"\b[\w\-]+\.(py|js|ts|tsx|jsx|go|rs|java|rb|php|c|cpp|h|hpp|cs|md|txt|"
    r"json|ya?ml|toml|ini|cfg|csv|html|css|scss|sh|bat|ps1|sql)\b",
    re.IGNORECASE,
)
# Building something NEW (vs. examining/improving an existing folder). A
# greenfield build needs a destination — the loop ASKS if none was referenced.
GREENFIELD_WORDS = [
    "build", "create", "make", "scaffold", "generate", "bootstrap",
    "new app", "new project", "from scratch", "starter", "greenfield",
]
EXEC_WORDS = ["execute", "run", "install", "launch"]
DESIGN_WORDS = ["design", "architecture", "architect", "blueprint", "schema", "structure"]
RESEARCH_WORDS = ["research", "investigate", "survey", "look up", "find out"]
CONTENT_WORDS = ["write", "draft", "essay", "article", "blog", "summarize", "story"]
EXTERNAL_READ_WORDS = ["download", "fetch", "browse", "search the web"]
HIGH_RISK_WORDS = [
    "delete", "remove", "pay", "buy", "purchase", "spend", "money",
    "email", "send", "publish", "deploy", "credentials", "password", "private",
]
GOVERNANCE_WORDS = ["file", "files", "money", "private", "secret", "account"]


def _any(words: list[str], lower: str) -> bool:
    return any(re.search(rf"\b{re.escape(w)}\b", lower) for w in words)


def classify(text: str, role_agents: dict | None = None) -> Classification:
    lower = text.lower()
    notes: list[str] = []

    action = _any(ACTION_WORDS, lower)
    code = _any(CODE_WORDS, lower) or bool(_FILE_ARTIFACT.search(text))
    execs = _any(EXEC_WORDS, lower)
    design = _any(DESIGN_WORDS, lower)
    research = _any(RESEARCH_WORDS, lower)
    content = _any(CONTENT_WORDS, lower)
    external_read = _any(EXTERNAL_READ_WORDS, lower)

    if action:
        task_type = TaskType.action
        notes.append("matched action words (external side effects implied)")
    elif code:
        task_type = TaskType.code
        notes.append(
            "matched filename artifact" if _FILE_ARTIFACT.search(text) and not _any(CODE_WORDS, lower)
            else "matched code words"
        )
    elif design:
        task_type = TaskType.design
        notes.append("matched design words")
    elif research:
        task_type = TaskType.research
        notes.append("matched research words")
    elif content:
        task_type = TaskType.content
        notes.append("matched content words")
    else:
        task_type = TaskType.question
        notes.append("no action/code/design markers; treated as question")

    words = len(text.split())
    if words <= 8:
        complexity = Complexity.trivial
    elif words >= 60 or text.count(". ") >= 3:
        complexity = Complexity.complex
    else:
        complexity = Complexity.standard
    if complexity == Complexity.trivial and task_type in (TaskType.action, TaskType.code, TaskType.design):
        complexity = Complexity.standard  # never trivialize side-effecty work
    notes.append(f"complexity from length/structure: {complexity.value}")

    if action and _any(HIGH_RISK_WORDS, lower):
        risk = Risk.high
    elif action or execs:
        risk = Risk.medium
    elif external_read:
        risk = Risk.low
    else:
        risk = Risk.none
    notes.append(f"risk: {risk.value}")

    # Any output-producing task may propose artifacts (still approval-gated).
    tools_allowed = task_type in (TaskType.action, TaskType.code, TaskType.content, TaskType.design)
    human_approval_required = risk_gt(risk, config.RISK_BOUNDARY)
    if human_approval_required:
        notes.append(f"risk above boundary ({config.RISK_BOUNDARY.value}) — human approval required")

    # code included: a coding task benefits from a context-gathering round first
    # (read/search the existing code, surface constraints) — and it's the role
    # the researcher seat (gemini by default) fills.
    needs_facts = task_type in (
        TaskType.question, TaskType.research, TaskType.design, TaskType.content, TaskType.code
    )
    needs_design = task_type in (TaskType.design, TaskType.code)
    produces_output = task_type in (TaskType.code, TaskType.content, TaskType.design, TaskType.action)
    quality_matters = complexity != Complexity.trivial or risk != Risk.none
    needs_governance = action or risk_gt(risk, Risk.low) or _any(GOVERNANCE_WORDS, lower)
    greenfield = task_type == TaskType.code and _any(GREENFIELD_WORDS, lower)
    if greenfield:
        notes.append("greenfield build (creates something new) — needs a target if none referenced")

    skills = []
    if needs_facts:
        skills.append("research")
    if needs_design:
        skills.append("architecture")
    if produces_output:
        skills.append("implementation")
    if quality_matters:
        skills.append("critique")

    return Classification(
        task_type=task_type,
        complexity=complexity,
        risk=risk,
        skills_needed=skills,
        agents_required=sorted(set((role_agents or config.ROLE_AGENTS).values())),
        tools_allowed=tools_allowed,
        human_approval_required=human_approval_required,
        rationale="; ".join(notes),
        needs_facts=needs_facts,
        needs_design=needs_design,
        produces_output=produces_output,
        quality_matters=quality_matters,
        needs_governance=needs_governance,
        greenfield=greenfield,
    )
