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

def delegation_contract(council: "Council", role_agents: dict[Role, str] | None) -> str:
    """The on-demand delegation menu shown to the lead: each available specialist
    talent and its backing origin model. The lead pulls one in ONLY when a
    specific talent materially improves the result — synergy, not a panel."""
    mapping = role_agents or config.ROLE_AGENTS
    lines: list[str] = []
    for role, talent in config.TALENTS.items():
        member = council.get(role)
        origin = (member.agent if member else None) or mapping.get(role) or "?"
        lines.append(f"- {role.value} ({origin}): {talent}")
    menu = "\n".join(lines)
    return (
        "YOU LEAD this task. Do it yourself directly. You MAY pull in another "
        "talent (a different-origin model with a specific strength) ONLY when it "
        "materially improves the result — do not convene a panel by default. To "
        "do so, emit one line exactly:\n"
        "CONSULT: <talent> - <specific question>   (get an answer back, you stay in control)\n"
        "DELEGATE: <talent> - <specific subtask>   (hand off a focused piece)\n"
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
        "Your role: lead. You own this task end to end. Produce the real, working "
        "result — not a plan or a description of it.\n"
        f"{delegation_contract(council, role_agents)}"
        f"{contract}"
        f"{_skill_hints(session, Role.lead, readable)}"
        f"{overview}"
        f"Context so far:\n{_recent_context(session, limit=5)}"
    )


# ---------------------------------------------------------------------------
# Panel rounds: every enabled seat contributes in parallel, then the lead
# synthesizes and declares the round DONE or CONTINUE.
# ---------------------------------------------------------------------------

# Panel seats are pure generate_text voices — no SKILL/ARTIFACT machinery is
# resolved for them (only the lead's), so their context note must not advertise
# capabilities they don't have.
_PANEL_CONTEXT = (
    "You operate inside a governed coordinator with no filesystem or network "
    "access of your own; the coordinator has already gathered any project "
    "content shown below. Do not refuse for lack of access — reason from what "
    "is provided and state assumptions where something is missing.\n"
    "You have NO tools in this environment, even if your instincts say "
    "otherwise: do NOT emit tool-use syntax (Read/invoke blocks, file-path "
    "JSON) — nothing executes it and your seat's contribution would be lost. "
    "Your reply text IS your entire contribution.\n"
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


def reply_is_stub(content: str) -> bool:
    """True when a reply merely announces or ATTEMPTS the work instead of doing
    it: tool-call debris or first-person deferral phrasing, with too little
    real prose left over. A contract marker (SKILL:/CONSULT:/ARTIFACT:/ROUND:)
    always counts as real work; a genuinely short direct answer with no
    deferral phrasing is NOT a stub."""
    text = (content or "").strip()
    if _ANY_MARKER_RE.search(text):
        return False
    prose, debris = _TOOL_DEBRIS_RE.subn("", text)
    prose = prose.strip()
    if len(prose) >= config.SYNTHESIS_STUB_CHARS:
        return False
    return debris > 0 or bool(_DEFERRAL_RE.search(prose))


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
    """One seat's independent take. Panel seats never write files or pull
    skills — they contribute intelligence; the lead materializes."""
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
        "Do NOT emit ARTIFACT/PROMOTE file blocks — the lead "
        "materializes files; describe what should change instead. Do not ask the "
        "human questions; state assumptions and proceed.\n"
        f"{overview}"
        f"{ctx_block}"
    )


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
        f"Your role: lead (round {round_idx + 1}). You own this task end to end. "
        "Produce the real, working result — not a plan or a description of it.\n"
        f"{delegation_contract(council, role_agents)}"
        f"{contract}"
        f"{_skill_hints(session, Role.lead, readable)}"
        f"{overview}"
        f"{panel_block}"
        f"Context so far:\n{_recent_context(session, limit=5)}\n"
        f"{_ROUND_CONTRACT}"
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
