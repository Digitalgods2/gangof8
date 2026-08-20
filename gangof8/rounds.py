"""Round prompts and contracts — the text layer of the deliberation loop.

Everything here builds prompt strings or parses marker lines out of replies;
no orchestration, no side effects. loop.py imports from this module (never the
reverse), so the prompt layer stays cycle-free and independently testable.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

from .models import Contribution, Council, CouncilMember, Role, Session
from . import assembly, config
from .skills import capability_manifest
from .workbench import execution_text


def _execution_task(session: Session) -> str:
    """The original request plus its stable, user-editable outcome contract."""
    return execution_text(session.task.text, session.outcome_contract)


def _recent_context(session: Session, limit: int = 3) -> str:
    parts = [
        f"[{c.role.value} r{c.round}] {c.content[:700]}"
        for c in session.contributions[-limit:]
    ]
    return "\n".join(parts) if parts else "(none yet)"


# Every role sees this so non-implementer roles stop treating can_write_files /
# can_run_commands as a blocker and stop asking the human to "enable" them: file
# production is governed (implementer emits ARTIFACT, human approves the write).
_GOVERNANCE_CONTEXT = (
    "You operate inside a governed coordinator. You cannot and do not need to "
    "perform file writes, shell commands, or network calls yourself — ignore any "
    "can_write_files / can_run_commands permission flags. When the task needs a "
    "file, the implementer emits an 'ARTIFACT: <filename>' block and the human "
    "approves the write. Do NOT ask whether to enable write permissions; assume "
    "the governed write path exists and proceed with your role.\n"
    "READING FILES: you have NO direct filesystem access and do not need it — and "
    "this is NOT a restriction on what you can analyze. NEVER say you 'cannot "
    "access' a path or that a folder is 'outside your workspace'; that is false "
    "here. The coordinator reads files FOR you, including folders outside its own "
    "directory. To see a folder, emit 'SKILL: list_dir .'; to read a file, "
    "'SKILL: read_file <path>'; to search, 'SKILL: search_project <query>'. The "
    "results are handed back to you. Use these instead of refusing, and base your "
    "analysis on what they return — never invent file contents.\n"
    "You have NO native tools here, whatever your instincts say — tool-call "
    "syntax (Read/invoke blocks, file-path JSON) is ignored and wastes your "
    "turn. Plain-text SKILL: lines are the ONLY way to read; your reply text "
    "IS your entire contribution.\n"
)

# What a DELEGATED talent needs to know to author files that are captured
# directly (the lead's fuller _output_contract covers PROMOTE — a talent must
# not emit those; delivery stays the lead's decision and the human's gate).
DELEGATE_FILE_CONTRACT = (
    "If your assignment is to AUTHOR a file, emit it literally and COMPLETELY:\n"
    "ARTIFACT: <filename>\n"
    "<full file contents>\n"
    "END_ARTIFACT\n"
    "Raw bytes go between the ARTIFACT and END_ARTIFACT lines — no ``` fences. "
    "Any explanation must come after END_ARTIFACT. Your "
    "ARTIFACT/EDIT blocks are captured directly as real files in the council "
    "space. Do NOT emit PROMOTE lines — delivery is the lead's decision.\n"
)


def delegation_contract(council: "Council", role_agents: dict[Role, str] | None,
                        produces_output: bool = False) -> str:
    """The lead's ORCHESTRATOR charter + the talent menu with each seat's
    backing origin model. The lead organizes and integrates; the talents do
    the substantive work — coding, research, writing, verification."""
    mapping = role_agents or config.ROLE_AGENTS
    lines: list[str] = []
    for role, talent in config.TALENTS.items():
        member = council.get(role)
        origin = (member.agent if member else None) or mapping.get(role) or "?"
        lines.append(f"- {role.value} ({origin}): {talent}")
    menu = "\n".join(lines)
    # The file-authoring mechanics that follow this block are long, concrete and
    # imperative ("emit each file literally, with its COMPLETE contents"). Four
    # lines of "you are the organizer" lost that argument every time: the lead
    # authored whole deliverables alone while nine mapped talents across five
    # models sat idle. So state the DIVISION OF LABOUR explicitly, say that the
    # mechanics below belong to whoever authors, and show one worked example —
    # models copy a demonstrated shape far more reliably than an exhortation.
    author = (
        "\nTHIS TASK SHIPS FILES. The substantive authoring is a talent's job, "
        "not yours. Everything after this section describes HOW A FILE IS "
        "WRITTEN — it applies to whoever writes it, which for real content is "
        "the talent you delegate to. A delegated talent's ARTIFACT/EDIT blocks "
        "are captured directly as real files in the council space.\n"
        "Your own reply carries the ASSIGNMENTS, and later the governed lines "
        "only you may emit: BUILD/PRODUCES, INSTALL, PROMOTE.\n"
        "A first reply on a substantial build should look like this:\n"
        "  DELEGATE: researcher - gather the source material for X, with citations\n"
        "  DELEGATE: implementer - author the complete <filename> covering X\n"
        "  CONSULT: critic - what would make this deliverable fail acceptance?\n"
        "Then their work returns and you integrate, verify and deliver it.\n"
        "Author a file yourself ONLY when it is small glue, or when a talent "
        "already returned the content and you are assembling it.\n"
    ) if produces_output else ""
    return (
        "YOU are the LEAD — the organizer and integrator, NOT the doer. Break the "
        "task into focused assignments, hand each to the right talent below, then "
        "integrate their results and quality-gate the whole. Doing a substantive "
        "piece solo when a mapped talent exists for it is a FAILURE of your role, "
        "not efficiency: these are different models, and a second one seeing the "
        "work is the point. One line per assignment:\n"
        "CONSULT: <talent> - <specific question>   (advice/verification returns to you)\n"
        "DELEGATE: <talent> - <specific subtask>   (the talent produces that piece)\n"
        "Their results come back and you are re-called to integrate and finish. "
        "When results already appear below, integrate them NOW — do not "
        "re-request the same work.\n"
        f"Available talents:\n{menu}\n"
        f"{author}"
    )


_ROLE_INSTRUCTIONS: dict[Role, str] = {
    Role.knowledge_retriever: (
        "Seat charter: Knowledge Retriever. Gather evidence, not opinions. "
        "Every material claim should include a source such as file:line, URL, "
        "or an explicit 'NO SOURCE FOUND - assumption' marker. Prefer primary "
        "sources and local code over summaries."
    ),
    Role.researcher: (
        "Seat charter: Researcher. Interpret the gathered evidence for the "
        "user's R&D goal. Separate established facts from implications and "
        "assumptions."
    ),
    Role.architect: (
        "Seat charter: Architect. Turn evidence into a coherent system design "
        "that fits this app's actual constraints and avoids wrong-altitude work."
    ),
    Role.code_generator: (
        "Seat charter: Code Generator. Propose modular implementation units, "
        "interfaces, and integration points that can be turned into concrete "
        "artifacts. Match the existing app style."
    ),
    Role.api_integrator: (
        "Seat charter: API Integrator. Define external integration contracts: "
        "endpoint/auth/request/response/error behavior. Mark unknowns instead "
        "of inventing API details."
    ),
    Role.critic: (
        "Seat charter: Critic. Challenge the strongest claim or implementation "
        "risk, concretely and with evidence."
    ),
    Role.red_team: (
        "Seat charter: Red Team. Try to break the proposal with adversarial "
        "cases, bad assumptions, security/privacy failure modes, and model "
        "hallucination risk."
    ),
    Role.fact_validator: (
        "Seat charter: Fact Validator. Independently verify the claims made so "
        "far, naming the checked source or the reason a claim can't be verified."
    ),
    Role.implementer: (
        "Seat charter: Implementer. Produce concrete deliverables and complete "
        "file artifacts when the task calls for them."
    ),
    Role.summarizer: (
        "Seat charter: Synthesizer. Resolve conflicts explicitly and separate "
        "established truth from assumptions and proposals."
    ),
}


def role_instruction(role: Role) -> str:
    return _ROLE_INSTRUCTIONS.get(role, f"Seat charter: {role.value}.")


def _capability_index() -> dict[str, dict]:
    """Reader-safe live catalogue keyed for prompt construction."""
    manifest = capability_manifest()
    capabilities = manifest.get("capabilities", [])
    if not isinstance(capabilities, list):
        return {}
    return {
        item["name"]: item
        for item in capabilities
        if isinstance(item, dict) and isinstance(item.get("name"), str)
    }


def _hint_capability(
    catalogue: dict[str, dict],
    name: str,
    role: Role,
    available_spaces: set[str],
) -> Optional[dict]:
    """Return a safe, positional-SKILL-compatible discovery capability."""
    capability = catalogue.get(name)
    if not capability:
        return None
    if capability.get("requires_approval") or capability.get("mutates"):
        return None
    if capability.get("category") not in {"read", "web"}:
        return None
    if role.value not in capability.get("allowed_roles", []):
        return None
    inputs = capability.get("inputs")
    primary = capability.get("primary_input")
    if not isinstance(inputs, list) or (primary and primary not in inputs):
        return None
    spaces = capability.get("permitted_spaces")
    if not isinstance(spaces, list):
        return None
    if spaces and not available_spaces.intersection(spaces):
        return None
    return capability


def _has_bound_git_repository(session: Session) -> bool:
    """A cheap prompt-time check; the handler performs the security validation."""
    for raw in (session.workspace_root, session.established_root):
        if not raw:
            continue
        try:
            marker = Path(raw).resolve() / ".git"
            if marker.is_dir() and not marker.is_symlink():
                return True
        except (OSError, RuntimeError):
            continue
    return False


def _skill_hints(session: Session, role: Role, readable: list[str] = ()) -> str:
    """Advertise catalogue-declared, no-approval discovery capabilities."""
    has_dir = bool(session.established_root or session.workspace_root)
    where = "established folder" if session.established_root else "project"
    available_spaces = {"sandbox"}
    if session.workspace_root:
        available_spaces.add("workspace")
    if session.established_root:
        available_spaces.add("established")
    catalogue = _capability_index()
    hints: list[str] = []
    ld = _hint_capability(catalogue, "list_dir", role, available_spaces)
    if has_dir and ld:
        hints.append(
            f"list the {where}'s files with a line 'SKILL: list_dir .' (use a subfolder to drill in)"
        )
    sp = _hint_capability(catalogue, "search_project", role, available_spaces)
    if has_dir and sp:
        hints.append(f"search the {where} with a line 'SKILL: search_project <query>'")
    rf = _hint_capability(catalogue, "read_file", role, available_spaces)
    if (readable or has_dir) and rf:
        avail = f" Available now: {', '.join(readable)}." if readable else ""
        hints.append(f"read a file with a line 'SKILL: read_file <path>'.{avail}")
    gs = _hint_capability(catalogue, "git_snapshot", role, available_spaces)
    if has_dir and gs and _has_bound_git_repository(session):
        hints.append(
            "inspect the bound Git repository with a line "
            "'SKILL: git_snapshot .' (use a contained repository subfolder "
            "instead of '.' when needed)"
        )
    ws = _hint_capability(catalogue, "web_search", role, available_spaces)
    wf = _hint_capability(catalogue, "web_fetch", role, available_spaces)
    if config.WEB_ENABLED and ws and wf:
        hints.append("search the live web with 'SKILL: web_search <query>' "
                     "(and read a page with 'SKILL: web_fetch <url>')")
    elif config.WEB_ENABLED and ws:
        hints.append("search the live web with 'SKILL: web_search <query>'")
    elif config.WEB_ENABLED and wf:
        hints.append("read a public page with 'SKILL: web_fetch <url>'")
    if not hints:
        return ""
    return "You may " + "; ".join(hints) + " (results are returned to you, no approval needed).\n"


# The file-output contract: how the lead writes real files. Lifted from the old
# implementer draft prompt so the materialize path stays consistent.
def _output_contract(session: Session) -> str:
    if session.established_root:
        promote = (
            f"\nThis task targets the EXISTING folder: {session.established_root}\n"
            "Your ARTIFACT/EDIT blocks are written into your own sandbox FREELY (no "
            "approval) so you can build and test. NOTHING reaches the real folder until "
            "you PROMOTE it AND the human approves. For each file that should land in "
            "the real folder, add a line:\n"
            "PROMOTE: <filename>   (one per file you want delivered)\n"
            "This applies on FOLLOW-UPS too: if you revise a file that was already "
            "delivered, you MUST re-emit its PROMOTE line — an ARTIFACT/EDIT alone only "
            "updates your sandbox copy, so without PROMOTE the user's file keeps the OLD "
            "version and your change never reaches them.\n"
        )
    else:
        promote = (
            "\nYour ARTIFACT/EDIT blocks are written into your own sandbox FREELY (no "
            "approval needed) — you need no filesystem access yourself.\n"
            "If the finished files should be DELIVERED to a real folder of the user's, "
            "add a line per file:\n"
            "PROMOTE: <filename>\n"
            "The coordinator will ask the user for the destination and get their "
            "approval before anything lands.\n"
        )
    # The BUILD paragraph below is easy to read as optional. When the user NAMED
    # the output format, say so first and say it as a requirement: the live
    # failure was a seat that authored a perfect 49KB PDF generator, emitted
    # PROMOTE for the script, and called the cookbook delivered.
    formats = list(getattr(session.classification, "deliverable_formats", None) or [])
    if formats:
        names = ", ".join(f".{fmt}" for fmt in formats)
        required = (
            f"THE DELIVERABLE OF THIS TASK IS A {names} FILE. ARTIFACT holds text and "
            f"nothing else, so nobody can type it out. A generator alone does not "
            f"satisfy the task: once the generator exists — whoever authored it — the "
            f"run must RUN it via INSTALL (if it needs packages) and BUILD/PRODUCES "
            f"naming the {names} file, and PROMOTE that file rather than the script. "
            f"Those governed lines are YOURS to emit as lead. A run that ends holding "
            f"only source code has produced nothing and the delivery gate will fail "
            f"it.\n\n"
        )
    else:
        required = ""
    return (
        required
        + "FILE-WRITING MECHANICS (these apply to WHOEVER authors a file — a talent "
        "you delegated to, or you when it is glue):\n"
        + "If the task needs files (code, docs, config), emit each file literally in "
        "this format, with its COMPLETE contents — never a summary of what the file "
        "would contain:\n"
        "ARTIFACT: <filename>\n"
        "<full file contents>\n"
        "END_ARTIFACT\n"
        "Use one ARTIFACT block per file and include EVERY file the task asks for. Do "
        "not describe the files in prose — write them out in full.\n"
        "Write each file's RAW bytes immediately after its ARTIFACT line and terminate "
        "that file with a line containing exactly END_ARTIFACT. Do NOT wrap file "
        "contents in ``` markdown code fences. A short rationale before the first "
        "ARTIFACT or an explanation after END_ARTIFACT is safe; neither becomes file "
        "content.\n"
        f"{promote}"
        "To MODIFY an existing file instead of overwriting it, emit a surgical edit "
        "(the OLD snippet must be unique in the file):\n"
        "EDIT: path/to/file.py\n"
        "<<<<<<< OLD\n<exact existing text>\n=======\n<replacement text>\n>>>>>>> NEW\n"
        "For a static parse/compile check, add one of these auto-safe lines:\n"
        "RUNTESTS: node --check path/to/file.js\n"
        "RUNTESTS: python -m py_compile path/to/file.py\n"
        "Functional RUNTESTS commands require explicit human approval and must name the command.\n"
        "ARTIFACT can only carry TEXT. When the deliverable is a file you cannot type "
        "out — a PDF, an image, an archive — author the generator as an ARTIFACT and "
        "then build it, naming every file the build must produce:\n"
        "BUILD: python make_book.py --in book.md --out book.pdf\n"
        "PRODUCES: book.pdf\n"
        "The build runs in the council space after explicit human approval; each "
        "PRODUCES file must really appear or the build fails. Never claim a binary "
        "deliverable you did not produce this way — describing one does not create it.\n"
        "If the build needs third-party packages, ask for them by name on their own "
        "line — the user approves the packages separately from the build:\n"
        "INSTALL: reportlab, pdfplumber\n"
        "Names with an optional single version bound only (reportlab>=4.2). URLs, "
        "VCS refs, paths and pip options are refused. Packages install for this "
        "session alone, not into the coordinator's environment.\n"
    )


def revision_patch_prompt(session: Session, targets: list[str], source_context: str) -> str:
    """One grounded author edits an existing deliverable without rewriting it.

    Best-of-N is useful for greenfield alternatives.  It is counterproductive
    for a 40–100 KB established file: every seat must reconstruct the whole
    program, then judges compare mostly duplicated bytes.  Revision work gets a
    single author, an exact sandbox copy, and a bounded review instead.
    """
    names = ", ".join(targets)
    return (
        f"Task: {_execution_task(session)}\n"
        f"{_GOVERNANCE_CONTEXT}"
        "You are the PRIMARY REVISION AUTHOR. This is an in-place change, not a "
        "greenfield rewrite. The exact current file has already been copied into "
        "the council sandbox. Preserve every unrelated behavior, public API, and "
        "existing game/module. Implement only the requested milestone.\n\n"
        f"Revision target(s): {names}\n"
        "Return one or more surgical edits ONLY, using this exact format:\n"
        "EDIT: path/to/file\n"
        "<<<<<<< OLD\n<unique exact text from the current file>\n=======\n"
        "<replacement text>\n>>>>>>> NEW\n\n"
        "Do NOT emit a complete ARTIFACT replacement. Do NOT remove or rename an "
        "existing public class/function/registration unless the task explicitly "
        "requires it. The coordinator applies these edits to the sandbox copy, "
        "runs its browser smoke/behavior checks, and asks a reviewer to inspect "
        "the patch before any delivery.\n\n"
        "CURRENT SOURCE (authoritative; edit this exact version):\n"
        f"-----\n{source_context}\n-----"
    )


def revision_review_prompt(session: Session, targets: list[str], patch_summary: str,
                           assertions: list[str]) -> str:
    """Ask one reviewer to inspect a narrow revision, not author a rival file."""
    required = "\n".join(f"- {item}" for item in assertions) or "- preserve existing public APIs"
    return (
        f"Task: {_execution_task(session)}\n"
        "You are the REVISION REVIEWER. Inspect the applied patch below against "
        "the task. Do not write code and do not propose a full rewrite. Identify "
        "only release-blocking defects: broken integration, lost existing APIs, "
        "or a requested behavior that is clearly absent.\n\n"
        f"Targets: {', '.join(targets)}\n"
        f"Required behavior/API signals:\n{required}\n\n"
        f"Applied patch:\n-----\n{patch_summary}\n-----\n\n"
        "End with exactly one of:\n"
        "REVIEW: PASS\n"
        "REVIEW: FAIL - <specific, actionable defect>"
    )


def revision_repair_prompt(session: Session, report: str, source_context: str) -> str:
    return (
        f"Task: {_execution_task(session)}\n"
        "A reviewer found this release-blocking defect in your in-place patch:\n"
        f"{report}\n\n"
        "Return ONLY the additional surgical EDIT block(s) needed to correct it; "
        "do not rewrite the file. The current sandbox source is:\n-----\n"
        f"{source_context}\n-----"
    )


def revision_review_failures(text: str) -> list[str]:
    """Extract explicit blocking findings; unstructured prose is advisory."""
    failures: list[str] = []
    for line in (text or "").splitlines():
        m = re.match(r"^\s*REVIEW\s*:\s*FAIL\s*(?:[-:—]\s*)?(.*)$", line, re.I)
        if m:
            detail = m.group(1).strip()
            failures.append(detail or "reviewer rejected the patch")
    return failures


def lead_prompt(
    session: Session, council: "Council", role_agents: dict[Role, str] | None,
    established_overview: str = "", readable: list[str] = (),
) -> str:
    """The single prompt that drives a task. The lead does the work directly and
    may pull in talents on demand. For output tasks it includes the file contract;
    for pure questions it just asks for the answer."""
    produces = bool(session.classification and session.classification.produces_output)
    overview = f"{established_overview}\n\n" if established_overview else ""
    contract = _output_contract(session) if produces else (
        "Produce the actual answer the task asks for — complete, concrete, and "
        "ready to use. Do not ask the human about details you can reasonably "
        "assume (styling, length, tone, naming) — state the assumption and "
        "answer. The one exception: if the request genuinely reads two ways and "
        "the two readings produce DIFFERENT deliverables, ask that single "
        "question instead of guessing.\n"
    )
    return (
        f"Task: {_execution_task(session)}\n"
        f"{_GOVERNANCE_CONTEXT}"
        "Your role: lead. You own the OUTCOME end to end: organize the work, "
        "assign it, integrate the results, and deliver the real, working result — "
        "not a plan or a description of it.\n"
        f"{delegation_contract(council, role_agents, produces_output=produces)}"
        f"{contract}"
        f"{_skill_hints(session, Role.lead, readable)}"
        f"{overview}"
        f"Context so far:\n{_recent_context(session, limit=5)}"
    )


# ---------------------------------------------------------------------------
# Panel rounds: every enabled seat contributes in parallel, then the lead
# synthesizes and declares the round DONE or CONTINUE.
# ---------------------------------------------------------------------------

# Panel seats have the same governed discovery skills as the lead (SKILL:
# lines, chained resolution in _panel_one) and their complete files are saved
# into the council sandbox namespaced per seat — real read/write access to the
# council space. Delivery stays with the lead: panel files are advisory drafts
# it reviews; only the lead PROMOTEs.
_PANEL_CONTEXT = (
    "You operate inside a governed coordinator with no filesystem or network "
    "access of your own — but the coordinator READS FOR YOU: emit a plain-text "
    "line 'SKILL: read_file <path>' / 'SKILL: search_project <query>' / "
    "'SKILL: list_dir .' and the results are handed back to you. Do not refuse "
    "for lack of access — request what you need, or reason from what is "
    "provided and state assumptions where something is missing.\n"
    "You have NO native tools in this environment, even if your instincts say "
    "otherwise: do NOT emit tool-use syntax (Read/invoke blocks, file-path "
    "JSON) — nothing executes it and your seat's contribution would be lost. "
    "Plain-text SKILL: lines are the only way to read; your reply text IS your "
    "entire contribution.\n"
)

_ROUND_CONTRACT = (
    "\nEnd your reply with exactly one line:\n"
    "ROUND: DONE                        (the task is complete — your reply above IS the result)\n"
    "ROUND: CONTINUE - <what is still open for the next round>\n"
    "Declare CONTINUE only when another round of panel input would materially "
    "improve the result. If you emit neither line, DONE is assumed.\n"
)

# 'ROUND: DONE' / 'ROUND: CONTINUE - why' in the same envelope-surviving style
# as ARTIFACT:/CONSULT: (bullets, bold, and :—–- separators tolerated).
_ROUND_MARKER = re.compile(
    r"^\s*(?:[-*•]\s*)?(?:\*\*)?ROUND(?:\*\*)?\s*[:—–-]\s*"
    r"(?:\*\*)?(?P<decision>DONE|CONTINUE)\b"
    r"(?:\s*[-–—:]\s*(?P<why>.*?))?\s*(?:\*\*)?\s*$",
    re.IGNORECASE | re.MULTILINE,
)


# First-person future intent — the signature of a reply that defers the work
# ("I'll read the core source files ... then deliver the recommendation set").
_DEFERRAL_RE = re.compile(
    r"\b(?:I(?:'|’| wi)ll|let me|I am going to|I'm going to|going to start)\b",
    re.IGNORECASE,
)
# Any contract marker line — a reply carrying one of these IS doing the work
# (or legitimately requesting it), regardless of length.
_ANY_MARKER_RE = re.compile(
    r"^\s*(?:[-*•]\s*)?(?:\*\*)?"
    r"(?:ARTIFACT|EDIT|RUN_?TESTS|PROMOTE|CONSULT|DELEGATE|SKILL|ROUND)"
    r"(?:\*\*)?\s*[:—–-]",
    re.IGNORECASE | re.MULTILINE,
)
# Tool-use ATTEMPTS rendered as text: a CLI agent trying to call its native
# tools instead of contributing (claude's blocked Read calls appear as
# '<summary>Read x</summary>' + a bare {"file_path": ...} JSON block; raw
# invoke/parameter XML is the same instinct). Nothing executes these — they
# are debris, not content, no matter how many chars they add up to.
_TOOL_DEBRIS_RE = re.compile(
    r"<summary>.*?</summary>"
    r"|<invoke\b.*?(?:</invoke>|\Z)"
    r"|<parameter\b.*?(?:</parameter>|\Z)"
    r"|^[ \t]*\{[^{}]*\"(?:file_path|command|pattern|query|path)\"[^{}]*\}[ \t]*$",
    re.IGNORECASE | re.DOTALL | re.MULTILINE,
)


# A SKILL: request line, whole. A legitimate work-in-progress marker BEFORE the
# resolver runs; dead weight after — a line still standing then is a request
# that will never be honored.
_SKILL_LINE_RE = re.compile(
    r"^\s*(?:[-*•]\s*)?(?:\*\*)?SKILL(?:\*\*)?\s*[:—–-].*$",
    re.IGNORECASE | re.MULTILINE,
)


def reply_is_stub(content: str, skills_resolved: bool = False) -> bool:
    """True when a reply merely announces or ATTEMPTS the work instead of doing
    it: tool-call debris or first-person deferral phrasing, with too little
    real prose left over. A contract marker (SKILL:/CONSULT:/ARTIFACT:/ROUND:)
    always counts as real work; a genuinely short direct answer with no
    deferral phrasing is NOT a stub.

    skills_resolved: pass True when the skill resolver has ALREADY run on this
    reply. Any SKILL: line still standing then is an unresolved request, not
    work — a live round ended on the bare line 'SKILL: search_project …'
    accepted as the synthesis, and nothing was delivered. With the flag set,
    those lines count as debris instead of blessing the reply."""
    text = (content or "").strip()
    skill_lines = 0
    if skills_resolved:
        text, skill_lines = _SKILL_LINE_RE.subn("", text)
        text = text.strip()
    if _ANY_MARKER_RE.search(text):
        return False
    prose, debris = _TOOL_DEBRIS_RE.subn("", text)
    prose = prose.strip()
    if len(prose) >= config.SYNTHESIS_STUB_CHARS:
        return False
    return (debris + skill_lines) > 0 or bool(_DEFERRAL_RE.search(prose))


def parse_round_decision(text: str) -> tuple[str, str]:
    """The lead's round verdict. No marker ⇒ DONE, so a marker-ignoring model
    degrades to a single-pass run — never a runaway loop. The LAST marker wins
    (the contract says to end with it)."""
    matches = list(_ROUND_MARKER.finditer(text or ""))
    if not matches:
        return "DONE", ""
    m = matches[-1]
    return m.group("decision").upper(), (m.group("why") or "").strip()


# A delegated specialist ends its reply with a RESULT: block — the part
# guaranteed to survive folding. Blind head-truncation used to cut a reply from
# the END, which is exactly where a well-written answer puts its conclusion.
_RESULT_MARKER = re.compile(
    r"^[ \t]*(?:\*\*)?RESULT(?:\*\*)?[ \t]*:", re.IGNORECASE | re.MULTILINE)

RESULT_CONTRACT = (
    "Finish your reply with this block — it is the ONLY part guaranteed to "
    "survive truncation when your answer is folded back to the requester:\n"
    "RESULT:\n"
    "finding: <your answer, 3-6 sentences>\n"
    "artifacts: <sandbox filenames you produced, or none>\n"
    "confidence: high|medium|low"
)


def split_result_block(text: str) -> tuple[str, str]:
    """(preamble, result_block): the block runs from the LAST 'RESULT:' line to
    the end; result_block is '' when the reply didn't use the contract."""
    matches = list(_RESULT_MARKER.finditer(text or ""))
    if not matches:
        return (text or "", "")
    m = matches[-1]
    return text[: m.start()], text[m.start():]


