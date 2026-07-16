"""Goal layer (/goal): owned build packages, shared staging, one final release.

New goals use a dependency graph with one named model owner per package.
Package artifacts accumulate in an isolated goal workspace and only the final
integrated manifest may cross into the user's project, through one approval.
Persisted pre-overhaul goals retain their legacy delivery workflow.
"""

from __future__ import annotations

import json
import re
import sqlite3
import uuid
from pathlib import Path
from typing import Optional

from . import assembly, config
from .models import Goal, GoalMilestone, utcnow


class GoalStore:
    """Transactional, durable state for planners and milestone advancement.

    A JSON read-modify-write store let a background planner resurrect a goal
    that the user had just cancelled.  Goals now share the existing SQLite
    database with sessions; the old ``goals.json`` is imported once so existing
    users retain their history.
    """

    def __init__(self, data_dir: Path):
        self.data_dir = Path(data_dir)
        self.path = self.data_dir / "goals.json"  # legacy import source
        self.db_path = self.data_dir / "gangof8.db"
        self._init_db()

    def _conn(self) -> sqlite3.Connection:
        return sqlite3.connect(self.db_path, timeout=10)

    def _init_db(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        with self._conn() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute(
                """CREATE TABLE IF NOT EXISTS goals (
                       goal_id TEXT PRIMARY KEY,
                       status TEXT NOT NULL,
                       updated_at TEXT NOT NULL,
                       json TEXT NOT NULL
                   )"""
            )
            count = conn.execute("SELECT COUNT(*) FROM goals").fetchone()[0]
            if count:
                return
            for goal in self._legacy_goals():
                conn.execute(
                    "INSERT OR IGNORE INTO goals (goal_id, status, updated_at, json) "
                    "VALUES (?, ?, ?, ?)",
                    (goal.goal_id, goal.status, goal.updated_at, goal.model_dump_json()),
                )

    def _legacy_goals(self) -> list[Goal]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return [Goal.model_validate(g) for g in data.get("goals", [])]
        except (FileNotFoundError, json.JSONDecodeError, ValueError):
            pass
        return []

    @staticmethod
    def _decode(raw: str) -> Optional[Goal]:
        try:
            return Goal.model_validate(json.loads(raw))
        except (json.JSONDecodeError, TypeError, ValueError):
            return None

    @staticmethod
    def _write(conn: sqlite3.Connection, goal: Goal) -> None:
        conn.execute(
            "INSERT INTO goals (goal_id, status, updated_at, json) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(goal_id) DO UPDATE SET status=excluded.status, "
            "updated_at=excluded.updated_at, json=excluded.json",
            (goal.goal_id, goal.status, goal.updated_at, goal.model_dump_json()),
        )

    def list(self) -> list[Goal]:
        with self._conn() as conn:
            rows = conn.execute("SELECT json FROM goals ORDER BY updated_at ASC").fetchall()
        return [goal for row in rows if (goal := self._decode(row[0])) is not None]

    def get(self, goal_id: str) -> Optional[Goal]:
        with self._conn() as conn:
            row = conn.execute("SELECT json FROM goals WHERE goal_id = ?", (goal_id,)).fetchone()
        return self._decode(row[0]) if row else None

    def save(self, goal: Goal) -> Goal:
        goal.updated_at = utcnow()
        with self._conn() as conn:
            self._write(conn, goal)
        return goal

    def save_owned(self, goal: Goal, token: str) -> bool:
        """Save only while the caller still owns the persisted goal lease."""
        if not token:
            return False
        goal.updated_at = utcnow()
        with self._conn() as conn:
            row = conn.execute("SELECT json FROM goals WHERE goal_id = ?", (goal.goal_id,)).fetchone()
            stored = self._decode(row[0]) if row else None
            if stored is None or stored.worker_lease != token:
                return False
            goal.worker_lease = token
            self._write(conn, goal)
        return True

    def claim_worker_lease(self, goal_id: str, allowed_statuses: set[str]) -> Optional[Goal]:
        """Atomically claim an eligible goal for a planning/advance worker."""
        with self._conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT json FROM goals WHERE goal_id = ?", (goal_id,)).fetchone()
            goal = self._decode(row[0]) if row else None
            if goal is None or goal.status not in allowed_statuses or goal.worker_lease:
                return None
            goal.worker_lease = uuid.uuid4().hex
            goal.updated_at = utcnow()
            self._write(conn, goal)
        return goal

    def lease_is_current(self, goal_id: str, token: str) -> bool:
        if not token:
            return False
        goal = self.get(goal_id)
        return bool(goal and goal.worker_lease == token)

    def release_worker_lease(self, goal_id: str, token: str) -> bool:
        if not token:
            return False
        with self._conn() as conn:
            row = conn.execute("SELECT json FROM goals WHERE goal_id = ?", (goal_id,)).fetchone()
            goal = self._decode(row[0]) if row else None
            if goal is None or goal.worker_lease != token:
                return False
            goal.worker_lease = ""
            goal.updated_at = utcnow()
            self._write(conn, goal)
        return True

    def cancel(self, goal_id: str) -> Optional[Goal]:
        """Cancel atomically and invalidate every planner/advancer epoch."""
        with self._conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT json FROM goals WHERE goal_id = ?", (goal_id,)).fetchone()
            goal = self._decode(row[0]) if row else None
            if goal is None:
                return None
            if goal.status not in ("completed", "cancelled", "failed"):
                goal.status = "cancelled"
                goal.epoch += 1
                goal.worker_lease = ""
                for package in goal.milestones:
                    if package.status == "running":
                        package.status = "cancelled"
                goal.updated_at = utcnow()
                self._write(conn, goal)
        return goal

    def resume(self, goal_id: str) -> Optional[Goal]:
        """Reopen a paused goal without invalidating healthy sibling workers.

        A package failure pauses scheduling, but other packages from the same
        wave are allowed to finish and commit.  Advancing the whole goal epoch
        here used to discard those valid completions and trigger duplicate model
        calls.  Only cancellation/restart invalidates an epoch; retrying merely
        resets failed/pending attempts inside the current epoch.
        """
        with self._conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT json FROM goals WHERE goal_id = ?", (goal_id,)).fetchone()
            goal = self._decode(row[0]) if row else None
            if goal is None or goal.status != "paused":
                return None
            goal.status = "running"
            goal.last_error = ""
            goal.worker_lease = ""
            for package in goal.milestones:
                if package.status == "failed":
                    package.status = "pending"
                    package.session_id = None
                elif package.status == "pending":
                    # A cancelled attempt may leave its terminal session id on a
                    # retryable package.  Binding the next attempt must be clean.
                    package.session_id = None
            pending = [m.index for m in goal.milestones if m.status != "done"]
            goal.current_index = min(pending) if pending else len(goal.milestones)
            goal.updated_at = utcnow()
            self._write(conn, goal)
        return goal

    def replan(self, goal_id: str) -> Optional[Goal]:
        """Atomically return an empty paused goal to the planning state.

        A planner/provider failure can legitimately leave no milestones. Such a
        goal must be planned again; treating ``all([])`` as a completed package
        graph would otherwise release an empty build.
        """
        with self._conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT json FROM goals WHERE goal_id = ?", (goal_id,)
            ).fetchone()
            goal = self._decode(row[0]) if row else None
            if (goal is None or goal.status != "paused" or goal.milestones
                    or goal.worker_lease):
                return None
            goal.status = "planning"
            goal.last_error = ""
            goal.release_status = "not_started"
            goal.release_files = []
            goal.release_session_id = None
            goal.updated_at = utcnow()
            self._write(conn, goal)
        return goal

    def park_active(self, goal_id: str, reason: str) -> Optional[Goal]:
        """Restart recovery: park running/planning work and revoke ownership."""
        with self._conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT json FROM goals WHERE goal_id = ?", (goal_id,)).fetchone()
            goal = self._decode(row[0]) if row else None
            if goal is None or goal.status not in ("planning", "running", "draining"):
                return goal
            goal.status = "paused"
            goal.last_error = reason
            goal.epoch += 1
            goal.worker_lease = ""
            # A final-batch goal can have many package workers.  A process restart
            # kills every one of them, not only current_index, so every running
            # binding becomes retryable in the new epoch.
            for package in goal.milestones:
                if package.status == "running":
                    package.status = "pending"
                    package.session_id = None
            goal.updated_at = utcnow()
            self._write(conn, goal)
        return goal

    def bind_milestone(self, goal_id: str, index: int, epoch: int,
                       session_id: str) -> Optional[Goal]:
        """Atomically attach a freshly-created session to the current step.

        The session row is intentionally created before this call.  If a cancel
        wins this transaction, the caller discards that unstarted session rather
        than reviving the goal with a stale read-modify-write save.
        """
        with self._conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT json FROM goals WHERE goal_id = ?", (goal_id,)).fetchone()
            goal = self._decode(row[0]) if row else None
            if (goal is None or goal.status != "running" or goal.epoch != epoch
                    or not (0 <= index < len(goal.milestones))):
                return None
            package = goal.milestones[index]
            if package.status != "pending":
                return None
            if goal.delivery_mode != "final_batch" and goal.current_index != index:
                return None
            if goal.delivery_mode == "final_batch" and not all(
                    goal.milestones[d].status == "done" for d in package.depends_on):
                return None
            package.status = "running"
            package.session_id = session_id
            pending = [m.index for m in goal.milestones if m.status != "done"]
            goal.current_index = min(pending) if pending else len(goal.milestones)
            goal.updated_at = utcnow()
            self._write(conn, goal)
        return goal

    def remove(self, goal_id: str) -> bool:
        with self._conn() as conn:
            cur = conn.execute("DELETE FROM goals WHERE goal_id = ?", (goal_id,))
            return cur.rowcount > 0


