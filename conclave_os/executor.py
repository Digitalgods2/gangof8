"""Action executor — the ONLY code that performs side effects for agents.

Dispatch is registry-driven: `execute` looks up the handler for
`action.kind` in conclave_os.skills.HANDLERS. Every approval-requiring call
must already carry an approved ApprovalRequest — the kernel enforces that;
the skill handlers enforce their own sandboxes (the shared filename/sandbox
helpers live here so skills.py can reuse them without a circular import).
"""

from __future__ import annotations

import re
from pathlib import Path

from .models import ProposedAction, Session

# Conservative filename whitelist: word chars, dot, dash, space. No path
# separators, no expansion tricks.
_SAFE_NAME = re.compile(r"^[\w.\- ]{1,100}$")


class ExecutionError(Exception):
    pass


def _safe_filename(raw: str) -> str:
    name = Path(raw.strip()).name  # drops any directory components
    if not name or set(name) <= {"."}:
        raise ExecutionError(f"unusable filename: {raw!r}")
    if not _SAFE_NAME.match(name):
        raise ExecutionError(f"filename contains disallowed characters: {name!r}")
    return name


def artifacts_dir(data_dir: Path, session_id: str) -> Path:
    return Path(data_dir) / "artifacts" / session_id


def resolve_in_workspace(root: Path, relpath: str) -> Path:
    """Resolve a relative path inside a workspace root, allowing subdirectories
    (src/main.py) but rejecting anything that escapes the root — `..` traversal,
    absolute paths, or drive-qualified paths. The containment check on the
    resolved path is the real security boundary."""
    raw = (relpath or "").strip().replace("\\", "/")
    if not raw or raw.startswith("/") or ":" in raw.split("/", 1)[0]:
        raise ExecutionError(f"workspace path must be relative: {relpath!r}")
    root = Path(root).resolve()
    target = (root / raw).resolve()
    if target == root:
        raise ExecutionError(f"path resolves to the workspace root itself: {relpath!r}")
    if root not in target.parents:
        raise ExecutionError(f"path escapes the workspace root: {relpath!r}")
    return target


def execute(session: Session, action: ProposedAction, data_dir: Path) -> str:
    """Dispatch the action to its registered skill handler and return the
    handler's result string (e.g. the written path, or file contents)."""
    from .skills import HANDLERS  # local import avoids a circular dependency

    handler = HANDLERS.get(action.kind)
    if handler is None:
        raise ExecutionError(f"unsupported action kind: {action.kind!r}")
    return handler(session, action, Path(data_dir))