def strip_round_marker(text: str) -> str:
    """The ROUND: DONE/CONTINUE line is loop control, not content — remove it
    when the synthesis text is used verbatim (e.g. as the final answer)."""
    return _ROUND_MARKER.sub("", text or "").strip()


def round_context(session: Session, seat_agent: str, round_idx: int) -> str:
    """What a panel seat carries into round N: the lead's prior synthesis, its
    own prior take, and the peers' — all capped. Derived purely from
    session.contributions so a resumed session rebuilds it exactly."""
    if round_idx <= 0:
        return ""
    prev = [c for c in session.contributions if c.round == round_idx - 1]
    parts: list[str] = []
    synth = next((c for c in reversed(prev) if c.role == Role.lead), None)
    if synth:
        parts.append("LEAD SYNTHESIS of the previous round:\n"
                     + synth.content[:config.SYNTHESIS_CARRYOVER_CHARS])
    own = next((c for c in reversed(prev)
                if c.role == Role.panelist and c.agent == seat_agent), None)
    if own:
        parts.append("YOUR OWN prior take (build on it, don't repeat it):\n"
                     + own.content[:config.PANEL_CARRYOVER_CHARS])
    peers = [c for c in prev if c.role == Role.panelist and c.agent != seat_agent]
    if peers:
        parts.append("PEER SEATS' prior takes:\n" + "\n".join(
            f"[{c.agent}] {c.content[:config.PEER_CARRYOVER_CHARS]}" for c in peers))
    return "\n\n".join(parts)


