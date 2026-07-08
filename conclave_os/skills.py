"""Skill Registry — the data-driven catalogue of side-effecting capabilities.

Each Skill lifts what used to be hardcoded write_file literals (category,
risk, approval requirement, allowed roles) into metadata the permission
kernel (conclave_os.governance.authorize_action) reads instead of branching
on action.kind. HANDLERS maps a skill name to the function that performs the
effect, sharing the sandbox helpers in conclave_os.executor.

executor.py must NOT import this module at top level — execute() imports
HANDLERS lazily to keep the dependency one-directional.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable, Optional

from pydantic import BaseModel

from . import config
from .executor import (
    ESTABLISHED,
    SANDBOX,
    WORKSPACE,
    ExecutionError,
    artifacts_dir,
    resolve_in_workspace,
    resolve_space,
    space_root,
)
from .models import ProposedAction, Risk, Role, Session

# Directories never worth searching (vendored / generated / VCS noise).
_SEARCH_SKIP_DIRS = {
    ".git", ".hg", ".svn", "node_modules", ".venv", "venv", "env",
    "__pycache__", ".mypy_cache", ".pytest_cache", "dist", "build",
    ".idea", ".vscode", ".next", "target",
}


class Skill(BaseModel):
    """Metadata for one registry-driven capability. The kernel role-gates and
    gates on this (allowed_roles + requires_approval + category/risk) instead
    of branching on the action kind."""

    name: str
    description: str
    category: str
    risk: Risk
    requires_approval: bool
    allowed_roles: list[Role]
    inputs: list[str]
    blocked_by_default: bool = True


# A handler performs one skill's effect and returns a result string (the
# written path, file contents, etc.). It may raise ExecutionError.
Handler = Callable[[Session, ProposedAction, Path], str]


def _arg(action: ProposedAction, key: str, legacy: str = "") -> str:
    """Prefer the registry-style args dict; fall back to the legacy
    filename/content fields for back-compat with the write_file path."""
    if key in action.args:
        return action.args[key]
    return getattr(action, legacy or key, "")


# --- space targeting (sandbox | workspace | established) ----------------------
# write/edit/run_tests act ONLY in the council's own spaces (sandbox|workspace);
# established is read-only and reached for real only via the gated `promote`.
_WRITE_SPACES = {SANDBOX, WORKSPACE}
_READ_SPACES = {SANDBOX, WORKSPACE, ESTABLISHED}


def _space_arg(action: ProposedAction, default: str, allowed: set[str]) -> str:
    raw = action.args.get("target") or action.args.get("space") or default
    s = str(raw).strip().lower()
    if s not in allowed:
        raise ExecutionError(
            f"invalid target {s!r} (allowed: {', '.join(sorted(allowed))})")
    return s


def _default_read_space(session: Session) -> str:
    """Where a bare read/search/list lands when no target is given: the richest
    bound space — the established folder being examined, else the workspace,
    else the ephemeral sandbox."""
    if session.established_root:
        return ESTABLISHED
    if session.workspace_root:
        return WORKSPACE
    return SANDBOX


def _assert_outside_established(session: Session, path: Path) -> None:
    """Hard guard: refuse any free (council-space) write that would resolve INSIDE
    the established folder OR ANY SUBFOLDER of it (e.g. a workspace mistakenly set
    under it). A subfolder of the source IS the source — it can only be reached by
    an APPROVED promote, never by a free write."""
    if not session.established_root:
        return
    est = Path(session.established_root).resolve()
    p = Path(path).resolve()
    if p == est or est in p.parents:
        raise ExecutionError(
            f"refusing to write inside the established folder ({est}); a subfolder "
            "of the source is still the source — it is reachable only via an "
            "approved promote, never a free write")


def _write_file(session: Session, action: ProposedAction, data_dir: Path) -> str:
    """Write content into a council space (sandbox default, or workspace). Free —
    no approval; the established folder (and any subfolder) is never a write target."""
    raw_name = _arg(action, "filename")
    content = _arg(action, "content")
    if not content.strip():
        raise ExecutionError(
            f"refusing to write empty artifact: {raw_name!r} "
            "(the agent produced no real file body)"
        )
    target = _space_arg(action, SANDBOX, _WRITE_SPACES)
    path = resolve_space(session, data_dir, target, raw_name)
    _assert_outside_established(session, path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # newline="" writes the model's bytes verbatim — no CRLF translation. Windows'
    # default would rewrite every \n to \r\n (turning emitted CRLF into \r\r\n and
    # breaking LF-critical files: shebangs, .sh scripts).
    path.write_text(content, encoding="utf-8", newline="")
    return str(path)


def _abs_read_inside_space(session: Session, data_dir: Path, raw: str) -> Optional[Path]:
    """If `raw` is an ABSOLUTE path that resolves INSIDE a bound space
    (established, workspace, or sandbox), return it resolved. Reads are
    non-destructive, so a model that cites the full path the USER wrote in the
    task should get the file rather than a 'must be relative' refusal (live:
    every seat was refused the source file it named in full, then invented the
    story from scratch). The boundary holds: anything outside every bound root
    returns None, and `..` is neutralized by resolving before the containment
    test."""
    raw = (raw or "").strip().strip("\"'`")
    if not raw:
        return None
    is_abs = ((len(raw) >= 2 and raw[1] == ":" and raw[0].isalpha())
              or raw.startswith("\\\\") or raw.startswith("//") or Path(raw).is_absolute())
    if not is_abs:
        return None
    try:
        target = Path(raw).resolve()
    except OSError:
        return None
    for space in (ESTABLISHED, WORKSPACE, SANDBOX):
        if space == ESTABLISHED and not session.established_root:
            continue
        if space == WORKSPACE and not session.workspace_root:
            continue
        try:
            root = space_root(session, data_dir, space).resolve()
        except (ExecutionError, OSError):
            continue
        if (target == root or root in target.parents) and target.is_file():
            return target
    return None


def _sandbox_candidate_bases(session: Session, data_dir: Path) -> set[str]:
    """Deliverable basenames the panel drafted to the sandbox THIS round — files
    saved namespaced '<agent>__<base>' → '<base>'. These are the candidates being
    judged; a file by the same name elsewhere is a copy of one, not source."""
    out: set[str] = set()
    d = artifacts_dir(data_dir, session.session_id)
    if not d.is_dir():
        return out
    for f in d.iterdir():
        if f.is_file() and "__" in f.name:
            out.add(f.name.split("__", 1)[1])
    return out


def _guard_rival_read(session: Session, data_dir: Path, path: Path, raw_name: str) -> None:
    """Refuse a panel read of a file that sits INSIDE the source folder but whose
    name is a candidate deliverable being produced and judged this round — a prior
    or rival answer to the very task at hand. Left open, a seat can pull a prior
    run's 'Benny's First Car Ride.txt' out of the source folder and crib it,
    contaminating the blind best-of-N (this is exactly what happened: three seats
    read a prior version nobody authorized). The task-NAMED source (its full name,
    with extension, appears in the task) is always allowed — that is real source,
    not a rival answer, so genuine 'read the file I told you to' reads still work."""
    if not session.established_root:
        return
    try:
        est = Path(session.established_root).resolve()
        p = Path(path).resolve()
    except OSError:
        return
    if not (p == est or est in p.parents):
        return  # not inside the source folder — sandbox/workspace reads are free
    if p.name and p.name in (session.task.text or ""):
        return  # the task named this exact file as source — authorized
    if p.name not in _sandbox_candidate_bases(session, data_dir):
        return  # not a candidate deliverable — an ordinary source read, allowed
    session.unresolved.append(
        f"blocked a panel read of '{p.name}' from the source folder — it matches a "
        "candidate being produced and judged this round (a prior/rival answer)")
    raise ExecutionError(
        f"refusing to read {raw_name!r}: a file by that name is a candidate "
        "deliverable being produced and judged this round — author your own; do "
        "not copy a prior or rival answer sitting in the source folder")


def _read_file(session: Session, action: ProposedAction, data_dir: Path) -> str:
    """Read a file from any space. With an explicit target, that space only.
    With none, every bound space is tried — default (richest) first, then the
    others — because the old single-space default made council-authored
    SANDBOX drafts unreadable whenever an established folder was bound (live:
    'Skill failed: read_file centipede.html' while the file sat right there
    in the sandbox, twice across two runs). An absolute path the user named in
    the task is honored when it lands inside a bound space (reads are safe)."""
    raw_name = _arg(action, "filename")
    abs_hit = _abs_read_inside_space(session, data_dir, raw_name)
    if abs_hit is not None:
        _guard_rival_read(session, data_dir, abs_hit, raw_name)
        return abs_hit.read_text(encoding="utf-8")
    if action.args.get("target") or action.args.get("space"):
        target = _space_arg(action, _default_read_space(session), _READ_SPACES)
        path = resolve_space(session, data_dir, target, raw_name)
        if not path.is_file():
            raise ExecutionError(f"file not found: {raw_name!r}")
        _guard_rival_read(session, data_dir, path, raw_name)
        return path.read_text(encoding="utf-8")
    default = _default_read_space(session)
    order = [default] + [s for s in (SANDBOX, WORKSPACE, ESTABLISHED) if s != default]
    tried: list[str] = []
    for target in order:
        if target == WORKSPACE and not session.workspace_root:
            continue
        if target == ESTABLISHED and not session.established_root:
            continue
        try:
            path = resolve_space(session, data_dir, target, raw_name)
        except ExecutionError:
            continue  # e.g. an absolute/escaping path for this space
        tried.append(target)
        if path.is_file():
            _guard_rival_read(session, data_dir, path, raw_name)
            return path.read_text(encoding="utf-8")
    raise ExecutionError(
        f"file not found in any space ({', '.join(tried) or 'none bound'}): {raw_name!r}")


def _edit_file(session: Session, action: ProposedAction, data_dir: Path) -> str:
    """Surgically replace a unique OLD snippet with NEW in an existing file in a
    council space (sandbox default, or workspace). Fails if the file is missing
    or OLD is absent / not unique — never a blind overwrite. The established
    folder is never edited directly (changes land there only via `promote`)."""
    raw_name = _arg(action, "filename")
    # Normalize CRLF in the model-supplied snippets to \n; the on-disk file is
    # read with universal newlines (→ \n), so a CRLF-emitting backend would
    # otherwise never match an otherwise-correct OLD snippet.
    old = _arg(action, "old").replace("\r\n", "\n").replace("\r", "\n")
    new = _arg(action, "new").replace("\r\n", "\n").replace("\r", "\n")
    if not old:
        raise ExecutionError("edit_file requires non-empty OLD text")
    target = _space_arg(action, SANDBOX, _WRITE_SPACES)
    path = resolve_space(session, data_dir, target, raw_name)
    _assert_outside_established(session, path)
    if not path.is_file():
        raise ExecutionError(f"file not found to edit: {raw_name!r}")
    text = path.read_text(encoding="utf-8")
    count = text.count(old)
    if count == 0:
        raise ExecutionError(f"OLD text not found in {raw_name!r}")
    if count > 1:
        raise ExecutionError(f"OLD text not unique in {raw_name!r} ({count} matches)")
    path.write_text(text.replace(old, new, 1), encoding="utf-8", newline="")
    return str(path)


def _run_tests(session: Session, action: ProposedAction, data_dir: Path) -> str:
    """Run a test command in a council space (sandbox default, or workspace) and
    return its output. Free — no approval (the council's own scratch); still
    bounded by timeout and output cap."""
    import subprocess

    cmd = (_arg(action, "command") or "").strip() or "pytest -q"
    target = _space_arg(action, SANDBOX, _WRITE_SPACES)
    cwd = space_root(session, data_dir, target)
    _assert_outside_established(session, cwd)  # never execute/write inside the source tree
    cwd.mkdir(parents=True, exist_ok=True)
    if not cwd.is_dir():
        raise ExecutionError("no directory to run tests in")
    try:
        proc = subprocess.run(
            cmd, shell=True, cwd=str(cwd), capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=config.RUN_TESTS_TIMEOUT,
        )
    except subprocess.TimeoutExpired as e:
        raise ExecutionError(f"command timed out after {config.RUN_TESTS_TIMEOUT}s") from e
    except OSError as e:
        raise ExecutionError(f"could not run command: {e}") from e
    body = (proc.stdout or "")
    if proc.stderr:
        body += f"\n[stderr]\n{proc.stderr}"
    status = "passed" if proc.returncode == 0 else f"exit {proc.returncode}"
    return f"$ {cmd}  (cwd: {cwd})\n[{status}]\n{body}"[: config.RUN_TESTS_OUTPUT_MAX_CHARS]


def _stage(session: Session, action: ProposedAction, data_dir: Path) -> str:
    """Move a file UP from the ephemeral sandbox into the permanent workspace —
    the council keeping work worth carrying across sessions. Free (no approval);
    both are the council's own spaces."""
    raw_name = _arg(action, "filename")
    src = resolve_space(session, data_dir, SANDBOX, raw_name)
    if not src.is_file():
        raise ExecutionError(f"nothing to stage (not in sandbox): {raw_name!r}")
    dst = resolve_space(session, data_dir, WORKSPACE, raw_name)
    _assert_outside_established(session, dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_bytes(src.read_bytes())
    return str(dst)


def _promote_source(session: Session, data_dir: Path, raw_name: str) -> Optional[Path]:
    """The council file `promote` would deliver: prefer the permanent workspace,
    fall back to the sandbox (so an ARTIFACT written to scratch can promote
    without an explicit stage)."""
    if session.workspace_root:
        try:
            ws_root = Path(session.workspace_root).resolve()
            est_root = Path(session.established_root).resolve() if session.established_root else None
        except OSError:
            ws_root = None
            est_root = None
        # If the active workspace is the established folder (or inside it), it is
        # not council-owned source material. Promotion must come from sandbox.
        workspace_is_established = (
            ws_root is not None
            and est_root is not None
            and (ws_root == est_root or est_root in ws_root.parents)
        )
        if not workspace_is_established:
            ws = resolve_space(session, data_dir, WORKSPACE, raw_name)
            if ws.is_file():
                return ws
    sb = resolve_space(session, data_dir, SANDBOX, raw_name)
    return sb if sb.is_file() else None


def _promote_dest(session: Session, data_dir: Path, raw_name: str) -> Path:
    """Where `promote` LANDS. An explicit delivery target the task named ("save
    it in <X>") WINS over the established source folder, so a "read from A, save
    to B" task delivers to B and never overwrites the source A. Falls back to the
    established folder (the historical in-place promote target)."""
    if session.delivery_root:
        root = Path(session.delivery_root)
        root.mkdir(parents=True, exist_ok=True)
        return resolve_in_workspace(root, raw_name)
    return resolve_space(session, data_dir, ESTABLISHED, raw_name)


def promote_diff(session: Session, data_dir: Path, raw_name: str) -> str:
    """Unified diff of what `promote` would change at the delivery target: the
    existing file there (if any) → the council version. Shown in the approval so
    the human sees exactly what lands in their real folder."""
    import difflib

    src = _promote_source(session, data_dir, raw_name)
    if src is not None and src.stat().st_size == 0:
        return f"REFUSING PROMOTE: council/{raw_name} is empty (0 bytes)"
    new = src.read_text(encoding="utf-8", errors="replace") if src else ""
    dst = _promote_dest(session, data_dir, raw_name)
    old = dst.read_text(encoding="utf-8", errors="replace") if dst.is_file() else ""
    label = "new file" if not old else "modified"
    diff = "".join(difflib.unified_diff(
        old.splitlines(keepends=True), new.splitlines(keepends=True),
        fromfile=f"established/{raw_name} ({label})", tofile=f"council/{raw_name}",
    ))
    return (diff or "(no textual difference)")[: config.PROMOTE_DIFF_MAX_CHARS]


def _promote(session: Session, action: ProposedAction, data_dir: Path) -> str:
    """Copy a council file (workspace, else sandbox) INTO the external established
    folder. The ONLY skill that writes real, user-owned code — approval-gated."""
    raw_name = _arg(action, "filename")
    src = _promote_source(session, data_dir, raw_name)
    if src is None:
        raise ExecutionError(f"nothing to promote (not in workspace/sandbox): {raw_name!r}")
    if src.stat().st_size == 0:
        raise ExecutionError(f"refusing to promote empty artifact: {raw_name!r}")
    dst = _promote_dest(session, data_dir, raw_name)
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_bytes(src.read_bytes())
    return str(dst)


def _web_search(session: Session, action: ProposedAction, data_dir: Path) -> str:
    """Answer a query with live web grounding (the coordinator does the search)."""
    from . import web

    if not config.WEB_ENABLED:
        raise ExecutionError("web access is disabled (CONCLAVE_OS_WEB=0)")
    query = _arg(action, "query").strip()
    if not query:
        raise ExecutionError("web_search requires a non-empty query")
    try:
        return web.web_search(query)
    except web.WebError as e:
        raise ExecutionError(str(e)) from e


def _web_fetch(session: Session, action: ProposedAction, data_dir: Path) -> str:
    """Fetch a public http(s) URL and return its readable text."""
    from . import web

    if not config.WEB_ENABLED:
        raise ExecutionError("web access is disabled (CONCLAVE_OS_WEB=0)")
    url = _arg(action, "url").strip()
    if not url:
        raise ExecutionError("web_fetch requires a URL")
    try:
        return web.web_fetch(url)
    except web.WebError as e:
        raise ExecutionError(str(e)) from e


def _search_root(session: Session, data_dir: Path, action: ProposedAction | None = None) -> Path:
    target = _space_arg(action, _default_read_space(session), _READ_SPACES) \
        if action is not None else _default_read_space(session)
    return space_root(session, data_dir, target)


def _fmt_size(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f} KB"
    return f"{n / (1024 * 1024):.1f} MB"


def _list_dir(session: Session, action: ProposedAction, data_dir: Path) -> str:
    """List the workspace (or session sandbox) directory tree so agents can
    DISCOVER what files exist before reading or writing — the missing first step
    for open-ended 'examine this app' tasks. Read-only, no approval; bounded by
    entry count and depth. The positional arg is a subdirectory ('.' or empty =
    the workspace root)."""
    space = _space_arg(action, _default_read_space(session), _READ_SPACES)
    base = space_root(session, data_dir, space).resolve()
    if not base.is_dir():
        return "No directory to list."
    raw = (_arg(action, "path") or "").strip().strip("/").replace("\\", "/")
    if raw in ("", "."):
        target = base
    else:
        target = resolve_space(session, data_dir, space, raw)
    if not target.is_dir():
        raise ExecutionError(f"not a directory: {raw or '.'!r}")

    # Breadth-first (shallow entries first) so the top-level layout — the most
    # useful view for "what is this app" — survives the entry cap instead of
    # being crowded out by deeply-nested files.
    entries: list[str] = []
    truncated = False
    everything = sorted(
        target.rglob("*"),
        key=lambda p: (len(p.relative_to(target).parts),
                       p.relative_to(target).as_posix().lower()),
    )
    for p in everything:
        rel_parts = p.relative_to(target).parts
        if any(part in _SEARCH_SKIP_DIRS for part in rel_parts):
            continue
        if len(rel_parts) > config.LIST_DIR_MAX_DEPTH:
            continue
        if len(entries) >= config.LIST_DIR_MAX_ENTRIES:
            truncated = True
            break
        rel = p.relative_to(target).as_posix()
        if p.is_dir():
            entries.append(f"  {rel}/")
        else:
            try:
                size = _fmt_size(p.stat().st_size)
            except OSError:
                size = "?"
            entries.append(f"  {rel}  ({size})")

    label = target.name or "project"
    if not entries:
        return f"{label}/ is empty."
    head = (f"{label}/ — {len(entries)} entr{'y' if len(entries) == 1 else 'ies'}"
            + (" (truncated)" if truncated else "") + ":\n")
    return (head + "\n".join(entries))[: config.LIST_DIR_RESULT_MAX_CHARS]


def _search_project(session: Session, action: ProposedAction, data_dir: Path) -> str:
    """Search the workspace (or session sandbox) for a string in file names and
    contents — a bounded, read-only grep so agents can see existing code before
    writing. Returns matching file names and `path:line: text` content hits."""
    query = _arg(action, "query").strip()
    if not query:
        raise ExecutionError("search_project requires a non-empty query")
    root = _search_root(session, data_dir, action).resolve()
    if not root.is_dir():
        return f"No project directory to search for {query!r}."

    q = query.lower()
    name_hits: list[str] = []
    matches: list[str] = []
    scanned = 0
    for p in sorted(root.rglob("*")):
        if any(part in _SEARCH_SKIP_DIRS for part in p.relative_to(root).parts):
            continue
        if not p.is_file():
            continue
        rel = p.relative_to(root).as_posix()
        if q in rel.lower():
            name_hits.append(rel)
        if scanned >= config.SEARCH_MAX_FILES or len(matches) >= config.SEARCH_MAX_MATCHES:
            continue
        try:
            if p.stat().st_size > config.SEARCH_MAX_FILE_BYTES:
                continue
            text = p.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue  # unreadable / binary
        scanned += 1
        for lineno, line in enumerate(text.splitlines(), 1):
            if q in line.lower():
                matches.append(f"  {rel}:{lineno}: {line.strip()[:200]}")
                if len(matches) >= config.SEARCH_MAX_MATCHES:
                    break

    if not name_hits and not matches:
        return f"No matches for {query!r} in {root.name or 'the project'}."
    parts = []
    if name_hits:
        shown = name_hits[:30]
        parts.append("Files with matching names:\n" + "\n".join(f"  {n}" for n in shown)
                     + ("\n  …" if len(name_hits) > len(shown) else ""))
    if matches:
        parts.append(f"Content matches ({len(matches)}):\n" + "\n".join(matches))
    return "\n\n".join(parts)[: config.SEARCH_RESULT_MAX_CHARS]


SKILLS: dict[str, Skill] = {
    "write_file": Skill(
        name="write_file",
        description="Write a file into a council space (sandbox default, or workspace). Free, no approval.",
        category="file_write",
        risk=Risk.low,
        requires_approval=False,
        # Council-space writes are free for EVERY seat (owner directive: a role
        # unable to land its work in the sandbox is a design failure — the
        # roster is pointless if the lead ends up writing everything). The one
        # boundary is promote: the only path to the user's real folder, still
        # lead/implementer + a human approval.
        allowed_roles=list(Role),
        inputs=["filename", "content", "target"],
    ),
    "read_file": Skill(
        name="read_file",
        description="Read a file from any space (sandbox/workspace/established).",
        category="read",
        risk=Risk.low,
        requires_approval=False,
        allowed_roles=list(Role),  # discovery is free for every seat (incl. panelists)
        inputs=["filename", "target"],
    ),
    "search_project": Skill(
        name="search_project",
        description="Search a space for a string in file names and contents.",
        category="read",
        risk=Risk.low,
        requires_approval=False,
        allowed_roles=list(Role),  # discovery is free for every seat (incl. panelists)
        inputs=["query", "target"],
    ),
    "list_dir": Skill(
        name="list_dir",
        description="List the files/folders in a space (sandbox/workspace/established) "
                    "so you can see what exists before reading or writing.",
        category="read",
        risk=Risk.low,
        requires_approval=False,
        allowed_roles=list(Role),  # discovery is free for every seat (incl. panelists)
        inputs=["path", "target"],
    ),
    "web_search": Skill(
        name="web_search",
        description="Search the live web and get a cited answer (use for current "
                    "facts, docs, libraries, prior art).",
        category="web",
        risk=Risk.low,
        requires_approval=False,
        allowed_roles=list(Role),  # governed web lookups: free for every seat
        inputs=["query"],
    ),
    "web_fetch": Skill(
        name="web_fetch",
        description="Fetch a specific public URL and read its text.",
        category="web",
        risk=Risk.low,
        requires_approval=False,
        allowed_roles=list(Role),  # governed web lookups: free for every seat
        inputs=["url"],
    ),
    "edit_file": Skill(
        name="edit_file",
        description="Surgically replace a unique snippet in a council-space file (sandbox/workspace). Free, no approval.",
        category="file_edit",
        risk=Risk.low,
        requires_approval=False,
        allowed_roles=list(Role),  # council-space edits: free for every seat
        inputs=["filename", "old", "new", "target"],
    ),
    "run_tests": Skill(
        name="run_tests",
        description="Run a test command in a council space (sandbox/workspace). Free, no approval.",
        category="code_exec",
        risk=Risk.medium,
        requires_approval=False,
        allowed_roles=[Role.lead, Role.implementer, Role.critic, Role.code_generator],
        inputs=["command", "target"],
    ),
    "stage": Skill(
        name="stage",
        description="Keep a sandbox file by moving it up into the permanent workspace. Free, no approval.",
        category="stage",
        risk=Risk.low,
        requires_approval=False,
        allowed_roles=[Role.lead, Role.implementer],
        inputs=["filename"],
    ),
    "promote": Skill(
        name="promote",
        description="Copy a workspace file INTO the external established folder (real code). Requires human approval.",
        category="promote",
        risk=Risk.medium,
        requires_approval=True,
        allowed_roles=[Role.lead, Role.implementer],
        inputs=["filename"],
    ),
}

HANDLERS: dict[str, Handler] = {
    "write_file": _write_file,
    "read_file": _read_file,
    "search_project": _search_project,
    "list_dir": _list_dir,
    "web_search": _web_search,
    "web_fetch": _web_fetch,
    "edit_file": _edit_file,
    "run_tests": _run_tests,
    "stage": _stage,
    "promote": _promote,
}


def get_skill(name: str) -> Optional[Skill]:
    """Return the registered Skill, or None for an unknown name."""
    return SKILLS.get(name)
