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
# (e.g. "C:\Users\me\proj").
_QUOTED = re.compile(r"""["'`]([^"'`\n]*[\\/][^"'`\n]*)["'`]""")
# A bare Windows drive path (C:\... or C:/...).
_WIN = re.compile(r"([A-Za-z]:[\\/][^\n\"'`<>|]+)")
# A UNC path (\\server\share\...).
_UNC = re.compile(r"(\\\\[^\s\"'`<>|]+)")
# A posix-rooted path (/abs/... or ~/...). Lower confidence — only accepted when
# it actually exists on disk, to avoid grabbing stray slashes in prose.
_POSIX = re.compile(r"(?<![\w])((?:~|/)[\w./\- ]+)")


def _candidates(text: str):
    for rx in (_QUOTED, _WIN, _UNC, _POSIX):
        for m in rx.finditer(text):
            yield rx, m.group(1).strip()


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
            return str(p.parent.resolve())
        if p.is_dir():
            return str(p.resolve())
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
                sp = Path(" ".join(words[:n]))
                if sp.is_dir():
                    return str(sp.resolve())
                if sp.is_file():
                    return str(sp.parent.resolve())
            except OSError:
                continue

    # (2) Still nothing on disk — a brand-new target. Accept only an unambiguous
    #     drive/UNC path, and keep just the FIRST word of the final path segment
    #     as the new dir/file name; anything after it is prose, not path.
    if rx in (_WIN, _UNC) or re.match(r"^[A-Za-z]:[\\/]|^\\\\", cand):
        sep = max(cand.rfind("\\"), cand.rfind("/"))
        if sep <= 0:
            return None
        seg = cand[sep + 1:].split()
        if not seg:
            return None
        try:
            target = Path(cand[:sep + 1] + seg[0])
            return str((target.parent if target.suffix else target).resolve())
        except OSError:
            return None
    return None
