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
# ---------------------------------------------------------------------------
# Deliverable FORMATS the council cannot type out.
#
# ARTIFACT blocks carry text by definition, so a PDF, a .docx, a .zip or an
# image can only be produced by an approved BUILD. Naming one is a statement
# about the FINISHED ARTIFACT — not evidence that the task is software work.
#
# Live failure this exists to stop: "research heavily the works of Auguste
# Escoffier and compile a pdf of his recipes" matched the CODE_WORDS entry
# 'compile', was typed `code`, and shipped a 49KB reportlab generator script
# that nothing ever ran. No PDF was produced and no gate noticed one was
# missing, so the run reported high confidence on a deliverable that did not
# exist.
# ---------------------------------------------------------------------------
_BINARY_FORMAT_ALIASES = {
    "pdf": "pdf",
    "doc": "docx", "docx": "docx", "odt": "odt", "rtf": "rtf",
    "xls": "xlsx", "xlsx": "xlsx", "ods": "ods",
    "ppt": "pptx", "pptx": "pptx", "odp": "odp",
    "epub": "epub", "mobi": "mobi",
    "zip": "zip", "tar": "tar", "gz": "gz", "tgz": "tar", "7z": "7z", "rar": "rar",
    "png": "png", "jpg": "jpg", "jpeg": "jpg", "gif": "gif", "bmp": "bmp",
    "tif": "tiff", "tiff": "tiff", "webp": "webp", "ico": "ico",
    "mp3": "mp3", "wav": "wav", "flac": "flac", "ogg": "ogg", "m4a": "m4a",
    "mp4": "mp4", "mov": "mov", "avi": "avi", "webm": "webm", "mkv": "mkv",
    "xlsm": "xlsx", "docm": "docx",
}
# "doc" and "tar" are ordinary English as often as they are formats, so they
# count only with a leading dot (report.doc). Everything in this set is
# unambiguous enough to count as a bare word ("compile a pdf"); "zip" is only
# here because the output-cue rule below already rejects "zip code".
_BARE_FORMAT_WORDS = {
    "pdf", "docx", "docm", "xlsx", "xlsm", "pptx", "epub", "mobi", "zip",
    "png", "jpg", "jpeg", "gif", "webp", "tiff", "mp3", "wav", "flac",
    "mp4", "mov", "webm", "odt", "ods", "odp",
}
_BINARY_FORMAT_MENTION = re.compile(
    r"(?:\.(?P<ext>"
    + "|".join(sorted(_BINARY_FORMAT_ALIASES, key=len, reverse=True))
    + r")|\b(?P<word>"
    + "|".join(sorted(_BARE_FORMAT_WORDS, key=len, reverse=True))
    + r"))\b",
    re.IGNORECASE)

# How far back from a format token to look for the cue that decides whether it
# names an OUTPUT or an INPUT.
_CUE_WINDOW = 60
# "compile a pdf" / "export it as a docx" — the format is being PRODUCED.
_OUTPUT_CUES = re.compile(
    r"\b(compile|compiling|create|creating|make|making|generate|generating|"
    r"produce|producing|build|building|export|exporting|render|rendering|"
    r"convert|converting|output|outputting|save|saving|write|writing|turn|"
    r"turning|assemble|assembling|deliver|delivering|publish|publishing|"
    r"emit|emitting|prepare|preparing|format|formatted|formatting|bundle|"
    r"bundling|package|packaging|ship|shipping|final|finished|deliverable|"
    r"want|need|give me|send me|hand me|email me|as|into|to|in)\b",
    re.IGNORECASE)
# "summarize this pdf" / "read the attached docx" — the format is an INPUT.
_INPUT_CUES = re.compile(
    r"\b(read|reading|attached|attach|attachment|summari[sz]e|summari[sz]ing|"
    r"from|parse|parsing|extract|extracting|analy[sz]e|analy[sz]ing|review|"
    r"reviewing|open|opening|uploaded|provided|supplied|given|existing|"
    r"this|these|those|my|our|their|the following)\b",
    re.IGNORECASE)