def plan_prompt(goal_text: str, panel: Optional[list[str]] = None) -> str:
    """Ask the architect for an owned dependency graph in a strict format."""
    seats = list(dict.fromkeys(s for s in (panel or []) if s))
    roster = ", ".join(seats) or "the available council"
    low_goal = (goal_text or "").lower()
    needs_zero_call_assembly = (
        ("single-file" in low_goal or "single file" in low_goal)
        and ("html" in low_goal or "web" in low_goal or "browser" in low_goal)
    )
    target_count = (
        min(len(seats) + (1 if needs_zero_call_assembly else 0),
            config.GOAL_MAX_MILESTONES)
        if seats else 2
    )
    return (
        "You are the ARCHITECT for a BUILD TEAM. Decompose the goal into bounded, "
        "owned work packages. Models are collaborators, not competing authors: each "
        "must own a distinct component, contract, integration surface, test, or "
        "performance responsibility. Prefer separate modules/files and explicit "
        "interfaces so independent packages can run in parallel without clobbering "
        "one shared file. Later packages consume real staged outputs.\n\n"
        f"Enabled build-team roster: {roster}. For a broad build, create {target_count} "
        "meaningful packages and assign every enabled model once before assigning a "
        f"second package. Never exceed {config.GOAL_MAX_MILESTONES}. For genuinely "
        "small work, use fewer packages instead of inventing duplicate edits. "
        "Package ownership is atomic: the named owner personally authors every output "
        "in that package so tightly coupled HTML, CSS, and JavaScript cannot be blindly "
        "mixed from unrelated implementations. Seven-model parallelism happens across "
        "packages, not within one cohesive package. Group only files whose interfaces "
        "must be designed together. Split responsibilities that need distinct "
        "dependency edges, acceptance behavior, or integration contracts. "
        "Claude and Codex, whenever enabled, must each own a substantive coding "
        "package that produces source files; review, judging, documentation, or a "
        "later rescue role does not satisfy that requirement. Prefer one of them "
        "for the final integration/release package as well.\n\n"
        "The legacy heading MILESTONE 1: is accepted, but prefer PACKAGE headings below.\n\n"
        "Answer in EXACTLY this format, nothing before, between, or after:\n"
        "PACKAGE 1: <short title>\n"
        "OWNER: <exactly one enabled roster name>\n"
        "AFTER: <HARD dependencies whose completed file bytes are required, or NONE>\n"
        "CONTRACTS: <package numbers whose declared interfaces are sufficient, or NONE>\n"
        "TASK: <complete self-contained responsibility and acceptance behavior>\n"
        "OUTPUTS: <comma-separated exclusive relative files, or NONE>\n"
        "RELEASE: <subset of OUTPUTS that the user should receive, or NONE>\n"
        "REQUIRES: <files that MUST physically exist before this package starts, or NONE>\n"
        "ASSEMBLY: <HTML_INLINE for deterministic single-file HTML assembly, or NONE>\n"
        "TEMPLATE: <an accepted upstream HTML template path, OWNER for one compact "
        "glue-only author call, or NONE>\n"
        "INTERFACE: <the API/data/DOM contract this package provides or consumes>\n"
        "CHECK: <optional static check: exactly 'node --check <file.js>' or "
        "'python -m py_compile <file.py>'>\n"
        "PACKAGE 2: <short title>\nOWNER: <roster name>\n"
        "AFTER: <comma-separated 1-based package numbers, or NONE>\n"
        "CONTRACTS: <comma-separated 1-based package numbers, or NONE>\n"
        "TASK: <...>\nOUTPUTS: <...>\nRELEASE: <...>\nREQUIRES: <...>\n"
        "ASSEMBLY: <...>\nTEMPLATE: <...>\n"
        "INTERFACE: <...>\nCHECK: <...>\n\n"
        "OUTPUTS is a staging contract. Two parallel packages may not own the same "
        "path. CONTRACTS never blocks scheduling and is only safe for descriptive work "
        "that does not execute against a future implementation. Any HTML, CSS, or "
        "JavaScript package that consumes another package's runtime API, DOM, timing, "
        "input, audio, or data behavior MUST use AFTER and list the provider outputs in "
        "REQUIRES so it receives the actual accepted bytes. Use AFTER whenever verified "
        "bytes are indispensable before authoring can begin: final assembly, generated "
        "schemas, migrations, revisions of another owner's file, or tests that execute "
        "the assembled system. A broad build should normally start most owners in the "
        "first wave. REQUIRES must list only hard physical inputs; do not list a future "
        "package output only for genuinely non-runtime CONTRACTS work. Every package must include OWNER, "
        "AFTER, CONTRACTS, OUTPUTS, RELEASE, REQUIRES, ASSEMBLY, TEMPLATE, and "
        "INTERFACE. Nothing is delivered per "
        "package; validated outputs remain private staging inputs. RELEASE is the "
        "explicit final-delivery manifest: normally only a final integration/package "
        "declares it, and every RELEASE path must also appear in that package's "
        "OUTPUTS. If the goal asks for a single file, RELEASE must name exactly that "
        "one file. All RELEASE files are reviewed and moved together at the end.\n\n"
        "For a single-file HTML release assembled from staged CSS/JavaScript, use "
        "ASSEMBLY: HTML_INLINE. Prefer an earlier package that owns a compact HTML "
        "shell/template and set TEMPLATE to that accepted path. That exact upstream "
        "TEMPLATE path must also appear in REQUIRES, and its owner "
        "must appear in AFTER. The template uses one "
        "literal '<!-- GANGOF8:STYLE path -->' or '<!-- GANGOF8:SCRIPT path -->' "
        "standalone directive for every source named in REQUIRES. Never put a directive "
        "inside an existing <style> or <script> element because expansion creates the "
        "complete element. The coordinator expands those "
        "accepted files directly without sending or truncating their bodies through a "
        "model. Use TEMPLATE: OWNER only when the integration owner genuinely must author "
        "the small DOM/bootstrap shell. Non-assembly packages use ASSEMBLY: NONE and "
        "TEMPLATE: NONE. Deterministic assembly is concatenation, not integration. Before "
        "it, include a non-assembly integration/QA package owned by a frontier coding "
        "model. That package must use AFTER for every runtime producer, REQUIRES their "
        "actual outputs, explicitly identify integration, QA, acceptance, quality, or "
        "verification in its title/task, and check every user-visible mode and control "
        "against the combined implementation. It must produce a runtime HTML/CSS/JS "
        "artifact that final assembly lists in REQUIRES; a prose QA report does not "
        "integrate the runtime graph.\n\n"
        f"GOAL:\n{goal_text}"
    )


