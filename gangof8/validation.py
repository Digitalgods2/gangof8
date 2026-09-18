"""Bounded command handling for coordinator-owned validation.

There is no OS sandbox behind a process ``cwd``.  Model-emitted commands must
therefore never be fed to a shell automatically.  The small static-check
grammar below is safe to run without a human because it parses/compiles only;
functional commands remain possible, but only after the normal code-execution
approval card is accepted.
"""

from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
import sys
import time
import unicodedata
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


class ValidationCommandError(ValueError):
    pass


class CommandResult(str):
    """String-compatible command report with machine-readable truth.

    Existing callers render and slice the report, so this deliberately remains
    a ``str`` while exposing the exit status that must decide build success.
    """

    def __new__(
        cls,
        text: str,
        *,
        argv: list[str],
        cwd: Path,
        returncode: int,
        stdout: str,
        stderr: str,
        duration_ms: int,
    ):
        obj = super().__new__(cls, text)
        obj.argv = list(argv)
        obj.cwd = str(cwd)
        obj.returncode = int(returncode)
        obj.stdout = stdout
        obj.stderr = stderr
        obj.duration_ms = int(duration_ms)
        return obj

    @property
    def ok(self) -> bool:
        return self.returncode == 0

    def model_dump(self) -> dict:
        return {
            "argv": list(self.argv),
            "cwd": self.cwd,
            "returncode": self.returncode,
            "ok": self.ok,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "duration_ms": self.duration_ms,
            "report": str(self),
        }


@dataclass
class ArtifactValidationResult:
    path: str
    validator_ids: list[str] = field(default_factory=list)
    passed: bool = True
    failures: list[str] = field(default_factory=list)
    evidence: dict = field(default_factory=dict)

    def fail(self, message: str) -> None:
        self.passed = False
        self.failures.append(message)

    def model_dump(self) -> dict:
        return {
            "path": self.path,
            "validator_ids": list(self.validator_ids),
            "passed": self.passed,
            "failures": list(self.failures),
            "evidence": dict(self.evidence),
        }


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
    if os.name == "nt":
        # Non-POSIX shlex keeps quotes. Build evidence shows the interpreter as
        # 'C:\...\python.exe', and a live repair echoed that line back as its
        # BUILD; the stray quote made the program name "python.exe'" and the
        # approved interpreter was refused. Strip one matching outer pair only.
        args = [
            arg[1:-1] if len(arg) >= 2 and arg[0] == arg[-1] and arg[0] in "'\"" else arg
            for arg in args
        ]
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
    if program.endswith(".exe") and program[:-4] in _APPROVED_PROGRAMS:
        program = program[:-4]
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
    # Resolve the approved NAME on PATH, never the path the model typed: a
    # file merely named node(.exe) elsewhere on disk is not the approved tool.
    resolved = shutil.which(program)
    if not resolved:
        raise ValidationCommandError(f"test tool is not on PATH: {args[0]!r}")
    return [resolved, *args[1:]]


# A dependency is a NAME (optionally with extras and a version range) and
# nothing else. Everything pip can be told to fetch from somewhere the human
# did not read — a URL, a VCS ref, a local path, 'pkg @ https://…', an
# alternate index — is rejected here, because the approval card shows this
# string and the card is only a gate if the string cannot mean something else.
# One version constraint per package, not a compound range: comma separates
# packages here AND clauses inside a PEP 508 range, so 'a, b>=1,<2' is genuinely
# ambiguous. A single bound is enough to pin a build, and refusing the ambiguity
# beats guessing which comma the model meant.
_PACKAGE_SPEC = re.compile(
    r"^[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?"            # PEP 508 name
    r"(?:\[[A-Za-z0-9._,-]+\])?"                              # optional extras
    r"(?:\s*(?:===|==|>=|<=|~=|!=|<|>)\s*[A-Za-z0-9._*+!-]+)?$"  # optional version
)
# '<' and '>' are omitted deliberately: they are version operators here, and
# specs are passed to pip as argv elements with shell=False, so they are never
# redirections. The regex above is what constrains where they may appear.
_SPEC_FORBIDDEN = set(":/\\@;&|`$()'\"")


def approved_package_specs(raw: str) -> list[str]:
    """Parse an INSTALL list into plain package requirements, or refuse.

    Only names, extras, and version ranges survive. pip options are rejected
    outright: -e, --index-url, --find-links and friends redirect where the code
    comes from, which is exactly the thing the human is being asked to approve.
    """
    specs = [s.strip() for s in (raw or "").split(",") if s.strip()]
    if not specs:
        raise ValidationCommandError("INSTALL must name at least one package")
    if len(specs) > 20:
        raise ValidationCommandError("INSTALL is limited to 20 packages at a time")
    for spec in specs:
        if spec.startswith("-"):
            raise ValidationCommandError(
                f"pip options are not allowed in INSTALL: {spec!r}")
        if any(ch in _SPEC_FORBIDDEN for ch in spec):
            raise ValidationCommandError(
                f"INSTALL takes package names only — no URLs, paths, or VCS refs: {spec!r}")
        if not _PACKAGE_SPEC.match(spec):
            raise ValidationCommandError(f"not a valid package requirement: {spec!r}")
    return specs