def _last_match_end(pattern: re.Pattern, window: str) -> int | None:
    """Offset of the LAST match in the window, or None. Nearest cue to the
    format token wins, so one sentence can name an input and an output
    ("turn the attached pdf into a docx")."""
    last = None
    for m in pattern.finditer(window):
        last = m.start()
    return last


def deliverable_formats(text: str) -> list[str]:
    """Non-text output formats the task declares as its deliverable.

    Only OUTPUT mentions count. The cue nearest the format token decides, which
    keeps "summarize this pdf" (an input) apart from "compile a pdf" (the
    deliverable) without needing to understand the sentence.
    """
    body = text or ""
    found: list[str] = []
    for m in _BINARY_FORMAT_MENTION.finditer(body):
        token = m.group("ext") or m.group("word") or ""
        fmt = _BINARY_FORMAT_ALIASES.get(token.lower())
        if not fmt or fmt in found:
            continue
        window = body[max(0, m.start() - _CUE_WINDOW):m.start()]
        out = _last_match_end(_OUTPUT_CUES, window)
        inp = _last_match_end(_INPUT_CUES, window)
        if out is None or (inp is not None and inp > out):
            continue
        found.append(fmt)
    return found


def binary_format_of(name: str) -> str:
    """The normalized deliverable format of a filename, '' when it is not one.
    Used by the delivery gate to tell a produced .pdf from its generator."""
    raw = (name or "").strip().replace("\\", "/").rsplit("/", 1)[-1]
    if "." not in raw:
        return ""
    return _BINARY_FORMAT_ALIASES.get(raw.rsplit(".", 1)[-1].lower(), "")


# CODE_WORDS that mean "assemble/produce" in ordinary English just as readily as
# they mean software work. On their own they must never type a document request
# as code — "compile a pdf of his recipes" is a cookbook, not a build system.
_AMBIGUOUS_CODE_WORDS = {"compile", "build", "package", "library", "program"}
_STRONG_CODE_WORDS = [w for w in CODE_WORDS if w not in _AMBIGUOUS_CODE_WORDS]


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


# "Make the output MATCH an existing reference exactly / a matched set": the user
# wants the deliverable to mirror a named source. When true (and a source is
# named), that source's STRUCTURE — its headings, tables, per-unit scaffolding
# (e.g. a picture book's per-spread illustration prompts) — is a HARD requirement,
# not the "commentary/outline" a generic 'no commentary' instruction would strip.
# Carried on the classification so the blind judges and the finisher weigh
# structural fidelity to the source, not prose alone (a plain-prose candidate that
# dropped the source's format once won a "matched set" task it did not match).
_MATCH_INTENT = (
    "matched set", "match the first", "match the existing", "match its",
    "match the source", "match the style", "same style", "same format",
    "same structure", "match exactly", "exactly the same", "mirror the",
    "in the same style", "consistent with the series", "feel like a matched",
    "formatted exactly like", "format exactly like", "format it exactly like",
)

# Generic words such as "build" occur in prose briefs ("build tension").
# These are the signals that still make a prose-document request a software task.
_PROGRAMMING_SIGNALS = [
    "code", "script", "function", "implement", "refactor", "compile",
    "program", "bug", "fix", "app", "application", "api", "module",
    "package", "library", "cli", "endpoint", "backend", "frontend",
    "class", "webpage", "web page", "website", "web app", "html", "css",
]
_EXECUTABLE_ARTIFACT = re.compile(
    r"\b[\w\-]+\.(py|js|ts|tsx|jsx|go|rs|java|rb|php|c|cpp|h|hpp|cs|"
    r"json|ya?ml|toml|ini|cfg|csv|html|css|scss|sh|bat|ps1|sql)\b",
    re.IGNORECASE,
)

