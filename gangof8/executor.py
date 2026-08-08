"""Action executor — the ONLY code that performs side effects for agents.

Dispatch is registry-driven: `execute` looks up the handler for
`action.kind` in gangof8.skills.HANDLERS. Every approval-requiring call
must already carry an approved ApprovalRequest — the kernel enforces that;
the skill handlers enforce their own sandboxes (the shared filename/sandbox
helpers live here so skills.py can reuse them without a circular import).
"""

from __future__ import annotations

import re
from pathlib import Path

from . import config
from .artifacts import canonical_protocol_filename
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
    """The ephemeral per-session sandbox. Lives under the NEUTRAL config.SANDBOX_ROOT
    (an isolated OS location), NEVER under data_dir or any project/source folder —
    so scratch writes can never touch source material. `data_dir` is accepted for
    call-site compatibility but intentionally not used for the location."""
    return Path(config.SANDBOX_ROOT) / session_id


def resolve_in_workspace(root: Path, relpath: str) -> Path:
    """Resolve a relative path inside a root directory, allowing subdirectories
    (src/main.py) but rejecting anything that escapes the root — `..` traversal,
    absolute paths, or drive-qualified paths. The containment check on the
    resolved path is the real security boundary. Used for every space (sandbox,
    workspace, established) so all three share one escape-proof boundary."""
    raw = (relpath or "").strip().replace("\\", "/")
    if not raw or raw.startswith("/") or ":" in raw.split("/", 1)[0]:
        raise ExecutionError(f"path must be relative: {relpath!r}")
    root = Path(root).resolve()
    target = (root / raw).resolve()
    if target == root:
        raise ExecutionError(f"path resolves to the root itself: {relpath!r}")
    if root not in target.parents:
        raise ExecutionError(f"path escapes the root: {relpath!r}")
    return target


# The three spaces a skill may address. sandbox + workspace are the council's
# OWN areas (free read/write); established is the external real folder (read +
# approval-gated promote target). See the spaces-model design.
SANDBOX, WORKSPACE, ESTABLISHED = "sandbox", "workspace", "established"
SPACES = (SANDBOX, WORKSPACE, ESTABLISHED)


def space_root(session: Session, data_dir: Path, space: str) -> Path:
    """Map a space name to its root directory for this session. Raises if the
    requested space isn't bound (no workspace / no established folder)."""
    if space == WORKSPACE:
        if not session.workspace_root:
            raise ExecutionError("no workspace bound for this session")
        return Path(session.workspace_root)
    if space == ESTABLISHED:
        if not session.established_root:
            raise ExecutionError("no established folder referenced for this task")
        return Path(session.established_root)
    if space == SANDBOX:
        return artifacts_dir(data_dir, session.session_id)
    raise ExecutionError(f"unknown space: {space!r}")


def resolve_space(session: Session, data_dir: Path, space: str, relpath: str) -> Path:
    """Resolve a relative path inside the named space, escape-checked."""
    return resolve_in_workspace(space_root(session, data_dir, space), relpath)


_PERSISTED_ACTION_INPUTS: dict[str, set[str]] = {
    # These fields predate the v1 capability manifest.  They are coordinator
    # metadata or handler inputs already present in saved sessions, so retaining
    # them is required for resume compatibility; no other undeclared keys pass.
    "write_file": {"contract_filename", "package_author"},
    "promote_batch": {"source_hashes"},
}


def _validated_handler(action: ProposedAction):
    """Resolve an action through the live v1 capability contract.

    Governance normally checks role/risk/approval when an action is proposed.
    Dispatch repeats the structural checks because restored sessions and direct
    callers must not be able to reach a stale handler with invented inputs or a
    space the capability does not declare.
    """
    from .skills import HANDLERS, get_skill  # local: skills imports this module

    kind = action.kind
    if not isinstance(kind, str) or not kind:
        raise ExecutionError(f"unsupported action kind: {kind!r}")
    skill = get_skill(kind)
    if skill is None or skill.name != kind:
        raise ExecutionError(f"unsupported action kind: {kind!r}")
    if action.role not in skill.allowed_roles:
        role = getattr(action.role, "value", str(action.role))
        raise ExecutionError(
            f"role {role!r} is not allowed to execute capability {kind!r}"
        )
    if not isinstance(action.args, dict):
        raise ExecutionError(f"capability {kind!r} requires an args object")

    declared = set(skill.inputs)
    compatible = set(_PERSISTED_ACTION_INPUTS.get(kind, set()))
    # ``space`` was the original spelling accepted by _space_arg; ``target`` is
    # the catalogue spelling.  Accept the alias only for capabilities which
    # actually declare a target.
    if "target" in declared:
        compatible.add("space")
    unknown = sorted(
        str(key) for key in action.args
        if not isinstance(key, str) or key not in declared | compatible
    )
    if unknown:
        raise ExecutionError(
            f"capability {kind!r} received undeclared input(s): {', '.join(unknown)}"
        )

    target = action.args.get("target")
    legacy_space = action.args.get("space")
    if target and legacy_space:
        target_name = str(target).strip().lower()
        legacy_name = str(legacy_space).strip().lower()
        if target_name != legacy_name:
            raise ExecutionError(
                f"capability {kind!r} received conflicting target and space inputs"
            )
    selected = target or legacy_space
    if selected:
        if not isinstance(selected, str):
            raise ExecutionError(
                f"capability {kind!r} target must be a space name"
            )
        selected_name = selected.strip().lower()
        if selected_name not in set(skill.permitted_spaces):
            allowed = ", ".join(skill.permitted_spaces) or "none"
            raise ExecutionError(
                f"capability {kind!r} cannot target {selected_name!r} "
                f"(permitted spaces: {allowed})"
            )

    handler = HANDLERS.get(kind)
    if handler is None or not callable(handler):
        raise ExecutionError(f"capability {kind!r} has no executable handler")
    return handler


def execute(session: Session, action: ProposedAction, data_dir: Path) -> str:
    """Dispatch the action to its registered skill handler and return the
    handler's result string (e.g. the written path, or file contents)."""
    handler = _validated_handler(action)

    # Defense in depth for restored sessions and non-parser action producers.
    if action.kind in {"write_file", "edit_file", "promote", "read_file", "stage"}:
        raw = action.args.get("filename", action.filename)
        clean = canonical_protocol_filename(raw)
        if not clean:
            raise ExecutionError(f"unusable filename: {raw!r}")
        action.filename = clean
        action.args["filename"] = clean
    return handler(session, action, Path(data_dir))