def _package_task_context(session: Session) -> str:
    """Return the package/interface portion of a composed goal task.

    Goal sessions carry the full product brief for traceability.  Repeating
    that entire brief to every file author needlessly multiplies context and
    makes a focused implementation call slower.  The goal composer already
    emits semantic section markers, so trim by those sections rather than by a
    made-up character or byte limit.  Legacy/unmarked tasks remain unchanged.
    """
    text = _execution_task(session)
    for marker in (
        "NON-BLOCKING INTERFACE INPUTS",
        "THE PACKAGE TO COMPLETE NOW",
    ):
        position = text.find(marker)
        if position >= 0:
            return text[position:].strip()
    return text.strip()


def package_output_prompt(
    session: Session,
    member: CouncilMember,
    round_idx: int,
    assigned_files: list[str],
    output_authors: dict[str, str],
    feedback: str = "",
    staged_context: str = "",
) -> str:
    """Focused exact-output authoring prompt for an accountable work package."""
    assigned = [name.replace("\\", "/") for name in assigned_files]
    all_outputs = [name.replace("\\", "/") for name in session.required_files]
    owner = session.work_package_owner
    assignment_lines = "\n".join(
        f"- {name}: {output_authors.get(name, owner)}" for name in all_outputs
    )
    responsibility = (
        "You are the accountable package owner and the primary author of the exact "
        "outputs assigned to you."
        if member.agent == owner else
        f"You are an implementation sub-agent working under accountable owner {owner}."
    )
    retry = (
        "\nTARGETED CORRECTION FROM THE COORDINATOR:\n" + feedback.strip() + "\n"
        if feedback.strip() else ""
    )
    accepted_context = (
        "\nACTUAL ACCEPTED STAGING BYTES AND API SURFACES:\n"
        + staged_context.strip()
        + "\n\nTreat these bytes as authoritative. The prose interface is only a "
          "summary; match the implemented signatures, units, DOM hooks, and load "
          "order exactly.\n"
        if staged_context.strip() else ""
    )
    if session.assembly_mode == assembly.HTML_INLINE:
        sources = [
            name for name in session.runtime_dependencies
            if name != session.assembly_template
        ]
        implementation_contract = (
            "This is a compact deterministic HTML integration template. Do not read "
            "or copy dependency bodies. Place each literal directive exactly once at "
            "the correct style/script location and load order:\n"
            + assembly.directive_contract(sources)
            + "\nAuthor only the document structure, required DOM IDs, accessibility/meta "
              "markup, and minimal bootstrap glue. Do not wrap directives in style or "
              "script tags.\n"
        )
    else:
        implementation_contract = (
            "Implement only your assigned outputs. Coordinate through the declared "
            "interfaces and sibling-output map; do not recreate another author's file.\n"
        )
        if any(name.endswith((".template.html", ".template.htm")) for name in assigned):
            implementation_contract += (
                "Every GANGOF8:STYLE or GANGOF8:SCRIPT directive in an HTML template "
                "must be a literal standalone line. Never place a directive inside an "
                "existing <style> or <script> element: the coordinator expands each "
                "directive into a complete element of that kind.\n"
            )
    exact = ", ".join(assigned)
    return (
        f"{_GOVERNANCE_CONTEXT}"
        f"Work package: {session.work_package_id or round_idx + 1}\n"
        f"Accountable owner: {owner}\n"
        f"Origin model for this author: {member.agent}\n"
        f"{responsibility}\n\n"
        "PACKAGE AND INTERFACE CONTEXT:\n"
        f"{_package_task_context(session)}\n\n"
        f"{accepted_context}"
        "EXACT OUTPUT ASSIGNMENTS:\n"
        f"{assignment_lines}\n\n"
        f"YOUR ASSIGNED OUTPUTS: {exact}\n"
        f"{implementation_contract}"
        "For every assigned output, emit exactly one complete block:\n"
        "ARTIFACT: <exact assigned relative filename>\n"
        "<full file contents>\n"
        "END_ARTIFACT\n"
        "Use raw contents without code fences. Do not emit PROMOTE, a plan, a status "
        "update, SKILL requests, or files assigned to another author.\n"
        f"{retry}"
    )


