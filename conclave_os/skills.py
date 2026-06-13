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

from .executor import ExecutionError, _safe_filename, artifacts_dir, resolve_in_workspace
from .models import ProposedAction, Risk, Role, Session


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
        description="Read a file from the session's artifacts sandbox.",
        category="read",
        risk=Risk.low,
        requires_approval=False,
        allowed_roles=[Role.researcher, Role.implementer],
        inputs=["filename"],
    ),
}

HANDLERS: dict[str, Handler] = {
    "write_file": _write_file,
    "read_file": _read_file,
}


def get_skill(name: str) -> Optional[Skill]:
    """Return the registered Skill, or None for an unknown name."""
    return SKILLS.get(name)
