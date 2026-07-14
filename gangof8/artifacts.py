"""Artifact marker parsing and body cleanup.

This module contains the pure text-handling pieces used by the deliberation
loop: ARTIFACT/EDIT/RUNTESTS/PROMOTE parsing plus cleanup of model-emitted file
bodies. It deliberately has no orchestration or side effects.
"""

from __future__ import annotations

import re

from .models import ProposedAction, Role


# 'ARTIFACT: <filename>' followed by raw file contents.
ARTIFACT_MARKER = re.compile(
    r"^\s*(?:\*\*)?ARTIFACT(?:\*\*)?\s*:\s*(?:\*\*)?\s*(.+?)\s*(?:\*\*)?\s*$",
    re.IGNORECASE | re.MULTILINE,
)

# Explicit terminator for the v2 artifact envelope.  The older protocol relied
# on the next action marker (or end-of-response), so a model's friendly closing
# explanation became JavaScript bytes.  The terminator makes file boundaries
# independent of whatever the model says afterwards.
ARTIFACT_END_MARKER = re.compile(
    r"^[ \t]*(?:<<<\s*)?END_ARTIFACT(?:\s*>>>)?[ \t]*$",
    re.IGNORECASE | re.MULTILINE,
)

# 'EDIT: <filename>' with conflict-marker-like OLD/NEW blocks.
EDIT_MARKER = re.compile(
    r"^[ \t]*(?:\*\*)?EDIT(?:\*\*)?[ \t]*:[ \t]*(?P<file>.+?)[ \t]*\n"
    r"[ \t]*<{7,}[^\n]*\n(?P<old>.*?)\n[ \t]*={7,}[^\n]*\n(?P<new>.*?)\n[ \t]*>{7,}[^\n]*",
    re.IGNORECASE | re.DOTALL | re.MULTILINE,
)

# 'RUNTESTS: <command>' proposes a free test run; command optional.
RUNTESTS_MARKER = re.compile(
    r"^[ \t]*(?:\*\*)?RUN_?TESTS(?:\*\*)?[ \t]*:[ \t]*(?P<cmd>.*?)[ \t]*$",
    re.IGNORECASE | re.MULTILINE,
)

# 'PROMOTE: <filename>' proposes copying a council file into the established
# folder, the approval-gated boundary that touches real user code.
PROMOTE_MARKER = re.compile(
    r"^[ \t]*(?:\*\*)?PROMOTE(?:\*\*)?[ \t]*:[ \t]*(?:\*\*)?\s*(?P<file>.+?)\s*(?:\*\*)?[ \t]*$",
    re.IGNORECASE | re.MULTILINE,
)

# Any block start bounds ARTIFACT content so later EDIT/RUNTESTS/PROMOTE blocks
# are not swallowed into the file body. A colon is required so ordinary prose in
# a file body is not mistaken for a block boundary.
BLOCK_START = re.compile(
    r"^[ \t]*(?:\*\*)?(?:ARTIFACT|EDIT|RUN_?TESTS|PROMOTE)(?:\*\*)?[ \t]*:",
    re.IGNORECASE | re.MULTILINE,
)

# A codifier can return an ARTIFACT header inside the replacement side of an
# EDIT block. That header is protocol, never document content. Strip it only
# when it is the first line, so ordinary references later in a document remain.
_LEADING_ARTIFACT_HEADER = re.compile(
    r"^\s*(?:\*\*)?ARTIFACT(?:\*\*)?\s*:\s*[^\r\n]+(?:\r?\n|$)",
    re.IGNORECASE,
)


_OUTER_FILENAME_WRAPPERS = (
    ("`", "`"), ("'", "'"), ('"', '"'), ("‘", "’"), ("“", "”"),
)


def canonical_protocol_filename(raw: str) -> str:
    """Strip matching presentation wrappers from a model-emitted path.

    Matching outer Markdown/quote characters are syntax, never filename bytes.
    Internal apostrophes (for example ``Benny's-theme.css``) are preserved.
    """
    name = str(raw or "").strip()
    changed = True
    while name and changed:
        changed = False
        if len(name) >= 4 and name.startswith("**") and name.endswith("**"):
            name = name[2:-2].strip()
            changed = True
            continue
        for left, right in _OUTER_FILENAME_WRAPPERS:
            if len(name) >= 2 and name.startswith(left) and name.endswith(right):
                name = name[len(left):len(name) - len(right)].strip()
                changed = True
                break
    name = name.replace("\\", "/")
    if (not name or any(ord(ch) < 32 for ch in name)
            or name[0] in "`'\"‘“" or name[-1] in "`'\"’”"):
        return ""
    return name