_COLLABORATION_LENS = {
    "architecture": (
        "Challenge the structure, state model, separation of responsibilities, "
        "and whether the implementation can satisfy the package contract cleanly."
    ),
    "correctness": (
        "Trace the important runtime paths and find concrete logic, completeness, "
        "usability, or accessibility defects."
    ),
    "integration": (
        "Check APIs, event flow, DOM hooks, timing units, persistence, load order, "
        "and every boundary with accepted dependencies."
    ),
    "adversarial": (
        "Attack edge cases, invalid states, rapid/repeated input, lifecycle races, "
        "failure recovery, and assumptions likely to break under real use."
    ),
    "implementation": (
        "Use your coding ability to propose a simpler, faster, or more robust "
        "implementation of weak portions while preserving the requested behavior."
    ),
    "verification": (
        "Independently verify stated requirements against the actual code and patch "
        "any mismatch you can prove from the artifact."
    ),
    "independent": (
        "Perform an independent code challenge without trusting the owner's design; "
        "look for high-impact defects and provide exact corrective edits."
    ),
}


def _collaboration_artifacts(artifacts: dict[str, str]) -> str:
    return "\n\n".join(
        f"===== ACTUAL BASELINE FILE: {filename} =====\n{content}\n"
        f"===== END BASELINE FILE: {filename} ====="
        for filename, content in artifacts.items()
    )


def package_collaboration_prompt(
    session: Session, member: CouncilMember, lens: str,
    artifacts: dict[str, str], staged_context: str = "",
) -> str:
    """Give one resource the owner's real baseline and demand usable patches."""
    focus = _COLLABORATION_LENS.get(lens, _COLLABORATION_LENS["independent"])
    allowed = ", ".join(artifacts)
    dependencies = (
        "ACTUAL ACCEPTED DEPENDENCY CONTEXT:\n" + staged_context.strip() + "\n\n"
        if staged_context.strip() else ""
    )
    return (
        f"{_GOVERNANCE_CONTEXT}"
        f"You are resource model {member.agent} in a full-council build. The "
        f"accountable file owner is {session.work_package_owner}; you are a code "
        "challenger, not a replacement owner.\n\n"
        f"PACKAGE CONTRACT:\n{_package_task_context(session)}\n\n"
        f"{dependencies}"
        f"YOUR REVIEW LENS ({lens.upper()}):\n{focus}\n\n"
        "Review the ACTUAL baseline bytes below. Do not return a plan, ask for "
        "tools, or merely suggest that someone inspect something later. If you "
        "find a defect, provide a deterministic OLD/NEW edit against an allowed "
        "file. Preserve unrelated behavior.\n\n"
        f"Allowed files: {allowed}\n\n"
        f"{_collaboration_artifacts(artifacts)}\n\n"
        "Reply in this contract:\n"
        "VERDICT: PASS or CHANGES\n"
        "FINDING: <one concrete, evidence-based finding>\n"
        "FINDING: <another finding, if needed>\n"
        "EDIT: <exact allowed filename>\n"
        "<<<<<<< OLD\n<text occurring exactly once>\n=======\n<replacement>\n>>>>>>> NEW\n"
        "Repeat EDIT blocks as needed. A PASS needs no EDIT. Do not emit ARTIFACT, "
        "PROMOTE, SKILL, CONSULT, or DELEGATE lines."
    )


