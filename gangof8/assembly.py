"""Deterministic materialization for build-team assembly packages.

Models may decide the small amount of integration glue, but they never copy
dependency bodies through a prompt.  An HTML template names validated staged
sources with explicit directives; the coordinator expands those directives
from the accepted files without character or byte truncation.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
from html import escape
from pathlib import Path
import re

from . import executor


HTML_INLINE = "html_inline"
OWNER_TEMPLATE = "owner"

_DIRECTIVE_RE = re.compile(
    r"^[ \t]*<!--\s*GANGOF8:(?P<kind>STYLE|SCRIPT)\s+"
    r"(?P<path>[^\r\n]+?)\s*-->[ \t]*(?:\r?\n)?",
    re.IGNORECASE | re.MULTILINE,
)
_UNRESOLVED_DIRECTIVE_RE = re.compile(
    r"<!--\s*GANGOF8:(?:STYLE|SCRIPT)\b", re.IGNORECASE
)
_SCRIPT_SRC_RE = re.compile(r"<script\b[^>]*\bsrc\s*=", re.IGNORECASE)
_STYLESHEET_LINK_RE = re.compile(
    r"<link\b(?=[^>]*\brel\s*=\s*['\"]?stylesheet\b)[^>]*\bhref\s*=",
    re.IGNORECASE,
)
_CSS_IMPORT_RE = re.compile(r"@import\b", re.IGNORECASE)


class AssemblyError(ValueError):
    """An assembly contract is unsafe, incomplete, or no longer reproducible."""


@dataclass(frozen=True)
class AssemblyResult:
    content: str
    sources: tuple[str, ...]
    source_hashes: dict[str, str]


def normalize_mode(value: str) -> str:
    normalized = re.sub(r"[\s-]+", "_", str(value or "").strip().lower())
    if normalized in {"", "none", "n/a", "na", "-"}:
        return ""
    if normalized in {HTML_INLINE, "inline_html"}:
        return HTML_INLINE
    return normalized


def normalize_template(value: str) -> str:
    normalized = str(value or "").strip().strip("`\"'")
    if normalized.lower() in {"", "none", "n/a", "na", "-"}:
        return ""
    if normalized.lower() in {"owner", "generated", "owner_generated"}:
        return OWNER_TEMPLATE
    return normalized.replace("\\", "/")


def infer_html_inline(
    required_files: list[str], dependencies: list[str], release_files: list[str],
    task_text: str,
) -> bool:
    """Backfill old plans whose final integration contract predates ASSEMBLY.

    Inference is intentionally structural rather than size-based: one HTML
    output, only inlineable staged sources, and explicit assembly/inline wording.
    """
    outputs = [name.replace("\\", "/") for name in required_files]
    deps = [name.replace("\\", "/") for name in dependencies]
    releases = [name.replace("\\", "/") for name in release_files]
    if len(outputs) != 1 or Path(outputs[0]).suffix.lower() not in {".html", ".htm"}:
        return False
    if releases and outputs[0] not in releases:
        return False
    if not deps or any(Path(name).suffix.lower() not in {".css", ".js"}
                       for name in deps):
        return False
    low = (task_text or "").lower()
    return ("inline" in low and "assembl" in low
            and any(phrase in low for phrase in ("single-file", "single file", "one file")))


def directive_contract(sources: list[str]) -> str:
    """Return the exact compact markers an owner-generated template must use."""
    lines: list[str] = []
    for raw in sources:
        name = raw.replace("\\", "/")
        suffix = Path(name).suffix.lower()
        if suffix == ".css":
            lines.append(f"<!-- GANGOF8:STYLE {name} -->")
        elif suffix == ".js":
            lines.append(f"<!-- GANGOF8:SCRIPT {name} -->")
        else:
            raise AssemblyError(f"HTML inline assembly does not support dependency: {name}")
    return "\n".join(lines)


def load_accepted_text(
    root: Path, name: str, expected_hashes: dict[str, str],
) -> tuple[str, str]:
    """Load one manifest-bound UTF-8 file after verifying its accepted hash."""
    path = executor.resolve_in_workspace(root, name)
    if not path.is_file():
        raise AssemblyError(f"assembly dependency is missing: {name}")
    raw = path.read_bytes()
    actual_hash = hashlib.sha256(raw).hexdigest()
    expected_hash = expected_hashes.get(name)
    if not expected_hash:
        raise AssemblyError(f"assembly dependency has no accepted hash: {name}")
    if actual_hash != expected_hash:
        raise AssemblyError(f"assembly dependency changed after acceptance: {name}")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise AssemblyError(f"assembly dependency is not UTF-8 text: {name}") from exc
    return text, actual_hash


def _validated_source(
    root: Path, name: str, expected_hashes: dict[str, str], kind: str,
) -> tuple[str, str]:
    text, actual_hash = load_accepted_text(root, name, expected_hashes)
    closing = re.compile(rf"</\s*{kind}\b", re.IGNORECASE)
    if closing.search(text):
        raise AssemblyError(
            f"cannot inline {name} exactly because it contains a closing </{kind}> token"
        )
    return text, actual_hash


def materialize_html_inline(
    template: str,
    staging_root: Path,
    sources: list[str],
    expected_hashes: dict[str, str],
) -> AssemblyResult:
    """Expand every declared staged source exactly once into an HTML template.

    There is deliberately no size threshold.  The contract is bounded by the
    explicit dependency manifest and accepted hashes, not by arbitrary slices.
    """
    normalized_sources = [name.replace("\\", "/") for name in sources]
    if not normalized_sources:
        raise AssemblyError("HTML inline assembly declares no dependency sources")
    if len(set(normalized_sources)) != len(normalized_sources):
        raise AssemblyError("HTML inline assembly dependency manifest contains duplicates")
    allowed = set(normalized_sources)
    seen: list[str] = []
    hashes: dict[str, str] = {}

    if _SCRIPT_SRC_RE.search(template) or _STYLESHEET_LINK_RE.search(template):
        raise AssemblyError("HTML inline template contains an external script or stylesheet reference")

    def replace(match: re.Match[str]) -> str:
        kind = match.group("kind").lower()
        name = match.group("path").strip().strip("`\"'").replace("\\", "/")
        if name not in allowed:
            raise AssemblyError(f"assembly template references undeclared dependency: {name}")
        if name in seen:
            raise AssemblyError(f"assembly template references a dependency more than once: {name}")
        suffix = Path(name).suffix.lower()
        if kind == "style" and suffix != ".css":
            raise AssemblyError(f"STYLE directive requires a CSS dependency: {name}")
        if kind == "script" and suffix != ".js":
            raise AssemblyError(f"SCRIPT directive requires a JavaScript dependency: {name}")
        source, digest = _validated_source(
            Path(staging_root), name, expected_hashes, kind
        )
        if kind == "style" and _CSS_IMPORT_RE.search(source):
            raise AssemblyError(f"inline stylesheet contains @import and is not self-contained: {name}")
        seen.append(name)
        hashes[name] = digest
        attr = escape(name, quote=True)
        return f'<{kind} data-gangof8-source="{attr}">\n{source}\n</{kind}>\n'

    content = _DIRECTIVE_RE.sub(replace, template)
    if _UNRESOLVED_DIRECTIVE_RE.search(content):
        raise AssemblyError("HTML inline template contains a malformed assembly directive")
    missing = [name for name in normalized_sources if name not in seen]
    if missing:
        raise AssemblyError("assembly template omitted dependencies: " + ", ".join(missing))
    low = content.lower()
    if "<html" not in low or "</html>" not in low:
        raise AssemblyError("assembled HTML template is not a complete document")
    return AssemblyResult(content=content, sources=tuple(seen), source_hashes=hashes)