def parse_proposals(sid: str, text: str, role: Role = Role.implementer) -> list[ProposedAction]:
    """Parse marker blocks into ProposedActions in document order."""
    starts = sorted(m.start() for m in BLOCK_START.finditer(text))

    ends = sorted(m.start() for m in ARTIFACT_END_MARKER.finditer(text))

    def content_end(after: int) -> int:
        boundaries = [s for s in starts if s > after]
        boundaries.extend(e for e in ends if e > after)
        return min(boundaries) if boundaries else len(text)

    found: list[tuple[int, ProposedAction]] = []
    for m in ARTIFACT_MARKER.finditer(text):
        fn = canonical_protocol_filename(m.group(1))
        if not fn:
            continue
        body = clean_artifact_body(text[m.end():content_end(m.end())], fn)
        found.append((m.start(), ProposedAction(
            session_id=sid, kind="write_file", role=role,
            filename=fn, content=body, args={"filename": fn, "content": body})))
    for m in EDIT_MARKER.finditer(text):
        fn = canonical_protocol_filename(m.group("file"))
        if not fn:
            continue
        found.append((m.start(), ProposedAction(
            session_id=sid, kind="edit_file", role=role, filename=fn,
            args={"filename": fn, "old": m.group("old"), "new": m.group("new")})))
    for m in RUNTESTS_MARKER.finditer(text):
        cmd = (m.group("cmd") or "").strip()
        found.append((m.start(), ProposedAction(
            session_id=sid, kind="run_tests", role=role,
            filename=cmd or "pytest -q", args={"command": cmd})))
    for m in PROMOTE_MARKER.finditer(text):
        fn = canonical_protocol_filename(m.group("file"))
        if not fn:
            continue
        found.append((m.start(), ProposedAction(
            session_id=sid, kind="promote", role=Role.implementer,
            filename=fn, args={"filename": fn})))
    return [action for _, action in sorted(found, key=lambda t: t[0])]


def strip_code_fence(text: str) -> str:
    """Drop a single wrapping ``` / ```lang fence."""
    t = text.strip()
    if not t.startswith("```"):
        return t
    lines = t.splitlines()
    lines = lines[1:]
    if lines and lines[-1].strip().startswith("```"):
        lines = lines[:-1]
    return "\n".join(lines).strip()


def html_doc_end(low: str) -> int:
    """Index just past the structural </html> in a lowercased HTML string."""
    first = low.find("</html>")
    if first == -1:
        return -1
    body_close = low.rfind("</body>")
    idx = low.rfind("</html>") if (body_close != -1 and first < body_close) else first
    return idx + len("</html>")


def clean_artifact_body(raw: str, filename: str = "") -> str:
    """Extract the real file body from an agent's ARTIFACT content."""
    t = raw.strip()
    while (match := _LEADING_ARTIFACT_HEADER.match(t)):
        t = t[match.end():].lstrip()
    name = filename.lower()
    low = t.lower()
    if name.endswith((".html", ".htm")):
        starts = [i for i in (low.find("<!doctype"), low.find("<html")) if i != -1]
        e = html_doc_end(low)
        if starts and e != -1 and e > min(starts):
            return t[min(starts):e].strip()
    elif name.endswith(".svg"):
        s = low.find("<svg")
        e = low.rfind("</svg>")
        if s != -1 and e != -1 and e + len("</svg>") > s:
            return t[s:e + len("</svg>")].strip()
    if t.startswith("```"):
        lines = t.splitlines()
        fences = [i for i, ln in enumerate(lines) if ln.lstrip().startswith("```")]
        if len(fences) == 2 and fences[0] == 0:
            return "\n".join(lines[1:fences[1]]).strip()
    # Legacy/raw source fallback.  Some coding CLIs obeyed ARTIFACT but omitted
    # the requested fence/end marker, then appended Markdown release notes.  Do
    # not broadly guess where arbitrary prose begins; trim only unmistakable
    # presentation headings after a plausible complete source statement.  The
    # normal syntax/acceptance gate still validates the resulting candidate.
    if name.endswith((".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx", ".css", ".py")):
        lines = t.splitlines()
        presentation = re.compile(
            r"^\s*(?:#{1,6}\s+|\*\*(?:what|summary|implementation|features?|"
            r"changes?|notes?|files?|how\b)[^*]*\*\*)",
            re.IGNORECASE,
        )
        for i, line in enumerate(lines):
            if i < 4 or not presentation.match(line):
                continue
            candidate = "\n".join(lines[:i]).rstrip()
            previous = next((ln.strip() for ln in reversed(lines[:i]) if ln.strip()), "")
            if candidate and previous.endswith((";", "}", ")", "]")):
                return candidate
    return t


def basename(name: str) -> str:
    return name.replace("\\", "/").split("/")[-1].strip()
