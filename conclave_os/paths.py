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
_WIN = re.compile(r"([A-Za-z]:[\\/][^\s\"'`<>|]+)")
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
        if not cand:
            continue
        try:
            p = Path(cand)
            if p.is_file():
                return str(p.parent.resolve())
            if p.is_dir():
                return str(p.resolve())
            # not on disk: accept only an unambiguous drive/UNC target
            if rx in (_WIN, _UNC) or re.match(r"^[A-Za-z]:[\\/]|^\\\\", cand):
                return str(p.resolve())
        except OSError:
            continue
    return None
