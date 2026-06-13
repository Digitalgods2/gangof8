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
from .executor import ExecutionError, _safe_filename, artifacts_dir, resolve_in_workspace
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


def _sandboxed_path(session: Session, data_dir: Path, raw_name: str) -> Path:
    """Resolve raw_name inside the session's artifacts sandbox, rejecting any
    path that escapes it (flat — directory components are dropped)."""
    name = _safe_filename(raw_name)
    out_dir = artifacts_dir(data_dir, session.session_id)
    path = (out_dir / name).resolve()
    if path.parent != out_dir.resolve():
        raise ExecutionError(f"path escapes the artifacts sandbox: {name!r}")
    return path


def _target_path(session: Session, data_dir: Path, raw_name: str) -> Path:
    """Where a file skill operates: inside the session's workspace root (real
    project, subdirs allowed) when one is bound, else the flat per-session
    artifacts sandbox."""
    if session.workspace_root:
        return resolve_in_workspace(Path(session.workspace_root), raw_name)
    return _sandboxed_path(session, data_dir, raw_name)


def _write_file(session: Session, action: ProposedAction, data_dir: Path) -> str:
    """Write content to the session's workspace (if bound) or artifacts sandbox."""
    raw_name = _arg(action, "filename")
    content = _arg(action, "content")
    path = _target_path(session, data_dir, raw_name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return str(path)


def _read_file(session: Session, action: ProposedAction, data_dir: Path) -> str:
    """Read a file from the session's workspace (if bound) or artifacts sandbox."""
    raw_name = _arg(action, "filename")
    path = _target_path(session, data_dir, raw_name)
    if not path.is_file():
        raise ExecutionError(f"file not found: {raw_name!r}")
    return path.read_text(encoding="utf-8")


def _edit_file(session: Session, action: ProposedAction, data_dir: Path) -> str:
    """Surgically replace a unique OLD snippet with NEW in an existing file
    (workspace or sandbox). Fails if the file is missing or OLD is absent /
    not unique — never a blind overwrite."""
    raw_name = _arg(action, "filename")
    old = _arg(action, "old")
    new = _arg(action, "new")
    if not old:
        raise ExecutionError("edit_file requires non-empty OLD text")
    path = _target_path(session, data_dir, raw_name)
    if not path.is_file():
        raise ExecutionError(f"file not found to edit: {raw_name!r}")
    text = path.read_text(encoding="utf-8")
    count = text.count(old)
    if count == 0:
        raise ExecutionError(f"OLD text not found in {raw_name!r}")
    if count > 1:
        raise ExecutionError(f"OLD text not unique in {raw_name!r} ({count} matches)")
    path.write_text(text.replace(old, new, 1), encoding="utf-8")
    return str(path)


def _run_tests(session: Session, action: ProposedAction, data_dir: Path) -> str:
    """Run a (human-approved) test command in the workspace/sandbox and return
    its output. The first code-execution capability — bounded by timeout and
    output cap; only ever runs after explicit approval (requires_approval)."""
    import subprocess

    cmd = (_arg(action, "command") or "").strip() or "pytest -q"
    cwd = Path(session.workspace_root) if session.workspace_root \
        else artifacts_dir(data_dir, session.session_id)
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


def _search_root(session: Session, data_dir: Path) -> Path:
    if session.workspace_root:
        return Path(session.workspace_root)
    return artifacts_dir(data_dir, session.session_id)


def _search_project(session: Session, action: ProposedAction, data_dir: Path) -> str:
    """Search the workspace (or session sandbox) for a string in file names and
    contents — a bounded, read-only grep so agents can see existing code before
    writing. Returns matching file names and `path:line: text` content hits."""
    query = _arg(action, "query").strip()
    if not query:
        raise ExecutionError("search_project requires a non-empty query")
    root = _search_root(session, data_dir).resolve()
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
        description="Write content to a file in the session's artifacts sandbox.",
        category="file_write",
        risk=Risk.medium,
        requires_approval=True,
        allowed_roles=[Role.implementer],
        inputs=["filename", "content"],
    ),
    "read_file": Skill(
        name="read_file",
        description="Read a file from the workspace (or the session's sandbox).",
        category="read",
        risk=Risk.low,
        requires_approval=False,
        allowed_roles=[Role.researcher, Role.implementer],
        inputs=["filename"],
    ),
    "search_project": Skill(
        name="search_project",
        description="Search the workspace for a string in file names and contents.",
        category="read",
        risk=Risk.low,
        requires_approval=False,
        allowed_roles=[Role.researcher, Role.architect, Role.implementer],
        inputs=["query"],
    ),
    "edit_file": Skill(
        name="edit_file",
        description="Replace a unique snippet in an existing file (surgical edit).",
        category="file_edit",
        risk=Risk.medium,
        requires_approval=True,
        allowed_roles=[Role.implementer],
        inputs=["filename", "old", "new"],
    ),
    "run_tests": Skill(
        name="run_tests",
        description="Run a test command in the workspace and return its output.",
        category="code_exec",
        risk=Risk.high,
        requires_approval=True,
        allowed_roles=[Role.implementer, Role.critic],
        inputs=["command"],
    ),
}

HANDLERS: dict[str, Handler] = {
    "write_file": _write_file,
    "read_file": _read_file,
    "search_project": _search_project,
    "edit_file": _edit_file,
    "run_tests": _run_tests,
}


def get_skill(name: str) -> Optional[Skill]:
    """Return the registered Skill, or None for an unknown name."""
    return SKILLS.get(name)