def package_collaboration_integration_prompt(
    session: Session, artifacts: dict[str, str], contributions: list[tuple[str, str]],
    staged_context: str = "",
) -> str:
    """Require the accountable owner to reconcile every peer contribution."""
    reviews = "\n\n".join(
        f"===== RESOURCE CONTRIBUTION: {seat} =====\n{content}\n"
        f"===== END RESOURCE CONTRIBUTION: {seat} ====="
        for seat, content in contributions
    )
    seats = ", ".join(seat for seat, _ in contributions)
    dependencies = (
        "ACTUAL ACCEPTED DEPENDENCY CONTEXT:\n" + staged_context.strip() + "\n\n"
        if staged_context.strip() else ""
    )
    return (
        f"{_GOVERNANCE_CONTEXT}"
        f"You are the accountable package owner ({session.work_package_owner}). "
        "The enabled resource council has challenged your actual baseline. Reconcile "
        "their findings now; you retain sole ownership of the final bytes.\n\n"
        f"PACKAGE CONTRACT:\n{_package_task_context(session)}\n\n"
        f"{dependencies}"
        f"{_collaboration_artifacts(artifacts)}\n\n"
        f"RESOURCE CONTRIBUTIONS:\n{reviews}\n\n"
        "For EACH resource listed below, emit exactly one disposition line:\n"
        "DISPOSITION: <seat> | ACCEPT, REJECT, or SUPERSEDE | <specific reason>\n"
        f"Required seats: {seats}\n\n"
        "Then emit every final package file in full, even when unchanged:\n"
        "ARTIFACT: <exact package relative filename>\n"
        "<complete integrated contents>\n"
        "END_ARTIFACT\n"
        "Use raw contents without fences. Do not emit PROMOTE, SKILL, CONSULT, or "
        "DELEGATE lines. Do not invent additional files."
    )


_COLLABORATION_FINDING_RE = re.compile(
    r"^\s*FINDING\s*:\s*(.+?)\s*$", re.IGNORECASE | re.MULTILINE,
)
_COLLABORATION_DISPOSITION_RE = re.compile(
    r"^\s*DISPOSITION\s*:\s*([\w.\-]+)\s*\|\s*"
    r"(ACCEPT|REJECT|SUPERSEDE)\s*\|\s*(.+?)\s*$",
    re.IGNORECASE | re.MULTILINE,
)


def parse_collaboration_findings(text: str) -> list[str]:
    return [" ".join(match.group(1).split())[:500]
            for match in _COLLABORATION_FINDING_RE.finditer(text or "")]


def parse_collaboration_dispositions(text: str) -> dict[str, str]:
    return {
        match.group(1): (
            f"{match.group(2).upper()} | {' '.join(match.group(3).split())[:500]}"
        )
        for match in _COLLABORATION_DISPOSITION_RE.finditer(text or "")
    }


def panel_prompt(
    session: Session, member: CouncilMember, round_idx: int,
    established_overview: str = "", readable: list[str] = (),
) -> str:
    """One seat's independent take. Panel seats have the coordinator's
    discovery skills and may include complete files (saved to the council
    sandbox, namespaced per seat, for the lead to review); delivery decisions
    stay with the lead."""
    overview = f"{established_overview}\n\n" if established_overview else ""
    ctx = round_context(session, member.agent, round_idx)
    ctx_block = f"{ctx}\n\n" if ctx else ""
    # only later rounds have a synthesis/peer takes to push back on
    disagree = (
        "anything the other seats appear to have missed. Be concrete and commit "
        "to positions; where you disagree with the synthesis or the peer takes "
        "below, say so and give evidence. "
        if ctx else
        "the pitfalls a first answer typically misses. Be concrete and commit "
        "to positions. "
    )
    return (
        f"Task: {_execution_task(session)}\n"
        f"{_PANEL_CONTEXT}"
        f"You are one seat on a multi-model council (round {round_idx + 1}; your "
        f"origin model: {member.agent}). Give YOUR best independent take on the "
        "task: the answer or design as you see it, the strongest objections, and "
        f"{disagree}"
        f"{_panel_file_contract(session)}"
        "Do not ask the human about details you can reasonably assume — state "
        "the assumption and proceed. Ask only if the request reads two ways and "
        "the readings produce DIFFERENT deliverables.\n"
        f"{_skill_hints(session, Role.panelist, readable)}"
        f"{overview}"
        f"{ctx_block}"
    )


def _panel_file_contract(session: Session) -> str:
    """What a panel seat should do about FILES. Best-of-N: on a file-producing
    build every seat authors its COMPLETE candidate implementation — the
    candidates are then scored blindly and the best one SHIPS unchanged, so a
    full draft is the point, not waste (the old design-only greenfield rule was
    correct only while the lead re-authored; now the winning draft is the
    deliverable). Non-output tasks stay prose."""
    produces = bool(session.classification and session.classification.produces_output)
    if not produces:
        return ""
    if session.collaboration_mode == "build_team" and session.work_package_owner:
        required = ", ".join(session.required_files) or "the package result"
        if session.assembly_mode == assembly.HTML_INLINE:
            sources = [
                name for name in session.runtime_dependencies
                if name != session.assembly_template
            ]
            directives = assembly.directive_contract(sources)
            return (
                "You own only the compact HTML integration template. The coordinator "
                "already has every accepted dependency and will expand it directly; "
                "do NOT request/read dependency files and do NOT copy any CSS or "
                "JavaScript body into your response. Produce the required complete HTML "
                f"document skeleton as ARTIFACT: {required}, placing each of these "
                "literal standalone directives exactly once at the correct style/script "
                "location and JavaScript load order:\n"
                f"{directives}\n"
                "The coordinator replaces each directive with a classic inline tag and "
                "the full hash-verified staged source. Author only DOM structure, IDs, "
                "accessibility/meta markup, and minimal bootstrap glue required by the "
                "task. Do not wrap a directive inside <style> or <script>. Emit the "
                "complete compact template now using the normal ARTIFACT/END_ARTIFACT "
                "envelope; no plan, SKILL lines, or PROMOTE.\n"
            )
        return (
            f"You are the ACCOUNTABLE OWNER of this build package, not a candidate in a "
            f"contest. Produce the substantive implementation assigned to you now. "
            f"The coordinator may assign exact sibling outputs to implementation "
            f"sub-agents while you retain package accountability. Required staged "
            f"outputs: {required}. Emit every file as —\n"
            "ARTIFACT: <exact relative filename>\n<full file contents>\nEND_ARTIFACT\n"
            "— raw bytes inside that envelope, with no fences. Do not emit "
            "PROMOTE: package files remain in shared staging until one final batch "
            "release. Honor other owners' interfaces and do not recreate their files.\n"
        )
    return (
        "This is a BEST-OF-N build: emit YOUR COMPLETE candidate implementation "
        "as a block —\n"
        "ARTIFACT: <filename>\n<full file contents>\nEND_ARTIFACT\n"
        "— raw bytes inside that envelope (no ``` fences). Every "
        "seat's candidate is saved and then SCORED by independent judges; the "
        "single highest-scoring file is shipped to the user UNCHANGED, so make "
        "yours complete, correct, and the one you'd want to win — not a sketch. "
        "Do NOT emit PROMOTE lines (delivery is decided by the scored vote). A "
        "short rationale before the ARTIFACT line is welcome.\n"
    )


CANDIDATE_CRITERIA = (
    "completeness (no TODOs, stubs, or truncation — the file must be whole and "
    "runnable), correctness by inspection (logic actually implements the task; "
    "no obvious bugs), fidelity to the request (every stated requirement met), "
    "and robustness (handles the edge cases a first attempt misses)"
)


