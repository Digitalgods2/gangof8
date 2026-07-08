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
    # Per-seat OpenRouter model-slug overrides (seat name → slug). Empty/absent ⇒
    # the built-in default from config.OPENROUTER_SEATS. Edit when the catalog
    # changes a slug.
    openrouter_models: dict[str, str] = {}
    # Explicit panel roster (seat names that contribute every round). Empty ⇒
    # derive per backend: the installed CLI agents plus enabled+keyed
    # OpenRouter seats.
    panel_seats: list[str] = []
    # Per-CLI-seat model pins (agent name → model id, e.g. claude → "opus",
    # gemini → "gemini-2.5-pro"). Empty/absent ⇒ each CLI's own default —
    # note the gemini SDK path defaults to gemini-2.5-flash.
    cli_models: dict[str, str] = {}
    # Per-CLI-seat call timeout in SECONDS (claude/codex/gemini → seconds).
    # Empty/absent for a seat ⇒ config.AGENT_TIMEOUTS default. A thorough seat
    # (claude/opus) can be given more room here so it isn't dropped mid-work; the
    # built-in authoring floors (lead/panel/codifier) still apply as a minimum.
    cli_timeouts: dict[str, int] = {}
    # Per-ROLE model pins (role name → exact model id, or OpenRouter slug for
    # an OpenRouter-mapped role), layered over the seat pin: role pin › seat
    # pin › CLI/seat default. Lets a rarely-called talent run a heavier model
    # than the seat's everyday default — e.g. lead stays on sonnet while
    # code_generator gets opus for the few calls it actually makes.
    role_models: dict[str, str] = {}
    # Which local CLI seats (claude/codex/gemini) are enabled. Absent/true ⇒
    # enabled (today's behaviour). false ⇒ the seat is dropped from the panel and
    # its roles fall back to an enabled OpenRouter seat — letting the app run
    # OpenRouter-only. A disable with no OpenRouter seat to fall back to is
    # ignored (the seat stays) so the council always has a lead.
    cli_enabled: dict[str, bool] = {}
    # Automatic rounds between "continue another n rounds?" consent checks.
    rounds_per_consent: int = Field(default_factory=lambda: config.ROUNDS_PER_CONSENT)


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