def plan_repair_prompt(
    goal_text: str,
    panel: Optional[list[str]],
    rejected_plan: str,
    validation_errors: list[str],
    attempt: int,
) -> str:
    """Ask for a complete replacement after deterministic plan validation.

    The prior response and exact validator output are deliberately preserved.
    The architect therefore repairs the graph it actually proposed instead of
    guessing why a generic retry was requested.  The normal planning contract
    remains the authority and is repeated in full on every fresh adapter call.
    """
    errors = "\n".join(f"- {error}" for error in validation_errors)
    prior = (rejected_plan or "<empty response>").strip()
    return (
        f"{plan_prompt(goal_text, panel)}\n\n"
        "PLAN REPAIR REQUIRED\n"
        f"This is repair attempt {attempt}. Your previous plan was rejected by "
        "the deterministic coordinator. Return a COMPLETE REPLACEMENT PLAN in "
        "the exact PACKAGE format above; do not return a patch, commentary, or "
        "explanation. You may add, remove, reorder, or merge packages, but the "
        "replacement must eliminate every error below without weakening any "
        "acceptance or release requirement.\n\n"
        f"VALIDATION ERRORS:\n{errors}\n\n"
        f"REJECTED PLAN:\n{prior}\n\n"
        "Now return only the complete corrected PACKAGE plan."
    )


