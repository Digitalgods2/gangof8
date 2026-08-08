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
from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator

from . import config

SETTINGS_SCHEMA_VERSION = 2
SETTINGS_PROFILE_VERSION = 1


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

    settings_version: int = SETTINGS_SCHEMA_VERSION
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
    # Legacy per-CLI-seat hard-cutoff map retained for profile compatibility.
    # Version-2 migration clears the old 320-second values; normal operation is
    # operator-supervised and has no elapsed-time cutoff.
    cli_timeouts: dict[str, int] = {}
    # Per-ROLE model pins (role name → exact model id, or OpenRouter slug for
    # an OpenRouter-mapped role), layered over the seat pin: role pin › seat
    # pin › CLI/seat default. Lets a rarely-called talent run a heavier model
    # than the seat's everyday default — e.g. lead stays on sonnet while
    # code_generator gets opus for the few calls it actually makes.
    role_models: dict[str, str] = {}
    # Which local CLI seats (claude/codex/gemini) are enabled. Absent/true ⇒
    # enabled (today's behaviour). false ⇒ the seat is dropped from the panel and
    # its roles are redistributed across all remaining enabled seats. If every
    # seat is disabled, task submission is rejected until one is enabled.
    cli_enabled: dict[str, bool] = {}
    # Automatic rounds between "continue another n rounds?" consent checks.
    rounds_per_consent: int = Field(default_factory=lambda: config.ROUNDS_PER_CONSENT)
    # The codifier may offer a validated merge after best-of-N selection. The
    # voted winner remains the default until the human explicitly chooses it.
    integration_review_enabled: bool = True
    # How build-team goals use enabled models. ``adaptive`` convenes every
    # enabled resource for standard/complex code packages while retaining the
    # focused owner+verifier path for small work. ``full_council`` always runs
    # the artifact-aware challenge wave; ``focused`` preserves the lean path.
    participation_mode: Literal["focused", "adaptive", "full_council"] = "adaptive"


# Portable profiles use an explicit allowlist.  Do not replace this with a
# blanket Settings.model_dump(): a future machine-specific path or secret must
# not silently become exportable just because it was added to Settings.
PORTABLE_SETTINGS_FIELDS = (
    "backend",
    "role_agents",
    "role_models",
    "panel_seats",
    "cli_models",
    "cli_timeouts",
    "cli_enabled",
    "openrouter_enabled",
    "openrouter_models",
    "budgets",
    "risk_boundary",
    "composer",
    "rounds_per_consent",
    "integration_review_enabled",
    "participation_mode",
    "ui",
)


class SettingsProfile(BaseModel):
    """Versioned, non-secret settings that can move between installations.

    Workspaces, sandbox roots, delivery paths, uploads, session state, and API
    keys do not exist in this schema.  Unknown fields are rejected instead of
    being ignored so a misleading or unsafe profile cannot appear to import.
    """

    model_config = {"extra": "forbid"}

    profile_version: Literal[SETTINGS_PROFILE_VERSION] = SETTINGS_PROFILE_VERSION
    name: str = "Gang of 8 settings"
    settings: dict[str, object]

    @field_validator("settings")
    @classmethod
    def portable_settings_only(cls, value: dict[str, object]) -> dict[str, object]:
        if not isinstance(value, dict):
            raise ValueError("profile settings must be an object")
        unknown = sorted(set(value) - set(PORTABLE_SETTINGS_FIELDS))
        if unknown:
            raise ValueError(
                "profile contains non-portable or unknown settings: " + ", ".join(unknown)
            )

        # Validate every supplied value through the real Settings model.  A
        # profile may be partial for forward-compatible hand-authored presets;
        # nested composer/UI objects retain their normal defaults.
        candidate = Settings().model_dump()
        for key, item in value.items():
            if isinstance(candidate[key], dict) and isinstance(item, dict):
                candidate[key].update(item)
            else:
                candidate[key] = item
        validated = Settings.model_validate(candidate).model_dump()
        return {key: validated[key] for key in value}


def portable_settings_data(settings: Settings) -> dict:
    """Return only the explicitly portable portion of effective settings."""
    data = settings.model_dump()
    return {key: data[key] for key in PORTABLE_SETTINGS_FIELDS}


def make_settings_profile(
    settings: Settings, name: str = "Gang of 8 settings"
) -> SettingsProfile:
    return SettingsProfile(name=name, settings=portable_settings_data(settings))


def apply_settings_profile(current: Settings, profile: SettingsProfile) -> Settings:
    """Overlay a validated portable profile without touching machine state."""
    merged = current.model_dump()
    for key, value in profile.settings.items():
        # Profile maps represent the complete selection.  Replacing them is
        # important: loading a profile must remove stale seat/model/role pins.
        merged[key] = value
    merged["settings_version"] = SETTINGS_SCHEMA_VERSION
    return Settings.model_validate(merged)


def default_settings_profile_path() -> Path:
    return Path(__file__).with_name("default-settings.json")


def load_default_settings_profile() -> SettingsProfile:
    data = json.loads(default_settings_profile_path().read_text(encoding="utf-8"))
    return SettingsProfile.model_validate(data)


def _settings_path(data_dir: Optional[Path] = None) -> Path:
    return (Path(data_dir) if data_dir else config.DATA_DIR) / "settings.json"


def migrate_settings_data(data: dict) -> dict:
    """Normalize persisted settings before validation.

    Version 2 clears legacy per-seat hard deadlines. They could silently stop
    useful work; explicit installation deadlines now live in environment policy.
    """
    out = dict(data)
    try:
        version = int(out.get("settings_version") or 0)
    except (TypeError, ValueError):
        version = 0
    if version < 2:
        out["cli_timeouts"] = {}
    out["settings_version"] = SETTINGS_SCHEMA_VERSION
    return out


def load_settings(
    data_dir: Optional[Path] = None, *, use_packaged_default: bool = False
) -> Settings:
    """Return the effective settings: config/env defaults overlaid with any
    values present in settings.json. Absent or partial files are tolerated —
    with no file the result equals the pure config/env defaults."""
    settings = Settings()  # config/env defaults via field default_factories
    path = _settings_path(data_dir)
    if not path.exists():
        if use_packaged_default:
            try:
                return apply_settings_profile(settings, load_default_settings_profile())
            except (json.JSONDecodeError, OSError, ValueError):
                # A damaged package must not make the application unstartable.
                # The explicit Load defaults endpoint will surface the error.
                pass
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
    stored = migrate_settings_data(stored)
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
