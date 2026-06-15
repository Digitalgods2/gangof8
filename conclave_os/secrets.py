"""Tiny local secrets store for API keys (e.g. OPENROUTER_API_KEY).

Keys are stored in DATA_DIR/secrets.json (DATA_DIR is gitignored). An env var of
the same UPPER_SNAKE name always WINS over the stored value, so CI / shells can
override without touching the file. Single-user, local app — not a vault.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Optional

# secret name -> the env var that overrides it
ENV_OVERRIDES = {
    "openrouter": "OPENROUTER_API_KEY",
}


class SecretStore:
    def __init__(self, data_dir: Path):
        self.path = Path(data_dir) / "secrets.json"

    def _load(self) -> dict:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

    def _save(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        try:  # best-effort: keep the file private on POSIX
            os.chmod(self.path, 0o600)
        except OSError:
            pass

    def get(self, name: str) -> Optional[str]:
        """Resolve a secret: env override wins, else the stored value."""
        env = ENV_OVERRIDES.get(name)
        if env:
            v = os.environ.get(env)
            if v and v.strip():
                return v.strip()
        v = self._load().get(name)
        return v.strip() if isinstance(v, str) and v.strip() else None

    def has(self, name: str) -> bool:
        return self.get(name) is not None

    def source(self, name: str) -> Optional[str]:
        """'env' | 'stored' | None — where the resolved value comes from."""
        env = ENV_OVERRIDES.get(name)
        if env and (os.environ.get(env) or "").strip():
            return "env"
        v = self._load().get(name)
        return "stored" if isinstance(v, str) and v.strip() else None

    def set(self, name: str, value: str) -> None:
        data = self._load()
        if value and value.strip():
            data[name] = value.strip()
        else:
            data.pop(name, None)
        self._save(data)

    def clear(self, name: str) -> None:
        data = self._load()
        if data.pop(name, None) is not None:
            self._save(data)

    @staticmethod
    def mask(value: Optional[str]) -> Optional[str]:
        """A safe-to-display hint, never the full key (e.g. 'sk-or…last4')."""
        if not value:
            return None
        v = value.strip()
        if len(v) <= 8:
            return "•" * len(v)
        return f"{v[:4]}…{v[-4:]}"