_MILESTONE_RE = re.compile(
    r"^\s*(?:MILESTONE|PACKAGE|WORK\s+PACKAGE)\s+\d+\s*:\s*(.+?)\s*$",
    re.IGNORECASE,
)
_TASK_RE = re.compile(r"^\s*TASK\s*:\s*(.*)$", re.IGNORECASE)
_OUTPUTS_RE = re.compile(r"^\s*OUTPUTS?\s*:\s*(.*)$", re.IGNORECASE)
_RELEASE_RE = re.compile(r"^\s*RELEASES?\s*:\s*(.*)$", re.IGNORECASE)
_REQUIRES_RE = re.compile(r"^\s*REQUIRES?\s*:\s*(.*)$", re.IGNORECASE)
_ASSEMBLY_RE = re.compile(r"^\s*ASSEMBLY\s*:\s*(.*)$", re.IGNORECASE)
_TEMPLATE_RE = re.compile(r"^\s*TEMPLATE\s*:\s*(.*)$", re.IGNORECASE)
_CHECK_RE = re.compile(r"^\s*CHECK\s*:\s*(.*)$", re.IGNORECASE)
_OWNER_RE = re.compile(r"^\s*OWNER\s*:\s*(.*)$", re.IGNORECASE)
_AFTER_RE = re.compile(r"^\s*AFTER\s*:\s*(.*)$", re.IGNORECASE)
_CONTRACTS_RE = re.compile(r"^\s*CONTRACTS?\s*:\s*(.*)$", re.IGNORECASE)
_INTERFACE_RE = re.compile(r"^\s*INTERFACE\s*:\s*(.*)$", re.IGNORECASE)
_BUILD_HINT_RE = re.compile(
    r"\b(?:build|implement|create|write|code|app|application|website|web|html|"
    r"javascript|python|api|service|game|module|file|repo|project)\b",
    re.IGNORECASE,
)


