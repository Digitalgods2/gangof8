"""Extract a filesystem path the user referenced in a task — used to set the
session's established folder (the external real dir the council reads and, with
approval, promotes into).

Per the owner directive: interpreting a path the user EXPLICITLY wrote is not an
"assumption". Inventing an unstated one would be — so this only returns a path
that actually appears in the text. A greenfield build with no referenced path is
left to the loop's greenfield gate, which ASKS the user.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

# A quoted string containing a path separator — the most reliable signal
# (e.g. "C:\Users\me\Benny's Splash.txt"). The closing delimiter must MATCH the
# opening one (backreference), so an apostrophe inside a double-quoted path no
# longer ends the match early.
_QUOTED = re.compile(r"""(?<![\w])(?P<q>["'`])(?P<p>(?:(?!(?P=q)).)*[\\/](?:(?!(?P=q)).)*)(?P=q)""")
# A bare Windows drive path (C:\... or C:/...). Apostrophes are allowed — real
# filenames contain them ("Benny's Splash.txt"); excluding "'" truncated the
# path there, pointing the established folder at the wrong directory.
_WIN = re.compile(r"(?P<p>[A-Za-z]:[\\/][^\n\"`<>|]+)")
# A UNC path (\\server\share\...).
_UNC = re.compile(r"(?P<p>\\\\[^\s\"`<>|]+)")
# A posix-rooted path (/abs/... or ~/...). Existing paths are accepted directly;
# a new leaf is accepted only when its parent exists (see _resolve_root).
_POSIX = re.compile(r"(?<![\w])(?P<p>(?:~|/)[\w./\- ']+)")


def _candidates(text: str):
    for rx in (_QUOTED, _WIN, _UNC, _POSIX):
        for m in rx.finditer(text):
            yield rx, m.group("p").strip()


def extract_established_root(text: str) -> Optional[str]:
    """Return the absolute folder a referenced path points at (a file → its
    parent), or None if the task references no path. Drive/UNC-qualified paths
    are accepted even if they don't exist yet (an intended target); a bare
    posix path must exist to count."""
    for rx, cand in _candidates(text or ""):
        cand = cand.strip().strip("\"'`").rstrip(".,:;) ")
        cand = re.sub(r"\s+(?:please|thanks?|now)$", "", cand, flags=re.IGNORECASE)
        if not cand:
            continue
        root = _resolve_root(cand, rx)
        if root:
            return root
    return None


# Delivery parsing gives an explicit save/output instruction priority over a
# generic "write" instruction. This avoids treating "write a sequel located at
# <source>" as a request to overwrite the source when a later sentence says
# "save the final file to <destination>".
_EXPLICIT_DELIVERY_VERB = (
    r"(?:sav(?:e|ed|ing)|output|export(?:ed|ing)?|plac(?:e|ed|ing)|"
    r"stor(?:e|ed|ing)|deliver(?:ed|ing)?|drop)"
)
_WRITE_DELIVERY_VERB = r"(?:writ(?:e|ing|ten)|put)"
_PATH_START = r"(?=[\"'`/~]|[A-Za-z]:[\\/]|\\\\)"
_EXPLICIT_DELIVERY_RE = re.compile(
    r"\b" + _EXPLICIT_DELIVERY_VERB
    + r"\b[^\n]{0,120}?\b(?:in|into|to|under|at|inside)\b\s*:?\s*" + _PATH_START,
    re.IGNORECASE,
)
_WRITE_DELIVERY_RE = re.compile(
    r"\b" + _WRITE_DELIVERY_VERB
    + r"\b[^\n]{0,70}?\b(?:in|into|to|under|inside)\b\s*:?\s*" + _PATH_START,
    re.IGNORECASE,
)


def extract_delivery_target(text: str) -> Optional[str]:
    """The folder an explicit SAVE/OUTPUT instruction points at ("save it as a
    .txt file in: C:\\...\\tmp", "write the report to <path>"), or None. Distinct
    from extract_established_root, which returns the FIRST path in the task — often
    a READ source ("read Benny's Splash.txt ..."). When a task both reads a source
    and states where to save, this is the destination promote should use, so the
    source folder is never silently overwritten."""
    text = text or ""
    for pattern in (_EXPLICIT_DELIVERY_RE, _WRITE_DELIVERY_RE):
        # A task can name more than one destination while it narrows the request;
        # the last explicit instruction is the final user intent.
        for m in reversed(list(pattern.finditer(text))):
            # Resolve the path that begins right after the preposition (bounded
            # window so we don't wander into a later sentence's path).
            root = extract_established_root(text[m.end(): m.end() + 400])
            if root:
                return root
    return None


def _nonroot(p: Path) -> Optional[str]:
    """A resolved folder — unless it's a filesystem/drive root (C:\\, /, a bare
    UNC share). Those are far too broad to be an established folder and dangerous
    to promote into, so they NEVER qualify (a bare `C:\\` mentioned in prose used
    to win over the actual target because the drive root exists on disk). A more
    specific path elsewhere in the text is chosen instead."""
    try:
        rp = p.resolve()
    except OSError:
        return None
    if rp.parent == rp:  # a root is its own parent
        return None
    return str(rp)


def _resolve_root(cand: str, rx) -> Optional[str]:
    """The established folder a single referenced path points at, or None.

    Windows and UNC paths legally contain spaces, so the greedy regex can swallow
    the prose that follows a path — e.g. "…\\tmp and open it in a browser" was
    taken whole and a folder literally named "tmp and open it in a browser" got
    created. This resolves the real target instead of trusting the raw capture.
    """
    try:
        p = Path(cand)
        if p.is_file():
            return _nonroot(p.parent)
        if p.is_dir():
            return _nonroot(p)
    except OSError:
        return None

    # Not on disk as written. Recover the real target, most-reliable step first.
    words = cand.split()
    if len(words) > 1:
        # (1) The longest whitespace-delimited prefix that EXISTS on disk. This
        #     keeps a genuine "C:\My Games\cool project" intact yet stops at a
        #     real target like "…\tmp" and drops the trailing prose after it.
        for n in range(len(words) - 1, 0, -1):
            try:
                sp = Path(" ".join(words[:n]).rstrip(",.;:)"))
                if sp.is_dir():
                    return _nonroot(sp)
                if sp.is_file():
                    return _nonroot(sp.parent)
            except OSError:
                continue

    # (2) Still nothing on disk: a brand-new target. Keep just the FIRST word
    # of the final path segment as the new dir/file name; anything after it is
    # prose, not path. POSIX targets need an existing parent, which makes a
    # bare slash in prose insufficient to create a delivery root.
    is_windows = rx in (_WIN, _UNC) or bool(re.match(r"^[A-Za-z]:[\\/]|^\\\\", cand))
    is_posix = rx is _POSIX or cand.startswith(("/", "~/"))
    if is_windows or is_posix:
        sep = max(cand.rfind("\\"), cand.rfind("/"))
        if sep <= 0:
            return None
        seg = cand[sep + 1:].split()
        if not seg:
            return None
        try:
            target = Path(cand[:sep + 1] + seg[0].rstrip(",.;:)")).expanduser()
            if is_posix and not target.parent.is_dir():
                return None
            return _nonroot(target.parent if target.suffix else target)
        except OSError:
            return None
    return None


def prior_deliverable_files(established_root: Optional[str], task_text: str) -> list[str]:
    """Basenames in the SOURCE folder that look like a prior version of THIS
    task's deliverable: referenced by TITLE (the file's stem appears in the task)
    but NOT an authorized named input (its full name, with extension, does not).

    A task names its read-source WITH the extension ("read Benny's Splash.txt")
    and its deliverable by TITLE ("write Benny's First Car Ride") — so a
    stem-but-not-name match is a prior/existing answer already sitting in the
    source folder. Seats can read it, and a shipped copy of it would otherwise go
    unnoticed. This only WARNS; blocking a rival CANDIDATE read is a separate,
    stronger guard in the read skill. Stems under 4 chars are ignored to avoid
    matching stray common words."""
    if not established_root:
        return []
    root = Path(established_root)
    if not root.is_dir():
        return []
    out: list[str] = []
    for p in sorted(root.iterdir()):
        stem = p.stem
        if (p.is_file() and stem and len(stem) >= 4
                and stem in task_text and p.name not in task_text):
            out.append(p.name)
    return out