def approved_build_argv(command: str) -> list[str]:
    """Parse a human-approved BUILD command without invoking a shell.

    Same direct-tool allowlist as a functional test: a build legitimately runs a
    project script (`python make_pdf.py`), which is already permitted, while
    shells, shell metacharacters, and -c/-m stay blocked. Those smuggle
    arbitrary inline code past the approval card, and the card only works as a
    gate if the command the human reads is the command that runs.

    Installing dependencies is deliberately not reachable from here: pip
    executes arbitrary package code and reaches the network, which is a
    different decision from running a build the human just read. That is what
    INSTALL / approved_package_specs is for — its own action, its own card."""
    try:
        return approved_test_argv(command)
    except ValidationCommandError as e:
        raise ValidationCommandError(str(e).replace("RUNTESTS", "BUILD")) from e


def run(argv: list[str], cwd: Path, timeout_s: int, output_limit: int,
        env: Optional[dict] = None) -> CommandResult:
    """Run an already-parsed argv with bounded output; never shell-expand it."""
    started = time.monotonic()
    try:
        proc = subprocess.run(
            argv, shell=False, cwd=str(cwd), capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=timeout_s, env=env,
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
    report = f"$ {shown}  (cwd: {cwd})\n[{status}]\n{body}"[:output_limit]
    return CommandResult(
        report,
        argv=argv,
        cwd=cwd,
        returncode=proc.returncode,
        stdout=(proc.stdout or "")[:output_limit],
        stderr=(proc.stderr or "")[:output_limit],
        duration_ms=int((time.monotonic() - started) * 1000),
    )


_PLACEHOLDER_RE = re.compile(
    r"(?:\bTBD\b|\bTODO\b|\bPLACEHOLDER\b|\[insert\s+[^\]]+\])",
    re.IGNORECASE,
)


def _assertion_text(assertions: Optional[list[str]]) -> str:
    return "\n".join(str(item) for item in (assertions or []) if item).lower()


def _validate_pdf(path: Path, assertions: Optional[list[str]]) -> ArtifactValidationResult:
    result = ArtifactValidationResult(
        path=str(path),
        validator_ids=["pdf.structure", "pdf.strict_parse", "pdf.text"],
    )
    try:
        raw = path.read_bytes()
    except OSError as exc:
        result.fail(f"PDF could not be read: {exc}")
        return result
    result.evidence["bytes"] = len(raw)
    if not raw.startswith(b"%PDF-"):
        result.fail("PDF header is missing")
    if b"%%EOF" not in raw[-2048:]:
        result.fail("PDF EOF marker is missing from the trailer")
    if b"startxref" not in raw[-4096:]:
        result.fail("PDF startxref is missing from the trailer")
    try:
        import pypdf

        reader = pypdf.PdfReader(str(path), strict=True)
        pages = len(reader.pages)
        result.evidence["pages"] = pages
        if pages <= 0:
            result.fail("PDF contains no pages")
        extracted = "\n".join((page.extract_text() or "") for page in reader.pages)
        result.evidence["extractable_text_chars"] = len(extracted.strip())
        if not extracted.strip():
            result.fail("PDF contains no extractable text")
        requested = _assertion_text(assertions)
        metadata = reader.metadata or {}
        result.evidence["metadata"] = {
            str(key): str(value)[:300] for key, value in dict(metadata).items()
        }
        if any(word in requested for word in ("metadata", "document title", "pdf title")):
            result.validator_ids.append("pdf.metadata")
            if not str(metadata.get("/Title") or "").strip():
                result.fail("PDF title metadata is required but absent")
        if "bookmark" in requested or "pdf outline" in requested:
            result.validator_ids.append("pdf.outline")
            try:
                outline = reader.outline
            except Exception as exc:  # strict evidence, not a best-effort read
                result.fail(f"PDF outline could not be read: {exc}")
            else:
                result.evidence["outline_items"] = len(outline or [])
                if not outline:
                    result.fail("PDF bookmarks/outline were requested but absent")
        if ("no placeholder" in requested or "without placeholder" in requested
                or "placeholder rejection" in requested):
            result.validator_ids.append("contract.no_placeholders")
            match = _PLACEHOLDER_RE.search(extracted)
            if match:
                result.fail(f"forbidden placeholder text found: {match.group(0)}")
        # A common document contract names the classical mother sauces and asks
        # for their order. Verify their observable text order deterministically.
        fold = lambda value: unicodedata.normalize("NFKD", value).encode(
            "ascii", "ignore").decode("ascii").lower()
        sauces = ["bechamel", "veloute", "espagnole", "hollandaise", "tomato"]
        requested_folded = fold(requested)
        if ("mother sauce" in requested_folded
                and all(name in requested_folded for name in sauces)):
            result.validator_ids.append("contract.mother_sauce_order")
            normalized = fold(extracted)
            positions = [normalized.find(name) for name in sauces]
            result.evidence["mother_sauce_positions"] = dict(zip(sauces, positions))
            if any(position < 0 for position in positions) or positions != sorted(positions):
                result.fail("mother sauces are absent or out of the required order")

        # Quantified document contracts need machine-countable identifiers.
        # This is intentionally opt-in: ordinary prose numbers are not treated
        # as record counts. Contracts that ask for unique IDs make the expected
        # cardinality and index coverage objectively testable before an LLM sees
        # the document.
        count_match = re.search(
            r"\b(\d{1,5})\s+(?:unique\s+)?(?:recipes?|entries|records|items)\b",
            requested,
        )
        unique_id_contract = bool(re.search(
            r"\bunique\s+(?:recipe\s+)?(?:ids?|identifiers?)\b",
            requested,
        ))
        if count_match and unique_id_contract:
            expected_count = int(count_match.group(1))
            result.validator_ids.append("contract.unique_ids")
            label_ids = re.findall(
                r"(?im)\b(?:recipe\s+)?(?:id|identifier)\s*[:#-]\s*"
                r"([A-Za-z][A-Za-z0-9]{0,15}(?:[-_]\d{2,6})|\d{2,8})\b",
                extracted,
            )
            token_ids = re.findall(
                r"\b[A-Za-z][A-Za-z0-9]{1,15}[-_]\d{2,6}\b",
                extracted,
            )
            observed_ids = list(dict.fromkeys(
                item.casefold() for item in [*label_ids, *token_ids]
            ))
            result.evidence["expected_unique_ids"] = expected_count
            result.evidence["observed_unique_ids"] = len(observed_ids)
            if len(observed_ids) != expected_count:
                result.fail(
                    f"contract requires exactly {expected_count} unique IDs; "
                    f"observed {len(observed_ids)}"
                )
            if re.search(r"\bindex\s+(?:coverage|contains|includes|lists)\b", requested):
                result.validator_ids.append("contract.index_coverage")
                index_positions = [
                    match.start() for match in re.finditer(
                        r"(?im)^\s*(?:recipe\s+)?index\s*$", extracted
                    )
                ]
                if not index_positions:
                    result.fail("an ID index was required but no index heading was found")
                else:
                    index_text = extracted[index_positions[-1]:].casefold()
                    indexed = sum(1 for item in observed_ids if item in index_text)
                    result.evidence["indexed_unique_ids"] = indexed
                    if indexed != expected_count:
                        result.fail(
                            f"ID index covers {indexed} of {expected_count} required IDs"
                        )
    except Exception as exc:  # pypdf strict parse includes xref/trailer consistency
        result.fail(f"strict PDF parse failed: {exc}")
    return result


def _validate_zip(path: Path) -> ArtifactValidationResult:
    result = ArtifactValidationResult(path=str(path), validator_ids=["zip.integrity"])
    try:
        with zipfile.ZipFile(path) as archive:
            bad = archive.testzip()
            result.evidence["members"] = len(archive.infolist())
            if bad:
                result.fail(f"archive member failed CRC validation: {bad}")
            if not archive.infolist():
                result.fail("archive contains no files")
    except (OSError, zipfile.BadZipFile) as exc:
        result.fail(f"archive integrity failed: {exc}")
    return result


def validate_artifact(
    path: Path,
    assertions: Optional[list[str]] = None,
) -> ArtifactValidationResult:
    """Dispatch deterministic format checks before any semantic reviewer."""
    path = Path(path)
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        return _validate_pdf(path, assertions)
    if suffix in {".zip", ".docx", ".xlsx", ".pptx"}:
        return _validate_zip(path)
    result = ArtifactValidationResult(path=str(path), validator_ids=["file.nonempty"])
    try:
        raw = path.read_bytes()
    except OSError as exc:
        result.fail(f"artifact could not be read: {exc}")
        return result
    result.evidence["bytes"] = len(raw)
    if not raw:
        result.fail("artifact is empty")
    if suffix == ".json" and raw:
        import json
        result.validator_ids.append("json.strict_parse")
        try:
            json.loads(raw.decode("utf-8"))
        except (UnicodeError, ValueError) as exc:
            result.fail(f"JSON parse failed: {exc}")
    return result