def requires_delivery_contract(goal_text: str) -> bool:
    """Whether a fallback plan must name concrete delivered files.

    A prose/research goal can legitimately finish with ``OUTPUTS: NONE``.  A
    build request cannot; accepting an unplanned single milestone for it was
    the original false-completion route.
    """
    return bool(_BUILD_HINT_RE.search(goal_text or ""))


def should_auto_route(goal_text: str, has_attachments: bool = False) -> bool:
    """Detect a substantial build that needs owned packages, not a tournament.

    The threshold is deliberately conservative: short fixes and questions stay
    ordinary sessions, while long production briefs with several implementation
    surfaces automatically gain parallel owners and one final batch approval.
    """
    if has_attachments:
        return False
    text = (goal_text or "").strip()
    low = text.lower()
    if not text or low.startswith("/goal") or not requires_delivery_contract(text):
        return False
    action = re.search(r"\b(?:build|implement|create|write|develop|overhaul)\b", low)
    if not action:
        return False
    signals = (
        "production-ready", "complete application", "complete app", "single-file",
        "single file", "architecture", "subsystem", "component", "acceptance",
        "responsive", "keyboard", "touch", "audio", "localstorage", "api",
        "tests", "performance", "accessibility", "multiple", "integration",
    )
    signal_count = sum(1 for marker in signals if marker in low)
    structured_lines = sum(1 for line in text.splitlines() if line.strip())
    return bool(
        len(text) >= 1600
        or (len(text) >= 900 and signal_count >= 2)
        or (len(text) >= 650 and signal_count >= 4)
        or (structured_lines >= 18 and signal_count >= 2)
    )


