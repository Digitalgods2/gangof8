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
    # web/UI build requests: a "calendar webpage" / "landing page website" is a
    # file-producing task, not a question — without these it misclassified as a
    # plain question and the lead was never asked to emit a file.
    "webpage", "web page", "website", "web app", "html", "css",
]
# A filename with a known code/text extension (main.py, requirements.txt) is a
# strong signal the task produces files even when no code verb is present.
_FILE_ARTIFACT = re.compile(
    r"\b[\w\-]+\.(py|js|ts|tsx|jsx|go|rs|java|rb|php|c|cpp|h|hpp|cs|md|txt|"
    r"json|ya?ml|toml|ini|cfg|csv|html|css|scss|sh|bat|ps1|sql)\b",
    re.IGNORECASE,
)
# A PROSE-DOCUMENT artifact (story.txt, essay.md) — the container of a writing
# deliverable, NOT a code signal. Used to keep "write the story, save it as
# X.txt" a CONTENT task instead of letting the .txt route it to code (which
# drops the prose into the game/runtime best-of-N judging path).
_DOC_ARTIFACT = re.compile(
    r"\b[\w\-]+\.(txt|md|markdown|rst|rtf|tex|docx?)\b", re.IGNORECASE
)
# Building something NEW (vs. examining/improving an existing folder). A
# greenfield build needs a destination — the loop ASKS if none was referenced.
GREENFIELD_WORDS = [
    "build", "create", "make", "scaffold", "generate", "bootstrap",
    "new app", "new project", "from scratch", "starter", "greenfield",
]
# Examining/understanding/recommending — the deliverable is prose analysis,
# not files. Counts only when no create/modify verb is also present. Noun and
# derived forms matter: "give a firm recommendation" must count the same as
# "recommend" (word-boundary matching won't find 'recommend' inside it).
ANALYSIS_WORDS = [
    "examine", "understand", "review", "analyze", "analyse", "assess",
    "evaluate", "audit", "recommend", "suggest", "critique", "inspect",
    "explain", "describe",
    "recommendation", "recommendations", "suggestion", "suggestions",
    "compare", "comparison", "advice", "opinion", "pros and cons",
    "trade-offs", "tradeoffs", "evaluation", "assessment",
]
# UNAMBIGUOUS "produce/change files now" verbs. Deliberately excludes vague
# words (make/add/improve/update/build) that show up in analysis phrasing like
# "make this app better" or "recommend things to add" — those must NOT force a
# recommendation task to produce files.
MODIFY_WORDS = [
    "implement", "refactor", "scaffold", "rewrite", "migrate", "port",
    "convert", "fix the", "fix a", "patch",
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

    # Analysis override: "examine/understand/recommend/review this app" is an
    # ANALYSIS task — its deliverable is the answer (recommendations), NOT files.
    # The mere mention of an "app" must not route it to code (which would spawn
    # the implementer and write scratch files). A real create/modify verb keeps
    # it as code; otherwise downgrade to research so NO files are produced.
    analysis = _any(ANALYSIS_WORDS, lower)
    # an explicit file to produce, or a clear create/modify verb, keeps it code
    wants_files = _any(MODIFY_WORDS, lower) or bool(_FILE_ARTIFACT.search(text))
    if analysis and not wants_files and task_type in (TaskType.code, TaskType.design):
        task_type = TaskType.research
        notes.append("analysis/examination intent (no create/modify verb) — "
                     "treated as research; produces no files")

    # Content override: a WRITING task ("write the story, save it as Benny's
    # Ride.txt") is CONTENT even though the .txt artifact tripped the code rule
    # above. The prose-document extension is the deliverable's container, not a
    # code signal — so when a content verb is present and the ONLY code marker is
    # a prose-document file (no real code word), route it to content. This keeps
    # it out of the game/runtime judging framing while still producing the file
    # (content is a produces_output type).
    if (content and task_type == TaskType.code
            and not _any(CODE_WORDS, lower)
            and _DOC_ARTIFACT.search(text)):
        task_type = TaskType.content
        notes.append("writing task producing a prose document — treated as content")

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

    # Any output-producing task may propose artifacts (promote stays gated).
    tools_allowed = task_type in (TaskType.action, TaskType.code, TaskType.content, TaskType.design)
    # Informational only: shown in the UI, but the loop no longer pauses on it —
    # the one hard gate is the promote approval at delivery time.
    human_approval_required = risk_gt(risk, config.RISK_BOUNDARY)
    if human_approval_required:
        notes.append(f"risk above boundary ({config.RISK_BOUNDARY.value}) — flagged for visibility")

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