# ``attachment_context()`` appends source bodies to the user's instruction.
# Classifying action/risk words across that body mistakes ordinary code such as
# ``element.remove()`` or ``send()`` for a request to perform an external side
# effect. Keep directive intent separate while still letting the attachment's
# filename/content provide a strong code-artifact signal.
_ATTACHMENTS_MARKER = "\n\nAttachments provided by the user:"
_ATTACHED_CODE_FILE = re.compile(
    r"^--- Attached [^:\r\n]+ file:\s*[^\r\n]+\."
    r"(?:py|js|ts|tsx|jsx|go|rs|java|rb|php|c|cpp|h|hpp|cs|html|css|scss|"
    r"sh|bat|ps1|sql)\s*---\s*$",
    re.IGNORECASE | re.MULTILINE,
)
_CODE_REVISION_WORDS = [
    "fix", "improve", "refactor", "repair", "patch", "rewrite", "update",
    "change", "modify", "correct", "debug", "optimize", "complete",
    "return", "provide", "deliver", "add", "integrate",
]


def wants_matched_output(text: str) -> bool:
    """True when the task asks for the output to MATCH a referenced source (a
    'matched set' / 'same style/format' / 'match … exactly'). Substring match —
    these are multi-word phrases, not single tokens."""
    low = (text or "").lower()
    return any(p in low for p in _MATCH_INTENT)


def _any(words: list[str], lower: str) -> bool:
    return any(re.search(rf"\b{re.escape(w)}\b", lower) for w in words)


def classify(text: str, role_agents: dict | None = None) -> Classification:
    lower = text.lower()
    directive_text = text.split(_ATTACHMENTS_MARKER, 1)[0]
    directive_lower = directive_text.lower()
    notes: list[str] = []

    # Side-effect intent must come from the user's directive, never tokens that
    # merely occur inside an attached implementation. An attached code file
    # paired with a revision/delivery verb is explicitly coding work even if
    # the directive also says to remove a bug or obsolete handler.
    action = _any(ACTION_WORDS, directive_lower)
    attached_code = bool(_ATTACHED_CODE_FILE.search(text))
    attachment_revision = attached_code and _any(
        _CODE_REVISION_WORDS, directive_lower
    )
    # Read the deliverable format from the DIRECTIVE only. An attached PDF whose
    # extracted text is appended to the task must not be mistaken for a PDF the
    # run has to produce.
    formats = deliverable_formats(directive_text)
    # A declared non-text deliverable disarms the ambiguous code words. Without
    # this "compile a pdf of his recipes" is typed `code` off the word 'compile'
    # alone and the run ships a generator script instead of the book. A real
    # programming signal (script/function/api/main.py/an attached source file)
    # still wins — "write a python script that builds a pdf" is coding work.
    ambiguous_code_only = bool(
        formats
        and not _any(_STRONG_CODE_WORDS, lower)
        and not _EXECUTABLE_ARTIFACT.search(text)
        and not attached_code
    )
    code = not ambiguous_code_only and (
        _any(CODE_WORDS, lower)
        or bool(_FILE_ARTIFACT.search(text))
        or attached_code
    )
    execs = _any(EXEC_WORDS, directive_lower)
    design = _any(DESIGN_WORDS, lower)
    research = _any(RESEARCH_WORDS, lower)
    content = _any(CONTENT_WORDS, lower)
    external_read = _any(EXTERNAL_READ_WORDS, directive_lower)

    if attachment_revision:
        task_type = TaskType.code
        notes.append("attached code artifact with explicit revision/delivery intent")
    elif action:
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
            and _DOC_ARTIFACT.search(text)
            and not _any(_PROGRAMMING_SIGNALS, lower)
            and not _EXECUTABLE_ARTIFACT.search(text)):
        task_type = TaskType.content
        notes.append("writing task producing a prose document — treated as content")

    # Deliverable override: a named non-text format is a FILE the run must
    # produce, whatever verb introduced it. Research that ends in "…and compile
    # a pdf" is not a prose answer, and the analysis override above must not be
    # allowed to strand it as one — a run typed `research` gets no file contract
    # at all, so the PDF could never be built.
    if formats:
        if ambiguous_code_only:
            notes.append(
                "declares a non-text deliverable; ambiguous code words "
                "(compile/build/package) alone do not make it a coding task")
        if task_type in (TaskType.question, TaskType.research):
            task_type = TaskType.content
            notes.append(
                f"names a non-text deliverable ({', '.join(formats)}) — treated "
                "as content; the run must actually produce that file")
    # On a genuine SOFTWARE task the source IS the deliverable, and a format
    # named there usually describes a feature ("build a zip export for my app"),
    # not the run's output. Only carry the requirement where the deliverable is
    # a document/artifact, so the delivery gate stays conservative.
    if formats and task_type == TaskType.code:
        notes.append(
            f"mentions {', '.join(formats)} inside a coding task — the source is "
            "the deliverable, so no produced-file requirement is recorded")
        formats = []
    elif formats:
        notes.append(f"deliverable format(s): {', '.join(formats)}")

    match_source = wants_matched_output(text)
    if match_source:
        notes.append("wants a MATCHED SET with a referenced source — structural "
                     "fidelity to that source is a hard requirement, not commentary")

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
        match_source=match_source,
        deliverable_formats=formats,
    )


