"""Round prompts and contracts — the text layer of the deliberation loop.

Everything here builds prompt strings or parses marker lines out of replies;
no orchestration, no side effects. loop.py imports from this module (never the
reverse), so the prompt layer stays cycle-free and independently testable.
"""

from __future__ import annotations

import re

from .models import Contribution, Council, CouncilMember, Role, Session
from . import config
from .skills import get_skill


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
    "Raw bytes right after the ARTIFACT line — no ``` fences, and no commentary "
    "after the content (the file must end at its real final byte). Your "
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
    author = (
        "This task ships FILES — DELEGATE the authoring itself, e.g. "
        "'DELEGATE: code_generator - author the complete <filename>, implementing "
        "<the agreed design>'. A delegated talent's ARTIFACT/EDIT blocks are "
        "captured directly as real files in the council space; you then review "
        "them and emit the PROMOTE lines yourself.\n") if produces_output else ""
    return (
        "YOU are the LEAD — the organizer and integrator, NOT the doer. Break the "
        "task into focused assignments, hand each to the right talent below, then "
        "integrate their results and quality-gate the whole. Author work yourself "
        "only when it is trivial glue; a substantive piece done solo should be "
        "the justified exception, not the default. One line per assignment:\n"
        "CONSULT: <talent> - <specific question>   (advice/verification returns to you)\n"
        "DELEGATE: <talent> - <specific subtask>   (the talent produces that piece)\n"
        "Their results come back and you are re-called to integrate and finish. "
        "When results already appear below, integrate them NOW — do not "
        "re-request the same work.\n"
        f"{author}"
        f"Available talents:\n{menu}\n"
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


def _skill_hints(session: Session, role: Role, readable: list[str] = ()) -> str:
    """Advertise the no-approval discovery skills this role may pull (read/search/
    list/web), but only when there's something to look at."""
    has_dir = bool(session.established_root or session.workspace_root)
    where = "established folder" if session.established_root else "project"
    hints: list[str] = []
    ld = get_skill("list_dir")
    if has_dir and ld and role in ld.allowed_roles:
        hints.append(
            f"list the {where}'s files with a line 'SKILL: list_dir .' (use a subfolder to drill in)"
        )
    sp = get_skill("search_project")
    if has_dir and sp and role in sp.allowed_roles:
        hints.append(f"search the {where} with a line 'SKILL: search_project <query>'")
    rf = get_skill("read_file")
    if (readable or has_dir) and rf and role in rf.allowed_roles:
        avail = f" Available now: {', '.join(readable)}." if readable else ""
        hints.append(f"read a file with a line 'SKILL: read_file <path>'.{avail}")
    ws = get_skill("web_search")
    if config.WEB_ENABLED and ws and role in ws.allowed_roles:
        hints.append("search the live web with 'SKILL: web_search <query>' "
                     "(and read a page with 'SKILL: web_fetch <url>')")
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
    return (
        "If the task needs files (code, docs, config), emit each file literally in "
        "this format, with its COMPLETE contents — never a summary of what the file "
        "would contain:\n"
        "ARTIFACT: <filename>\n"
        "<full file contents>\n"
        "Use one ARTIFACT block per file and include EVERY file the task asks for. Do "
        "not describe the files in prose — write them out in full.\n"
        "Write each file's RAW bytes immediately after its ARTIFACT line. Do NOT wrap "
        "file contents in ``` markdown code fences, and do NOT add ANY commentary, "
        "notes, or explanation AFTER the last file's content — the file must end at "
        "its real final byte. A short rationale BEFORE the first ARTIFACT line is "
        "welcome (it is shown to the user); everything after the first ARTIFACT line "
        "must be only file contents and ARTIFACT/EDIT/PROMOTE/RUNTESTS lines.\n"
        f"{promote}"
        "To MODIFY an existing file instead of overwriting it, emit a surgical edit "
        "(the OLD snippet must be unique in the file):\n"
        "EDIT: path/to/file.py\n"
        "<<<<<<< OLD\n<exact existing text>\n=======\n<replacement text>\n>>>>>>> NEW\n"
        "To run the test suite in your sandbox (free, no approval), add a line:\n"
        "RUNTESTS: <command>   (e.g. RUNTESTS: pytest -q; omit the command to default)\n"
    )


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
        "ready to use. Do not ask the human questions; state assumptions and answer.\n"
    )
    return (
        f"Task: {session.task.text}\n"
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
        f"Task: {session.task.text}\n"
        f"{_PANEL_CONTEXT}"
        f"You are one seat on a multi-model council (round {round_idx + 1}; your "
        f"origin model: {member.agent}). Give YOUR best independent take on the "
        "task: the answer or design as you see it, the strongest objections, and "
        f"{disagree}"
        f"{_panel_file_contract(session)}"
        "Do not ask the human questions; state assumptions and proceed.\n"
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
    return (
        "This is a BEST-OF-N build: emit YOUR COMPLETE candidate implementation "
        "as a block —\n"
        "ARTIFACT: <filename>\n<full file contents>\n"
        "— raw bytes right after the ARTIFACT line (no ``` fences, no commentary "
        "after the content; the file must end at its real final byte). Every "
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
        blocks.append(f"{head}\n{body[:config.CANDIDATE_SCORE_MAX_CHARS]}")
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
        f"Task the candidates implement: {session.task.text}\n"
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


def winner_fix_prompt(session: Session, filename: str, body: str, defects: list[str],
                      source: str = "") -> str:
    """Ask for SURGICAL edits to the winning candidate — the defects judges
    flagged — never a rewrite (best-of-N ships the winner's own code). `source`
    (the reference to match) is included so the finisher isn't blind to it: the
    live failure was the codifier answering with `SKILL: read_file` to fetch the
    source it lacked instead of edits, so the winner shipped unfixed."""
    deflines = "\n".join(f"- {d}" for d in defects)
    return (
        f"Task: {session.task.text}\n"
        f"{_GOVERNANCE_CONTEXT}"
        f"{_source_fidelity_block(session, source)}"
        f"The file below WON a blind best-of-N vote and will ship. Judges flagged "
        "these specific defects to fix — fix ONLY these, with surgical edits; do "
        "NOT rewrite or restyle the file (its author won on merit):\n"
        f"{deflines}\n"
        "Emit one EDIT block per fix (the OLD snippet must be unique in the file):\n"
        "EDIT: " + filename + "\n"
        "<<<<<<< OLD\n<exact existing text>\n=======\n<replacement text>\n>>>>>>> NEW\n"
        "If a flagged defect is not real or not safely fixable in isolation, skip "
        "it. Emit nothing but EDIT blocks.\n\n"
        f"----- {filename} -----\n{body[:config.SKILL_RESULT_SANDBOX_MAX_CHARS]}"
    )


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


def chair_review_prompt(session: Session, top: list[dict], source: str = "") -> str:
    """The chair reviews the blind vote's top two IN FULL and ratifies or
    overrides. `top` = [{label, score, votes, content, role}, ...], winner
    first. Author identity stays hidden (Candidate N) as it was for the judges.
    `source` (when named) is the reference the output must match — the chair, like
    the judges, otherwise never saw it."""
    blocks = []
    for c in top:
        blocks.append(
            f"===== Candidate {c['label']} ({c['role']} — score {c['score']}, "
            f"{c['votes']} first-place vote(s)) =====\n"
            f"{c['content'][:config.CANDIDATE_SCORE_MAX_CHARS]}")
    win, run = top[0]["label"], top[1]["label"]
    return (
        f"Task the candidates implement: {session.task.text}\n"
        f"{_GOVERNANCE_CONTEXT}"
        f"{CHAIR_CHARTER}"
        f"{_source_fidelity_block(session, source)}"
        "The blind judge vote ranked these two highest. The vote is ADVISORY to "
        "you — judges score by reading and can miss a real bug or a missed "
        "requirement. Read BOTH in full and decide. Emit exactly one line:\n"
        f"RATIFY: Candidate {win}\n"
        "  — or —\n"
        f"OVERRIDE: Candidate {run} - <specific, concrete reason the runner-up is better>\n"
        "Then list concrete defects in the file you chose for a surgical finishing "
        "pass — one per line, 'DEFECT: <what and where>' — or 'DEFECT: none'. "
        "Do NOT rewrite; the author won on merit.\n\n"
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


def chair_recover_prompt(session: Session, filename: str, body: str, error: str) -> str:
    """Every candidate the panel produced crashed. The chair repairs the most
    complete attempt — surgical EDITs to make it RUN — rather than discarding
    all the panel's work and starting over."""
    return (
        f"Task: {session.task.text}\n"
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
        f"----- {filename} -----\n{body[:config.SKILL_RESULT_SANDBOX_MAX_CHARS]}"
    )


def integration_prompt(session: Session, filename: str, candidates: list[dict], source: str = "") -> str:
    """Ask the codifier whether a real micro-level merge is worth offering.

    This is deliberately an offer, not an automatic rewrite: only a concrete,
    materially better integration earns a human decision point.
    """
    labeled = "\n\n".join(
        f"----- Candidate {c['label']} ({c['role']}; score {c['score']}, "
        f"{c['votes']} first-place votes) -----\n{c['content']}"
        for c in candidates
    )
    return (
        f"Task: {session.task.text}\n"
        f"{_GOVERNANCE_CONTEXT}"
        "You are the council's integration reviewer. The blind vote has chosen "
        "a default winner, but independently authored candidates can contain "
        "complementary strengths at a fine-grained level. Evaluate EVERY "
        "candidate below against the task and each other.\n"
        "Offer an integration only when it makes a concrete, testable improvement "
        "over the voted winner without mixing incompatible designs. Do not offer "
        "one merely to average preferences. The human will decide whether to use it.\n"
        "If no meaningful integration exists, emit exactly:\n"
        "SYNERGY: NO - <brief reason>\n"
        "If a source reference is supplied below, use it directly and do not request "
        "a SKILL to read it again.\n"
        "If it does, emit exactly this structure, with a COMPLETE replacement "
        "file and no PROMOTE line:\n"
        "SYNERGY: YES\n"
        "RATIONALE: <which specific strengths from which candidates are combined>\n"
        f"SOURCES: <Candidate numbers>\nARTIFACT: {filename}\n"
        "<complete integrated file contents>\n\n"
        f"CANDIDATES:\n{labeled}"
        + (f"\n\nSOURCE REFERENCE:\n{source}" if source else "")
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
        "ready to use. Do not ask the human questions; state assumptions and answer.\n"
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
        f"Task: {session.task.text}\n"
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
        f"Task: {session.task.text}\n"
        f"{_GOVERNANCE_CONTEXT}"
        f"Your build's test run FAILED (repair attempt {attempt} of {max_attempts}). "
        "Fix the code NOW — do not explain the failure to the human; repair it.\n"
        f"Files you have written so far: {file_list}\n"
        "Emit ONLY what changes: a surgical edit per fix —\n"
        "EDIT: <filename>\n"
        "<<<<<<< OLD\n<exact existing text>\n=======\n<replacement text>\n>>>>>>> NEW\n"
        "— or a full 'ARTIFACT: <filename>' re-write when the change is large. The "
        "same test command re-runs automatically after your fix; emit a "
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