def _source_fidelity_block(session: Session, source: str) -> str:
    """Injected into the judge / chair / finish prompts when the task named a
    source the output must MATCH — so structural fidelity is actually scored
    instead of assumed. The blind stages never saw the source otherwise (authors
    get it in the round-0 overview), which is how a plain-prose candidate that
    dropped an illustrated-spread source's entire format won a 'match the first
    book exactly' vote. When the task also explicitly asked for a 'matched set'
    (classification.match_source), the requirement is stated as HARD."""
    if not source:
        return ""
    cls = getattr(session, "classification", None)
    hard = bool(cls and cls.match_source)
    strength = (
        "The task explicitly asks for a MATCHED SET: reproducing this source's "
        "STRUCTURE and FORMAT is a HARD requirement, weighed as strongly as prose "
        "quality. " if hard else
        "The task references this source; weigh how faithfully each candidate "
        "matches its structure and format. ")
    return (
        "\n===== SOURCE THE OUTPUT MUST MATCH (read it before scoring) =====\n"
        f"{source}\n===== end source =====\n"
        f"{strength}A candidate that drops sections, tables, or per-unit "
        "scaffolding the source has (e.g. per-spread illustration prompts) does NOT "
        "match, however clean it reads — and the source's own structure IS the "
        "required format here, never the 'commentary' or 'outline' a generic "
        "instruction might tell you to strip.\n")


def score_candidates_prompt(session: Session, labeled: list[tuple], source: str = "") -> str:
    """A blind scoring pass: the judge sees each candidate's FULL body labeled
    'Candidate N' with author identity stripped, scores each on the criteria,
    and names its winner. `labeled` is [(label, body[, runtime_note]), ...] — the
    runtime note is EVIDENCE from actually executing the candidate headless."""
    blocks = []
    for item in labeled:
        label, body = item[0], item[1]
        runtime = item[2] if len(item) > 2 else ""
        head = f"===== {label} =====" + (f"\n[RUNTIME (headless execution): {runtime}]" if runtime else "")
        # the WHOLE body, never truncated — judging a capped window scored
        # "which candidate fit under the cap", not which was best (live: a 23KB
        # game beat three richer 38-53KB ones because only it fit in 24000 chars
        # and the rest read as cut off mid-file)
        blocks.append(f"{head}\n{body}")
    n = len(labeled)
    # The runtime-weighing instruction is RIGHT for executable candidates (a game
    # that reads clean but crashes must lose) and WRONG for prose — a .txt story
    # has no "on-screen rendering", and injecting this made a judge invent an
    # "animates under simulated play" defect and penalise the story for it. Only
    # include it when a candidate actually carries runtime evidence.
    has_runtime = any(len(it) > 2 and (it[2] or "").strip() for it in labeled)
    runtime_guidance = (
        "Each candidate carries RUNTIME evidence from actually executing it headless "
        "(whether it runs without throwing, and whether it updates/responds under "
        "simulated input). WEIGH THIS HEAVILY: a candidate that reads beautifully but "
        "does NOT actually run must NOT beat one that runs — a file that throws on "
        "load or renders a static/near-empty screen scores LOW however clean it "
        "looks.\n"
    ) if has_runtime else ""
    return (
        f"Task the candidates implement: {_execution_task(session)}\n"
        f"{_GOVERNANCE_CONTEXT}"
        f"You are an impartial JUDGE. Below are {n} independent candidate "
        "implementations of the SAME task, authorship hidden. Score each STRICTLY "
        f"on: {CANDIDATE_CRITERIA}.\n"
        f"{runtime_guidance}"
        f"{_source_fidelity_block(session, source)}"
        "Read every candidate fully. A candidate that is truncated, stubbed, or "
        "misses a requirement must score low no matter how elegant the rest is.\n"
        f"Emit exactly one line per candidate, scoring 0-{config.JUDGE_SCORE_MAX}:\n"
        "SCORE Candidate 1: <n>\n...\n"
        f"SCORE Candidate {n}: <n>\n"
        "Then one line naming your single best:\n"
        "WINNER: Candidate <k>\n"
        "Optionally, note concrete defects in the winner the author should fix "
        "(one per line, prefixed 'DEFECT: ') — surgical fixes only, not a "
        "rewrite. Base every score on what the code ACTUALLY does.\n\n"
        + "\n\n".join(blocks)
    )


_SCORE_RE = re.compile(r"^\s*(?:[-*•]\s*)?(?:\*\*)?SCORE\s+Candidate\s+(\d+)\s*[:—–-]\s*(\d+)",
                       re.IGNORECASE | re.MULTILINE)
_WINNER_RE = re.compile(r"^\s*(?:[-*•]\s*)?(?:\*\*)?WINNER\s*[:—–-]\s*(?:Candidate\s+)?(\d+)",
                        re.IGNORECASE | re.MULTILINE)
_DEFECT_RE = re.compile(r"^\s*(?:[-*•]\s*)?(?:\*\*)?DEFECT\s*[:—–-]\s*(.+?)\s*$",
                        re.IGNORECASE | re.MULTILINE)


def parse_candidate_scores(text: str, n: int) -> tuple[dict[int, int], int | None, list[str]]:
    """(scores, winner_index, defects) from a judge reply. scores maps the
    1-based candidate number → clamped score; winner is the WINNER: line's
    number (or the top-scored if absent); defects are the winner's noted fixes.
    1-based candidate numbers out of range are ignored."""
    scores: dict[int, int] = {}
    for m in _SCORE_RE.finditer(text or ""):
        idx = int(m.group(1))
        if 1 <= idx <= n:
            scores[idx] = max(0, min(config.JUDGE_SCORE_MAX, int(m.group(2))))
    wm = _WINNER_RE.search(text or "")
    winner = int(wm.group(1)) if wm and 1 <= int(wm.group(1)) <= n else None
    if winner is None and scores:
        winner = max(scores, key=lambda k: scores[k])
    defects = [d.strip() for d in _DEFECT_RE.findall(text or "") if d.strip()]
    return scores, winner, defects


# The lead's standing charter — who it is on EVERY task, stated up front so it
# acts as the chair by design, not by whatever the flow leaves over.
CHAIR_CHARTER = (
    "YOU ARE THE CHAIR of this council — its final arbiter, not one of its "
    "authors. Here is how this council works and your standing role in it, the "
    "same on every task: the panel of independent models each produce a "
    "complete candidate; every candidate is EXECUTED and the ones that crash "
    "are disqualified; independent judges then score the survivors blindly. "
    "Your job is to RATIFY OR OVERTURN that vote, FINISH the chosen file with "
    "surgical corrections, and — only when nothing the panel produced runs — "
    "RECOVER the best attempt. You do not write the deliverable from scratch "
    "while any candidate stands; you decide, correct, and stand behind what "
    "ships. Nothing leaves this council that you have not ratified.\n"
)

_OVERRIDE_RE = re.compile(r"^\s*(?:[-*•]\s*)?(?:\*\*)?OVERRIDE\s*[:—–-]\s*(?:Candidate\s+)?(\d+)",
                          re.IGNORECASE | re.MULTILINE)
_RATIFY_RE = re.compile(r"^\s*(?:[-*•]\s*)?(?:\*\*)?RATIFY\b", re.IGNORECASE | re.MULTILINE)


def chair_finish_prompt(session: Session, candidates: list[dict], filename: str,
                        offer_integration: bool, source: str = "") -> str:
    """The chair's SINGLE finishing pass — the decisions the old flow spent
    three serial codifier calls on (review → fix → integration), each
    re-reading the same candidate bodies. `candidates` = [{label, role, score,
    votes, content, judge_defects}, ...], vote winner first, runner-up second;
    the remaining candidates are included only when integration review is on.
    Author identity stays hidden (Candidate N) as it was for the judges.
    `source` (when named) is the reference the output must match — the chair,
    like the judges, otherwise never saw it."""
    blocks = []
    for c in candidates:
        flagged = "".join(
            f"\n[JUDGE DEFECT D{i}: {d}]"
            for i, d in enumerate(c.get("judge_defects") or [], 1)
        )
        blocks.append(
            f"===== Candidate {c['label']} ({c['role']} — score {c['score']}, "
            f"{c['votes']} first-place vote(s)) ====={flagged}\n"
            f"{c['content']}")  # in full — "read both finalists in full" must be literally true
    win, run = candidates[0]["label"], candidates[1]["label"]
    integration = ""
    if offer_integration:
        integration = (
            "3) INTEGRATION — independently authored candidates can contain "
            "complementary strengths at a fine-grained level. Evaluate EVERY "
            "candidate below against the task and each other. Offer an "
            "integration ONLY when it makes a concrete, testable improvement "
            "over the file you chose without mixing incompatible designs — "
            "never merely to average preferences (the human decides whether to "
            "use it; your chosen candidate stays the default). If no meaningful "
            "integration exists, end your reply with exactly:\n"
            "SYNERGY: NO - <brief reason>\n"
            "If one does, end with exactly this structure and a COMPLETE "
            "replacement file (no PROMOTE line):\n"
            "SYNERGY: YES\n"
            "RATIONALE: <which specific strengths from which candidates are combined>\n"
            f"SOURCES: <Candidate numbers>\nARTIFACT: {filename}\n"
            "<complete integrated file contents>\nEND_ARTIFACT\n")
    return (
        f"Task the candidates implement: {_execution_task(session)}\n"
        f"{_GOVERNANCE_CONTEXT}"
        f"{CHAIR_CHARTER}"
        f"{_source_fidelity_block(session, source)}"
        "The blind judge vote ranked these candidates; the top two are your "
        "FINALISTS (VOTE WINNER and runner-up). The vote is ADVISORY to you — "
        "judges score by reading and can miss a real bug or a missed "
        "requirement. Read both finalists in full, then deliver ALL of your "
        "chair duties in THIS ONE reply, in this order:\n"
        "1) THE DECISION — exactly one line:\n"
        f"RATIFY: Candidate {win}\n"
        "  — or —\n"
        f"OVERRIDE: Candidate {run} - <specific, concrete reason the runner-up is better>\n"
        "2) DEFECTS AND FIXES — list the concrete defects in the file you "
        "chose, one per line, 'DEFECT: <what and where>' (or 'DEFECT: none'), "
        "then FIX exactly those with surgical EDIT blocks against that file — "
        "do NOT rewrite or restyle it; its author won on merit. The OLD "
        "snippet must be unique in the file:\n"
        f"EDIT: {filename}\n"
        "<<<<<<< OLD\n<exact existing text>\n=======\n<replacement text>\n>>>>>>> NEW\n"
        "If a flagged defect is not real or not safely fixable in isolation, "
        "name it and skip the edit. For EVERY JUDGE DEFECT D-number on the "
        "candidate you choose, also emit exactly one closure line: "
        "'RESOLVE D1: FIXED - <edit/evidence>' or "
        "'RESOLVE D1: REJECTED - <specific evidence it is not a defect>'. "
        "A missing resolution is an open defect and blocks release.\n"
        f"{integration}\n"
        + "\n\n".join(blocks)
    )