def with_intent(
    cls: Classification,
    *,
    task_type: TaskType | None = None,
    produces_output: bool | None = None,
    deliverable_formats: list[str] | None = None,
    note: str = "",
) -> Classification:
    """Re-derive a Classification after the LLM intent pass revised the reading.

    ``classify`` computes half a dozen flags FROM ``task_type`` (tools_allowed,
    needs_facts, needs_design, produces_output, greenfield, skills_needed).
    Patching task_type with ``model_copy`` alone would leave those stale — a
    request re-typed from `code` to `content` would keep needs_design=True and
    an "architecture" skill it no longer wants. Everything derived is recomputed
    here so the two paths can never disagree.

    Text-derived signals (complexity, risk, match_source, needs_governance) are
    deliberately kept from the rule pass: they read the words, which is a job
    regex does honestly.
    """
    new_type = task_type or cls.task_type
    formats = list(cls.deliverable_formats if deliverable_formats is None
                   else deliverable_formats)
    # The source IS the deliverable on a software task, so a format named there
    # describes a feature, not an output requirement (see classify()).
    if new_type == TaskType.code:
        formats = []

    derived_output = new_type in (
        TaskType.code, TaskType.content, TaskType.design, TaskType.action)
    new_output = derived_output if produces_output is None else bool(produces_output)
    # A named non-text deliverable is a file by definition; never let a model's
    # produces_output=False strand a run that has to build a PDF.
    if formats:
        new_output = True

    skills: list[str] = []
    needs_facts = new_type in (
        TaskType.question, TaskType.research, TaskType.design,
        TaskType.content, TaskType.code)
    needs_design = new_type in (TaskType.design, TaskType.code)
    if needs_facts:
        skills.append("research")
    if needs_design:
        skills.append("architecture")
    if new_output:
        skills.append("implementation")
    if cls.quality_matters:
        skills.append("critique")

    rationale = cls.rationale
    if note:
        rationale = f"{rationale}; {note}" if rationale else note
    return cls.model_copy(update={
        "task_type": new_type,
        "produces_output": new_output,
        "deliverable_formats": formats,
        "tools_allowed": new_type in (
            TaskType.action, TaskType.code, TaskType.content, TaskType.design),
        "needs_facts": needs_facts,
        "needs_design": needs_design,
        "greenfield": cls.greenfield and new_type == TaskType.code,
        "skills_needed": skills,
        "rationale": rationale,
    })