def _relative_paths(value: str) -> tuple[list[str], list[str], bool]:
    """Parse a contract list without ever collapsing or accepting a path.

    The older basename-only comparison made ``src/app.js`` and ``legacy/app.js``
    indistinguishable.  Keep normalized POSIX-relative paths end-to-end and
    reject traversal/drive-qualified planner output instead of guessing.
    """
    raw_items = [item.strip().strip("`") for item in (value or "").split(",")]
    raw_items = [item for item in raw_items if item]
    if len(raw_items) == 1 and raw_items[0].lower() in {"none", "n/a", "-"}:
        return [], [], True
    out: list[str] = []
    errors: list[str] = []
    for raw in raw_items:
        item = raw.replace("\\", "/")
        if (not item or item.startswith("/") or item.startswith("//")
                or re.match(r"^[A-Za-z]:", item)
                or any(part in ("", ".", "..") for part in item.split("/"))):
            errors.append(f"invalid relative path: {raw!r}")
            continue
        if item not in out:
            out.append(item)
    return out, errors, False


def parse_milestones(text: str) -> list[GoalMilestone]:
    """Parse the architect's plan. Tolerant of surrounding prose/fences; returns
    [] when nothing parseable is found (the caller falls back to a single
    milestone: the goal itself — degraded, never broken)."""
    milestones: list[GoalMilestone] = []
    title: Optional[str] = None
    task_lines: list[str] = []
    outputs: list[str] = []
    release_files: list[str] = []
    requires: list[str] = []
    checks: list[str] = []
    owner = ""
    after: list[int] = []
    contract_after: list[int] = []
    interface = ""
    assembly_mode = ""
    assembly_template = ""
    outputs_declared = False
    outputs_none = False
    release_declared = False
    contract_errors: list[str] = []
    in_task = False

    def flush() -> None:
        nonlocal title, task_lines, outputs, release_files, requires, checks, owner, after, contract_after
        nonlocal interface, assembly_mode, assembly_template
        nonlocal outputs_declared, outputs_none, release_declared
        nonlocal contract_errors, in_task
        if title is not None:
            task = "\n".join(task_lines).strip() or title
            milestones.append(GoalMilestone(
                index=len(milestones), title=title, task_text=task,
                package_id=f"wp_{len(milestones) + 1}", owner=owner,
                depends_on=after, contract_depends_on=contract_after,
                interface_contract=interface,
                required_files=outputs, release_files=release_files,
                release_declared=release_declared, dependencies=requires,
                assembly_mode=assembly_mode, assembly_template=assembly_template,
                acceptance_commands=checks,
                contract_declared=outputs_declared,
                requires_delivery=bool(outputs) and not outputs_none,
                contract_error="; ".join(contract_errors),
            ))
        title, task_lines, outputs, release_files, requires, checks = None, [], [], [], [], []
        owner, after, contract_after, interface = "", [], [], ""
        assembly_mode, assembly_template = "", ""
        outputs_declared, outputs_none, release_declared = False, False, False
        contract_errors, in_task = [], False

    for line in (text or "").splitlines():
        m = _MILESTONE_RE.match(line)
        if m:
            flush()
            title = m.group(1).strip()
            continue
        own = _OWNER_RE.match(line)
        if own and title is not None:
            owner = own.group(1).strip().strip("`")
            continue
        aft = _AFTER_RE.match(line)
        if aft and title is not None:
            raw = aft.group(1).strip()
            if raw.lower() not in {"", "none", "n/a", "-"}:
                after = list(dict.fromkeys(
                    int(n) - 1 for n in re.findall(r"\d+", raw) if int(n) > 0
                ))
            continue
        contracts = _CONTRACTS_RE.match(line)
        if contracts and title is not None:
            raw = contracts.group(1).strip()
            if raw.lower() not in {"", "none", "n/a", "-"}:
                contract_after = list(dict.fromkeys(
                    int(n) - 1 for n in re.findall(r"\d+", raw) if int(n) > 0
                ))
            continue
        t = _TASK_RE.match(line)
        if t and title is not None:
            in_task = True
            task_lines.append(t.group(1))
            continue
        o = _OUTPUTS_RE.match(line)
        if o and title is not None:
            outputs_declared = True
            outputs, errors, outputs_none = _relative_paths(o.group(1))
            contract_errors.extend(errors)
            continue
        release = _RELEASE_RE.match(line)
        if release and title is not None:
            release_declared = True
            release_files, errors, _ = _relative_paths(release.group(1))
            contract_errors.extend(errors)
            continue
        req = _REQUIRES_RE.match(line)
        if req and title is not None:
            requires, errors, _ = _relative_paths(req.group(1))
            contract_errors.extend(errors)
            continue
        assembly_match = _ASSEMBLY_RE.match(line)
        if assembly_match and title is not None:
            assembly_mode = assembly.normalize_mode(assembly_match.group(1))
            continue
        template_match = _TEMPLATE_RE.match(line)
        if template_match and title is not None:
            assembly_template = assembly.normalize_template(template_match.group(1))
            if assembly_template and assembly_template != assembly.OWNER_TEMPLATE:
                parsed, errors, _ = _relative_paths(assembly_template)
                contract_errors.extend(errors)
                assembly_template = parsed[0] if len(parsed) == 1 else ""
                if len(parsed) != 1 and not errors:
                    contract_errors.append("TEMPLATE must name exactly one relative file")
            continue
        check = _CHECK_RE.match(line)
        if check and title is not None and check.group(1).strip():
            command = check.group(1).strip()
            # ``CHECK: NONE`` explicitly declares that no automatic static
            # command exists. Persisting the sentinel made valid packages fail.
            if command.strip("` ").lower() not in {
                "none", "n/a", "na", "-", "not applicable", "no check", "no checks",
            }:
                checks.append(command)
            continue
        contract = _INTERFACE_RE.match(line)
        if contract and title is not None:
            interface = contract.group(1).strip()
            continue
        if in_task:
            if line.strip().startswith("```"):
                continue
            task_lines.append(line)
    flush()
    return milestones[: config.GOAL_MAX_MILESTONES]


