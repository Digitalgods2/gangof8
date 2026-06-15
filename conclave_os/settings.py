"""Persisted, editable settings — the configurable layer over `config.py`.

Precedence (highest first): values present in `DATA_DIR/settings.json` ›
environment variables › code defaults in `config.py`. `load_settings` seeds a
`Settings` from config/env, then overlays whatever the JSON file contains, so a
missing file (or a partial one) yields exactly today's behaviour.

No secrets are ever persisted here. The agent CLIs manage their own auth.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, Field

from . import config
from .models import Budgets


class ComposerSettings(BaseModel):
    prose_min_chars: int = config.COMPOSER_PROSE_MIN_CHARS
    reserved_calls: int = config.COMPOSER_RESERVED_CALLS
    max_critic_tests: int = config.MAX_CRITIC_TESTS_PER_ROUND


class UISettings(BaseModel):
    poll_interval_ms: int = 3000
    collapse_finished: bool = True


class Settings(BaseModel):
    """The full effective configuration. Defaults mirror `config.py`.

    `role_agents` maps role name → seat name; an empty dict means "use the
    backend's default mapping" (config.ROLE_AGENTS_BY_BACKEND). `budgets` holds
    optional per-complexity overrides keyed by complexity name; empty means use
    BUDGETS_BY_COMPLEXITY.
    """

    backend: str = Field(default_factory=lambda: config.BACKEND)
    role_agents: dict[str, str] = {}
    budgets: dict[str, dict] = {}
    risk_boundary: str = Field(default_factory=lambda: config.RISK_BOUNDARY.value)
    composer: ComposerSettings = Field(default_factory=ComposerSettings)
    ui: UISettings = Field(default_factory=UISettings)
    # Which OpenRouter seats are enabled (seat name → bool). Absent/false ⇒ the
    # seat is not registered. Opt-in: they cost per-token and need an API key.
    openrouter_enabled: dict[str, bool] = {}


def _settings_path(data_dir: Optional[Path] = None) -> Path:
    return (Path(data_dir) if data_dir else config.DATA_DIR) / "settings.json"


def load_settings(data_dir: Optional[Path] = None) -> Settings:
    """Return the effective settings: config/env defaults overlaid with any
    values present in settings.json. Absent or partial files are tolerated —
    with no file the result equals the pure config/env defaults."""
    settings = Settings()  # config/env defaults via field default_factories
    path = _settings_path(data_dir)
    if not path.exists():
        return settings
    try:
        stored = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return settings  # unreadable / corrupt file → fall back to defaults
    if not isinstance(stored, dict):
        return settings
    # Overlay only the keys actually present, so partial files keep defaults
    # for everything else (validation re-fills nested model defaults).
    merged = settings.model_dump()
    for key, value in stored.items():
        if key not in merged:
            continue
        if isinstance(merged[key], dict) and isinstance(value, dict):
            merged[key].update(value)
        else:
            merged[key] = value
    return Settings.model_validate(merged)


def save_settings(settings: Settings, data_dir: Optional[Path] = None) -> None:
    """Persist settings to DATA_DIR/settings.json. API keys are never part of
    the Settings model, so nothing secret is written."""
    path = _settings_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(settings.model_dump_json(indent=2), encoding="utf-8")


def budgets_overrides(settings: Settings) -> dict:
    """Build a complexity→Budgets map from settings overrides, falling back to
    config.BUDGETS_BY_COMPLEXITY for any complexity not overridden. Returns the
    config map unchanged when there are no overrides."""
    if not settings.budgets:
        return config.BUDGETS_BY_COMPLEXITY
    merged = {}
    for complexity, base in config.BUDGETS_BY_COMPLEXITY.items():
        override = settings.budgets.get(complexity.value)
        if override:
            merged[complexity] = base.model_copy(update=dict(override))
        else:
            merged[complexity] = base
    return merged