def parse_chair_decision(text: str, winner_label: int, runnerup_label: int) -> tuple[int, bool, list[str]]:
    """(chosen_label, overrode, defects). An OVERRIDE naming the runner-up
    switches the winner; anything else (RATIFY, or an OVERRIDE naming the
    winner, or silence) keeps the vote's winner. Defects drive the finish pass."""
    overrode = False
    chosen = winner_label
    m = _OVERRIDE_RE.search(text or "")
    if m and int(m.group(1)) == runnerup_label:
        chosen, overrode = runnerup_label, True
    defects = [d.strip() for d in _DEFECT_RE.findall(text or "")
               if d.strip() and d.strip().lower() != "none"]
    return chosen, overrode, defects


_RESOLUTION_RE = re.compile(
    r"^\s*(?:[-*]\s*)?(?:\*\*)?RESOLVE\s+(D\d+)\s*:\s*"
    r"(FIXED|REJECTED)\s*[-—–:]\s*(.+?)\s*$",
    re.IGNORECASE | re.MULTILINE,
)
_VERDICT_RE = re.compile(
    r"^\s*(?:[-*]\s*)?(?:\*\*)?VERDICT\s*:\s*(PASS|FAIL)\b",
    re.IGNORECASE | re.MULTILINE,
)
_CHECK_RE = re.compile(
    r"^\s*(?:[-*]\s*)?(?:\*\*)?CHECK(?:\s+(R\d+))?\s*:\s*"
    r"(PASS|FAIL)\s*[-—–:]\s*(.+?)\s*$",
    re.IGNORECASE | re.MULTILINE,
)


def parse_defect_resolutions(text: str) -> dict[str, dict[str, str]]:
    return {
        m.group(1).upper(): {"status": m.group(2).upper(), "detail": m.group(3).strip()}
        for m in _RESOLUTION_RE.finditer(text or "")
    }


def acceptance_requirements(task: str) -> list[str]:
    """Extract a bounded, stable checklist from an implementation brief."""
    raw_lines = [re.sub(r"^\s*(?:[-*•]|\d+[.)])\s*", "", line).strip()
                 for line in (task or "").splitlines()]
    candidates = [line for line in raw_lines if 8 <= len(line) <= 500]
    candidates.extend(
        part.strip() for part in re.split(r"(?<=[.!?])\s+|;\s+", task or "")
        if 8 <= len(part.strip()) <= 500
    )
    explicit = re.compile(
        r"\b(?:must|should|shall|required|include|support|allow|provide|ensure|"
        r"controls?|keyboard|touch|pause|audio|responsive|persist|save|load|"
        r"acceptance|deliver|file|function|feature)\b",
        re.IGNORECASE,
    )
    selected = [item for item in candidates if explicit.search(item)] or candidates
    out: list[str] = []
    seen: set[str] = set()
    for item in selected:
        key = re.sub(r"\s+", " ", item).strip().lower()
        if key and key not in seen:
            seen.add(key)
            out.append(item)
        if len(out) >= 40:
            break
    return out or [(task or "deliver the requested implementation").strip()[:500]]


def frontier_release_prompt(
    session: Session, files: list[tuple[str, str]],
    defect_register: Optional[list[str]] = None,
    resolutions: Optional[dict[str, dict[str, str]]] = None,
    repair_attempt: int = 0,
) -> str:
    """Independent frontier release engineering: semantic acceptance + repair."""
    register = "\n".join(
        f"D{i}: {defect}" for i, defect in enumerate(defect_register or [], 1)
    ) or "none"
    closure = "\n".join(
        f"{key}: {value.get('status')} - {value.get('detail')}"
        for key, value in (resolutions or {}).items()
    ) or "none"
    bodies = "\n\n".join(
        f"===== FILE: {name} =====\n{content}" for name, content in files
    )
    requirements = acceptance_requirements(_execution_task(session))
    checklist = "\n".join(
        f"R{i}: {requirement}" for i, requirement in enumerate(requirements, 1)
    )
    repair = (
        "This is the confirmation pass after your prior repair. Do not trust the "
        "claimed fix; re-inspect the resulting files from scratch."
        if repair_attempt else
        "REPAIR MANDATE — you are a release ENGINEER, not a critic. For EVERY "
        "check you FAIL where the fix is knowable from the code in front of "
        "you, you MUST ship that fix in this same reply:\n"
        "- surgical fixes: one block per edit, each starting with the exact "
        "literal header 'EDIT: <exact path>' — never 'OLD/NEW EDIT' or a "
        "numbered variant like 'EDIT 1:', every edit restates the same plain "
        "'EDIT: <path>' header — followed by 'OLD:' and a fenced code block "
        "with the exact existing text, then 'NEW:' and a fenced code block "
        "with its replacement;\n"
        "- broader fixes (structural rework, many touchpoints): a COMPLETE "
        "replacement file as 'ARTIFACT: <exact path shown below>' followed by "
        "the full file body and a line 'END_ARTIFACT'.\n"
        "A FAIL without a repair is acceptable ONLY when the fix genuinely "
        "requires its owner's rebuild (a missing subsystem, absent content you "
        "cannot invent); say so in that DEFECT line as 'requires owner "
        "rebuild: <why>'. Rejecting without repairing fixable defects wastes "
        "an entire verification cycle and is treated as an incomplete review."
    )
    return (
        "You are the independent FRONTIER RELEASE ENGINEER. You did not select or "
        "summarize this result. Inspect the actual final code in full and protect "
        "the user from a superficially runnable but incomplete package.\n\n"
        f"ORIGINAL TASK:\n{_execution_task(session)}\n\n"
        f"JUDGE DEFECT REGISTER:\n{register}\n\n"
        f"CHAIR CLOSURE CLAIMS:\n{closure}\n\n"
        f"REQUIRED ACCEPTANCE CHECKLIST:\n{checklist}\n\n"
        f"{repair}\n"
        "Test every explicit behavior in the task, not merely syntax/load. Verify "
        "every registered defect is demonstrably closed. Emit exactly one line "
        "for EVERY R-number: 'CHECK R1: PASS - <specific evidence>' or "
        "'CHECK R1: FAIL - <specific evidence>'. Then emit DEFECT lines for "
        "every remaining problem. End with exactly 'VERDICT: PASS' only if all "
        "explicit requirements pass and no material defect remains; otherwise end "
        "with 'VERDICT: FAIL'. Missing R-number checks invalidate a PASS. For repairs, "
        "use exact unique OLD/NEW EDIT blocks and the exact file paths shown.\n\n"
        f"{bodies}"
    )


def parse_frontier_verdict(text: str) -> tuple[str, list[dict[str, str]], list[str]]:
    match = _VERDICT_RE.search(text or "")
    verdict = match.group(1).upper() if match else "FAIL"
    checks = [
        {"id": (m.group(1) or "").upper(),
         "status": m.group(2).upper(), "detail": m.group(3).strip()}
        for m in _CHECK_RE.finditer(text or "")
    ]
    defects = [d.strip() for d in _DEFECT_RE.findall(text or "")
               if d.strip() and d.strip().lower() != "none"]
    if not checks or any(item["status"] == "FAIL" for item in checks) or defects:
        verdict = "FAIL"
    return verdict, checks, defects


def frontier_runtime_repair_prompt(
    session: Session, filename: str, content: str, failure: str,
) -> str:
    return (
        "You are still the IMPLEMENTATION OWNER of this candidate, not a judge. "
        "The coordinator executed your code and found the failure below. Repair "
        "your own implementation now with exact, unique OLD/NEW EDIT blocks. "
        "Preserve its design and fulfill the original task; return no plan.\n\n"
        f"ORIGINAL TASK:\n{_execution_task(session)}\n\n"
        f"RUNTIME FAILURE:\n{failure}\n\n"
        f"===== FILE: {filename} =====\n{content}"
    )


