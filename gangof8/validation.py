"""Bounded command handling for coordinator-owned validation.

There is no OS sandbox behind a process ``cwd``.  Model-emitted commands must
therefore never be fed to a shell automatically.  The small static-check
grammar below is safe to run without a human because it parses/compiles only;
functional commands remain possible, but only after the normal code-execution
approval card is accepted.
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path


class ValidationCommandError(ValueError):
    pass


_SHELLS = {"cmd", "cmd.exe", "powershell", "powershell.exe", "pwsh", "pwsh.exe",
           "sh", "bash", "zsh", "fish"}
_APPROVED_PROGRAMS = {
    "python", "python3", "py", "pytest", "node", "npm", "npx", "ruff",
    "cargo", "go", "dotnet", "java", "mvn", "gradle",
}
_SHELL_META = set("|&;<>()`$")


def _split(command: str) -> list[str]:
    raw = (command or "").strip()
    if not raw:
        raise ValidationCommandError("a test command is required")
    try:
        args = shlex.split(raw, posix=os.name != "nt")
    except ValueError as e:
        raise ValidationCommandError(f"invalid command quoting: {e}") from e
    if not args:
        raise ValidationCommandError("a test command is required")
    return args


def _inside(root: Path, raw: str) -> str:
    item = (raw or "").replace("\\", "/")
    if (not item or item.startswith("/") or item.startswith("//")
            or len(item) >= 2 and item[1] == ":"):
        raise ValidationCommandError(f"check path must be relative: {raw!r}")
    path = (root / item).resolve()
    root = root.resolve()
    if root not in path.parents or not path.is_file():
        raise ValidationCommandError(f"check path is absent or escapes the stage: {raw!r}")
    return str(path)


def static_check_argv(command: str, cwd: Path) -> list[str]:
    """Parse the only automatic validation forms.

    - ``node --check path/to/file.js`` parses JavaScript without running it.
    - ``python -m py_compile path/to/file.py`` compiles Python without running it.
    """
    args = _split(command)
    program = Path(args[0]).name.lower()
    if program in {"node", "node.exe"} and len(args) == 3 and args[1] == "--check":
        node = shutil.which(args[0])
        if not node:
            raise ValidationCommandError("node is not available for static validation")
        return [node, "--check", _inside(cwd, args[2])]
    if (program in {"python", "python.exe", "python3", "py", "py.exe"}
            and len(args) >= 4 and args[1:3] == ["-m", "py_compile"]):
        files = [_inside(cwd, item) for item in args[3:]]
        return [sys.executable, "-m", "py_compile", *files]
    raise ValidationCommandError(
        "automatic CHECK supports only 'node --check <file.js>' or "
        "'python -m py_compile <file.py>'; use RUNTESTS for an approved functional test"
    )


def is_static_check(command: str, cwd: Path) -> bool:
    try:
        static_check_argv(command, cwd)
        return True
    except ValidationCommandError:
        return False


def approved_test_argv(command: str) -> list[str]:
    """Parse a human-approved functional command without invoking a shell."""
    args = _split(command)
    if any(any(char in _SHELL_META for char in arg) for arg in args):
        raise ValidationCommandError("shell operators are not allowed in RUNTESTS")
    program = Path(args[0]).name.lower()
    if program in _SHELLS or program not in _APPROVED_PROGRAMS:
        raise ValidationCommandError(
            "RUNTESTS must invoke an approved direct test tool, not a shell or arbitrary executable"
        )
    if program.startswith("python") or program in {"py", "py.exe"}:
        if any(arg in {"-c", "-m", "--command"} for arg in args[1:]):
            # ``python -m py_compile`` is handled by static_check_argv.  A user
            # may still approve pytest directly, which is clearer in the card.
            raise ValidationCommandError("use pytest directly; python -c/-m is not allowed in RUNTESTS")
        return [sys.executable, *args[1:]]
    resolved = shutil.which(args[0])
    if not resolved:
        raise ValidationCommandError(f"test tool is not on PATH: {args[0]!r}")
    return [resolved, *args[1:]]


def approved_build_argv(command: str) -> list[str]:
    """Parse a human-approved BUILD command without invoking a shell.

    Same direct-tool allowlist as a functional test: a build legitimately runs a
    project script (`python make_pdf.py`), which is already permitted, while
    shells, shell metacharacters, and -c/-m stay blocked. Those smuggle
    arbitrary inline code past the approval card, and the card only works as a
    gate if the command the human reads is the command that runs.

    Installing dependencies is deliberately NOT permitted here: pip executes
    arbitrary package code and reaches the network, which is a different
    decision from running a build the human just read. A build that needs
    missing packages fails with a clear error instead."""
    try:
        return approved_test_argv(command)
    except ValidationCommandError as e:
        raise ValidationCommandError(str(e).replace("RUNTESTS", "BUILD")) from e


def run(argv: list[str], cwd: Path, timeout_s: int, output_limit: int) -> str:
    """Run an already-parsed argv with bounded output; never shell-expand it."""
    try:
        proc = subprocess.run(
            argv, shell=False, cwd=str(cwd), capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=timeout_s,
        )
    except subprocess.TimeoutExpired as e:
        raise ValidationCommandError(f"command timed out after {timeout_s}s") from e
    except OSError as e:
        raise ValidationCommandError(f"could not run command: {e}") from e
    body = proc.stdout or ""
    if proc.stderr:
        body += f"\n[stderr]\n{proc.stderr}"
    status = "passed" if proc.returncode == 0 else f"exit {proc.returncode}"
    shown = " ".join(shlex.quote(a) for a in argv)
    return f"$ {shown}  (cwd: {cwd})\n[{status}]\n{body}"[:output_limit]
