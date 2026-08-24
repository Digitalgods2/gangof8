"""Durable, prompt-injected instructions and verified memory for council seats."""

from __future__ import annotations

import os
import re
import threading
from pathlib import Path
from typing import Iterable


PRODUCTION_SEATS = ("system", "claude", "codex", "gemini", "deepseek", "glm", "qwen", "kimi")
_SAFE_SEAT = re.compile(r"[^a-zA-Z0-9_.-]+")


class SeatProfileStore:
    """Own editable ``AGENT.md`` and append-only verified ``MEMORY.md`` files.

    Packaged profiles are seeds.  Runtime copies live below the application's
    data directory so operator edits and learned lessons survive upgrades and
    restarts.  Every model prompt receives a bounded copy through AgentRegistry.
    """

    def __init__(self, data_dir: Path, *, prompt_char_limit: int = 12_000) -> None:
        self.root = Path(data_dir) / "seats"
        self.templates = Path(__file__).with_name("seat_profiles")
        self.prompt_char_limit = max(2_000, int(prompt_char_limit))
        self._lock = threading.RLock()

    @staticmethod
    def _slug(seat: str) -> str:
        value = _SAFE_SEAT.sub("_", str(seat or "unknown").strip()).strip("._")
        return value or "unknown"

    def _paths(self, seat: str) -> tuple[Path, Path]:
        folder = self.root / self._slug(seat)
        return folder / "AGENT.md", folder / "MEMORY.md"

    @staticmethod
    def _write_once(path: Path, content: str) -> None:
        if path.exists():
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        temporary.write_text(content.rstrip() + "\n", encoding="utf-8")
        try:
            os.replace(temporary, path)
        except OSError:
            if not path.exists():
                raise
            temporary.unlink(missing_ok=True)

    def _seed(self, seat: str, filename: str) -> str:
        candidate = self.templates / self._slug(seat) / filename
        if candidate.is_file():
            return candidate.read_text(encoding="utf-8")
        if filename == "AGENT.md":
            return (
                f"# {seat} council seat\n\n"
                "Serve the current assigned role and lens. Use available evidence, "
                "request needed context, produce the requested protocol, and never "
                "claim success without verification.\n"
            )
        return f"# {seat} verified memory\n\nOnly verified recovery lessons belong here.\n"

    def ensure(self, seat: str) -> tuple[Path, Path]:
        with self._lock:
            agent_path, memory_path = self._paths(seat)
            self._write_once(agent_path, self._seed(seat, "AGENT.md"))
            self._write_once(memory_path, self._seed(seat, "MEMORY.md"))
            return agent_path, memory_path

    def ensure_many(self, seats: Iterable[str]) -> None:
        for seat in dict.fromkeys(str(item) for item in seats if item):
            self.ensure(seat)

    def prompt_context(self, seat: str) -> str:
        agent_path, memory_path = self.ensure(seat)
        with self._lock:
            charter = agent_path.read_text(encoding="utf-8", errors="replace")
            memory = memory_path.read_text(encoding="utf-8", errors="replace")
        # Keep the operator-owned charter intact; retain the newest end of a
        # growing memory file.  The labels prevent either from being confused
        # with untrusted task/source text.
        charter_cap = self.prompt_char_limit // 2
        memory_cap = self.prompt_char_limit - charter_cap
        charter = charter[:charter_cap]
        memory = memory[-memory_cap:]
        return (
            "TRUSTED LOCAL COUNCIL SEAT CONTEXT\n"
            f"Seat: {seat}\n\nAGENT.md\n{charter}\n\n"
            f"MEMORY.md (verified lessons only)\n{memory}\n"
            "END TRUSTED LOCAL COUNCIL SEAT CONTEXT"
        )

    def remember_verified_recovery(
        self,
        seat: str,
        *,
        fault_signature: str,
        category: str,
        strategy: str,
        changed_files: Iterable[str],
        session_id: str,
    ) -> None:
        """Append a deduplicated lesson only after deterministic verification."""
        _agent_path, memory_path = self.ensure(seat)
        key = (fault_signature or f"{category}:{strategy}")[:64]
        marker = f"<!-- recovery:{key} -->"
        files = ", ".join(dict.fromkeys(changed_files)) or "verified output"
        entry = (
            f"\n{marker}\n- Session `{session_id}` verified recovery from "
            f"`{category}` using `{strategy}`; validated change: {files}.\n"
        )
        with self._lock:
            current = memory_path.read_text(encoding="utf-8", errors="replace")
            if marker in current:
                return
            temporary = memory_path.with_name(f".{memory_path.name}.{os.getpid()}.tmp")
            temporary.write_text(current.rstrip() + entry, encoding="utf-8")
            os.replace(temporary, memory_path)