def compose_milestone_task(goal: Goal, index: int) -> str:
    """The task text a milestone's session actually receives: the current
    milestone framed inside the goal, with what's already delivered (so seats
    build on it) and what is explicitly NOT in scope yet."""
    ms = goal.milestones[index]
    n = len(goal.milestones)
    build_team = goal.collaboration_mode == "build_team"
    parts = [
        (f"[BUILD PACKAGE {index + 1}/{n}] {ms.title}" if build_team
         else f"[GOAL MILESTONE {index + 1}/{n}] {ms.title}"),
        "",
        ("You are the named OWNER of one bounded package in a larger build. Do "
         "the substantive implementation for THIS package and respect its interface. "
         "Your output is staged internally; do not request or emit PROMOTE actions."
         if build_team else
         "This task is one milestone of a larger goal being built step by step. "
         "Do ONLY this milestone now."),
        "",
        f"OVERALL GOAL: {goal.text}",
    ]
    if build_team:
        parts.append(f"PACKAGE OWNER: {ms.owner or 'assigned council member'}")
        if ms.acceptance_detail:
            parts.extend([
                "",
                "RETRY CORRECTION â€” the previous attempt was rejected. Do not repeat "
                "it; correct the concrete failure against the current accepted staging bytes:",
                ms.acceptance_detail,
            ])
        contract_inputs = [
            goal.milestones[d] for d in ms.contract_depends_on
            if 0 <= d < len(goal.milestones) and d != index
        ]
        if contract_inputs:
            parts.append("")
            parts.append(
                "NON-BLOCKING INTERFACE INPUTS — these owners may still be working. "
                "Author against the declared contracts now; do not wait for, read, or "
                "recreate their future files:"
            )
            for dependency in contract_inputs:
                outputs = ", ".join(dependency.required_files) or "no file outputs"
                parts.append(
                    f"- Package {dependency.index + 1}: {dependency.title} "
                    f"(owner {dependency.owner or 'unassigned'}; "
                    f"future outputs: {outputs})"
                )
                parts.append(
                    f"  Contract: {dependency.interface_contract or 'No interface text supplied.'}"
                )
    done = [m for m in goal.milestones if m.status == "done" and m.index != index]
    if done:
        parts.append("")
        parts.append(
            "AVAILABLE IN THE SHARED GOAL STAGING WORKSPACE — consume these real "
            "outputs; do not recreate another owner's component:"
            if build_team else
            "ALREADY COMPLETED — these files exist in the project folder; build "
            "on them, do not recreate them:"
        )
        if goal.staging_root:
            parts.append(f"Shared staging root: {Path(goal.staging_root).resolve()}")
        for m in done:
            accepted = m.accepted_files or m.files
            files = (
                f" (accepted paths: {', '.join(str(Path(f).resolve()) for f in accepted[:6])})"
                if accepted else ""
            )
            parts.append(f"- Milestone {m.index + 1}: {m.title}{files}")
            if m.summary:
                parts.append(f"  Outcome: {m.summary[:config.GOAL_SUMMARY_MAX_CHARS]}")
    parts.append("")
    parts.append("THE PACKAGE TO COMPLETE NOW:" if build_team else "THE MILESTONE TO COMPLETE NOW:")
    parts.append(ms.task_text)
    if ms.interface_contract:
        parts.append("\nINTERFACE CONTRACT:")
        parts.append(ms.interface_contract)
    if ms.required_files:
        parts.append(
            "\nPACKAGE OUTPUT CONTRACT — author all of these exact staged paths:"
            if build_team else
            "\nACCEPTANCE CONTRACT — all of these files must be delivered:"
        )
        parts.extend(f"- {name}" for name in ms.required_files)
    if build_team:
        parts.append("\nFINAL RELEASE CONTRACT:")
        if ms.release_files:
            parts.append(
                "These package outputs are user-facing final deliverables and will be "
                "included in the one aggregate release:"
            )
            parts.extend(f"- {name}" for name in ms.release_files)
        else:
            parts.append(
                "NONE — this package's outputs stay in private staging as integration inputs."
            )
    if ms.dependencies:
        parts.append(
            "\nHARD SOURCE/STAGING DEPENDENCIES — these real files must exist and be "
            "validated; unlike interface inputs, they block this package:"
            if build_team else
            "\nRUNTIME DEPENDENCIES — validate against these delivered files:"
        )
        parts.extend(f"- {name}" for name in ms.dependencies)
    if ms.assembly_mode:
        parts.append("\nDETERMINISTIC ASSEMBLY CONTRACT:")
        parts.append(f"- Mode: {ms.assembly_mode}")
        parts.append(f"- Template: {ms.assembly_template or assembly.OWNER_TEMPLATE}")
        parts.append(
            "Dependency bodies are expanded directly from accepted staging bytes. "
            "Do not request, copy, summarize, or re-emit those files through the model."
        )
    if ms.acceptance_commands:
        parts.append("\nREQUIRED ACCEPTANCE CHECKS:")
        parts.extend(f"- {command}" for command in ms.acceptance_commands)
    other_packages = [m for m in goal.milestones if m.index != index and m.status != "done"]
    if build_team and other_packages:
        parts.append("")
        parts.append(
            "OTHER OWNED PACKAGES — they may run in parallel; they are explicitly OUT "
            "of scope for this owner. These are assignments, not live status indicators:"
        )
        parts.extend(
            f"- Package {m.index + 1}: {m.title} — owner {m.owner or 'unassigned'}"
            for m in other_packages
        )
    else:
        later = [m for m in goal.milestones if m.status == "pending" and m.index != index]
    if not build_team and later:
        parts.append("")
        parts.append("PLANNED LATER — explicitly OUT of scope for this milestone:")
        parts.extend(f"- {m.title}" for m in later)
    return "\n".join(parts)