def chair_recover_prompt(session: Session, filename: str, body: str, error: str) -> str:
    """Every candidate the panel produced crashed. The chair repairs the most
    complete attempt — surgical EDITs to make it RUN — rather than discarding
    all the panel's work and starting over."""
    return (
        f"Task: {_execution_task(session)}\n"
        f"{_GOVERNANCE_CONTEXT}"
        f"{CHAIR_CHARTER}"
        "Every candidate the panel produced FAILED to run. Below is the most "
        f"complete attempt; executed headless, it threw: {error}\n"
        "As chair, RECOVER it: emit surgical EDIT blocks that make it run "
        "correctly. Fix the actual failure and any obvious siblings of it; do "
        "NOT rewrite the file wholesale. The OLD snippet must be unique:\n"
        "EDIT: " + filename + "\n"
        "<<<<<<< OLD\n<exact existing text>\n=======\n<replacement text>\n>>>>>>> NEW\n"
        "Emit nothing but EDIT blocks.\n\n"
        # full body: EDIT OLD-snippets must be verifiably unique in the WHOLE file
        f"----- {filename} -----\n{body}"
    )


def parse_integration_decision(text: str) -> tuple[bool, str, list[str]]:
    """Return (offered, rationale, source labels) from a codifier reply."""
    offered = bool(re.search(r"^\s*SYNERGY\s*:\s*YES\b", text or "", re.I | re.M))
    if not offered:
        return False, "", []
    rationale = re.search(r"^\s*RATIONALE\s*:\s*(.+)$", text or "", re.I | re.M)
    sources = re.search(r"^\s*SOURCES\s*:\s*(.+)$", text or "", re.I | re.M)
    labels = re.findall(r"Candidate\s+\d+", sources.group(1) if sources else "", re.I)
    return True, (rationale.group(1).strip() if rationale else ""), labels


def synthesis_prompt(
    session: Session, council: "Council", role_agents: dict[Role, str] | None,
    round_idx: int, panel_results: list[Contribution],
    established_overview: str = "", readable: list[str] = (),
) -> str:
    """The lead's per-round prompt: the task, the panel's independent views, the
    file-output contract, the delegation menu, and the ROUND: DONE/CONTINUE
    termination contract. With no panel this is the plain lead prompt plus the
    round contract."""
    produces = bool(session.classification and session.classification.produces_output)
    overview = f"{established_overview}\n\n" if established_overview else ""
    contract = _output_contract(session) if produces else (
        "Produce the actual answer the task asks for — complete, concrete, and "
        "ready to use. Do not ask the human about details you can reasonably "
        "assume (styling, length, tone, naming) — state the assumption and "
        "answer. The one exception: if the request genuinely reads two ways and "
        "the two readings produce DIFFERENT deliverables, ask that single "
        "question instead of guessing.\n"
    )
    panel_block = ""
    if panel_results:
        views = "\n\n".join(
            f"--- {c.agent} ---\n{c.content[:config.PANEL_TO_LEAD_CHARS]}"
            for c in panel_results)
        panel_block = (
            f"PANEL VIEWS (round {round_idx + 1} — independent takes from the "
            "other council models; weigh them on evidence, adopt what is right, "
            "and name real disagreements rather than papering over them):\n"
            f"{views}\n\n")
    return (
        f"Task: {_execution_task(session)}\n"
        f"{_GOVERNANCE_CONTEXT}"
        f"Your role: lead (round {round_idx + 1}). You own the OUTCOME end to "
        "end: organize the work, assign it to your talents, integrate their "
        "results, and deliver the real, working result — not a plan or a "
        "description of it.\n"
        f"{delegation_contract(council, role_agents, produces_output=produces)}"
        f"{contract}"
        f"{_skill_hints(session, Role.lead, readable)}"
        f"{overview}"
        f"{panel_block}"
        f"Context so far:\n{_recent_context(session, limit=5)}\n"
        f"{_ROUND_CONTRACT}"
    )


def test_fix_prompt(
    session: Session, failure_output: str, files: list[str],
    attempt: int, max_attempts: int, readable: list[str] = (),
) -> str:
    """The goal-loop repair prompt: the lead sees the real test failure and must
    fix the code in-reply — not explain the failure back to the human."""
    file_list = ", ".join(files) if files else "(none yet)"
    return (
        f"Task: {_execution_task(session)}\n"
        f"{_GOVERNANCE_CONTEXT}"
        f"Your build's test run FAILED (repair attempt {attempt} of {max_attempts}). "
        "Fix the code NOW — do not explain the failure to the human; repair it.\n"
        f"Files you have written so far: {file_list}\n"
        "Emit ONLY what changes: a surgical edit per fix —\n"
        "EDIT: <filename>\n"
        "<<<<<<< OLD\n<exact existing text>\n=======\n<replacement text>\n>>>>>>> NEW\n"
        "— or a full 'ARTIFACT: <filename>' re-write when the change is large. The "
        "same static check re-runs automatically after your fix; a functional "
        "RUNTESTS command waits for the user's approval. Emit a "
        "'RUNTESTS: <command>' line only to CHANGE the command. If the failure is "
        "genuinely unfixable here (e.g. a missing system dependency), say why in "
        "one short paragraph and emit no blocks.\n"
        f"{_skill_hints(session, Role.lead, readable)}"
        f"TEST OUTPUT:\n{failure_output}\n"
    )


def round_summaries(session: Session) -> str:
    """One capped line per completed round (from the lead syntheses) so the
    human can decide whether more rounds are worth it."""
    lines: list[str] = []
    for spec in session.rounds:
        synth = next((c for c in reversed(session.contributions)
                      if c.round == spec.round and c.role == Role.lead), None)
        gist = " ".join((synth.content if synth else "").split())[:config.ROUND_SUMMARY_CHARS]
        lines.append(f"round {spec.round + 1}: {gist or '(no synthesis recorded)'}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Mandatory independent review: one seat that did NOT author the deliverable
# checks it before delivery. Deliberately narrow — this reviewer does not
# redesign, rewrite, or bikeshed; it answers whether the thing the user asked
# for is actually what was produced.
# ---------------------------------------------------------------------------
REVIEW_MARKER = re.compile(
    r"^[ \t]*(?:\*\*)?REVIEW(?:\*\*)?[ \t]*:[ \t]*(?:\*\*)?[ \t]*(?P<verdict>PASS|FAIL)",
    re.IGNORECASE | re.MULTILINE,
)


def deliverable_review_prompt(session: Session, files: list[tuple[str, str]],
                              author: str) -> str:
    """Ask a second model whether the delivered artifact IS the deliverable.

    Framed around the request, not around code quality. The failure this exists
    to catch is categorical — a script where a PDF was wanted, an empty shell
    where content was wanted, a stub where a whole file was wanted — not style.
    """
    listing = "\n\n".join(
        f"--- {name} ({len(body)} chars) ---\n{body[:config.REVIEW_FILE_MAX_CHARS]}"
        + ("\n[... truncated for review ...]"
           if len(body) > config.REVIEW_FILE_MAX_CHARS else "")
        for name, body in files
    )
    return (
        f"TASK THE USER ASKED FOR:\n{_execution_task(session)}\n\n"
        f"WHAT {author} PRODUCED — these exact files are about to be delivered:\n"
        f"{listing}\n\n"
        "You are the INDEPENDENT REVIEWER. You did not write this and you are "
        "not rewriting it. Answer one question: is this actually the thing the "
        "user asked for?\n\n"
        "Judge ONLY these, in order:\n"
        "1. WRONG KIND OF ARTIFACT — the deliverable is a different kind of "
        "thing than was requested (a program that would produce the output "
        "instead of the output; a plan instead of the work; a stub instead of a "
        "complete file).\n"
        "2. MISSING — something the request explicitly named is absent.\n"
        "3. INCOMPLETE — the file is cut off, or a section is a placeholder.\n"
        "4. FALSE CLAIM — the work claims something the bytes do not support.\n\n"
        "Do NOT fail it for style, structure, naming, formatting, efficiency, "
        "or choices you would have made differently. Unstated details the author "
        "reasonably assumed are NOT defects.\n\n"
        "Reply in exactly this form and nothing else:\n"
        "REVIEW: PASS\n"
        "or\n"
        "REVIEW: FAIL\n"
        "FINDINGS:\n"
        "- <one concrete defect, naming the file>\n"
        "- <another, if any>\n"
    )


def parse_review(reply: str) -> tuple[str, list[str]]:
    """('pass'|'fail'|'', findings). An empty verdict means the reviewer did not
    answer in the contract — treated as no opinion, never as a failure."""
    match = REVIEW_MARKER.search(reply or "")
    if not match:
        return "", []
    verdict = match.group("verdict").lower()
    findings: list[str] = []
    for line in (reply or "").splitlines():
        stripped = line.strip()
        if stripped.startswith(("- ", "* ", "• ")):
            text = stripped[2:].strip()
            if text and text.lower() not in {"none", "n/a", "none.", "no defects"}:
                findings.append(text[:400])
    return verdict, findings[:8]
