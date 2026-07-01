"""Settings / preferences.

Covers the Settings model + precedence (settings.json › env › config default),
service consumption (no settings.json ⇒ unchanged behaviour), the settings
endpoints, and the local-CLI seat listing.
"""

import json

import pytest
from fastapi.testclient import TestClient

from conclave_os import config
from conclave_os.service import ConclaveService
from conclave_os.settings import Settings, load_settings, save_settings


# ---- Settings model + persistence + precedence -------------------------------


def test_defaults_when_no_file(tmp_path):
    s = load_settings(tmp_path)
    assert s.backend == config.BACKEND
    assert s.role_agents == {}
    assert s.risk_boundary == config.RISK_BOUNDARY.value
    assert s.composer.prose_min_chars == config.COMPOSER_PROSE_MIN_CHARS
    assert s.ui.poll_interval_ms == 3000
    assert s.ui.collapse_finished is True


def test_round_trip(tmp_path):
    s = Settings(backend="cli", role_agents={"critic": "codex"})
    s.ui.poll_interval_ms = 5000
    save_settings(s, tmp_path)
    assert (tmp_path / "settings.json").exists()
    loaded = load_settings(tmp_path)
    assert loaded.backend == "cli"
    assert loaded.role_agents == {"critic": "codex"}
    assert loaded.ui.poll_interval_ms == 5000


def test_partial_file_overlays(tmp_path):
    # Only one nested composer key set; everything else must keep defaults.
    (tmp_path / "settings.json").write_text(
        json.dumps({"composer": {"reserved_calls": 9}}), encoding="utf-8"
    )
    s = load_settings(tmp_path)
    assert s.composer.reserved_calls == 9
    assert s.composer.prose_min_chars == config.COMPOSER_PROSE_MIN_CHARS  # default kept
    assert s.backend == config.BACKEND  # untouched


def test_env_respected_when_file_lacks_key(tmp_path, monkeypatch):
    monkeypatch.setenv("CONCLAVE_OS_BACKEND", "cli")
    import importlib

    from conclave_os import config as cfg
    importlib.reload(cfg)
    import conclave_os.settings as settings_mod
    importlib.reload(settings_mod)
    try:
        # No settings.json ⇒ env-derived config default wins.
        s = settings_mod.load_settings(tmp_path)
        assert s.backend == "cli"
        # settings.json value overrides env.
        (tmp_path / "settings.json").write_text(
            json.dumps({"backend": "mock"}), encoding="utf-8"
        )
        s2 = settings_mod.load_settings(tmp_path)
        assert s2.backend == "mock"
    finally:
        monkeypatch.delenv("CONCLAVE_OS_BACKEND", raising=False)
        importlib.reload(cfg)
        importlib.reload(settings_mod)


def test_corrupt_file_falls_back(tmp_path):
    (tmp_path / "settings.json").write_text("{ not json", encoding="utf-8")
    s = load_settings(tmp_path)
    assert s.backend == config.BACKEND


# ---- Service consumption -----------------------------------------------------


def test_service_unchanged_without_settings_file(tmp_path):
    svc = ConclaveService(data_dir=tmp_path)
    assert svc.backend == config.BACKEND
    assert svc.role_agents == config.ROLE_AGENTS_BY_BACKEND[config.BACKEND]
    assert "mock" in svc.registry.names()


def test_service_consumes_settings_file(tmp_path):
    save_settings(Settings(role_agents={"critic": "codex"}), tmp_path)
    svc = ConclaveService(data_dir=tmp_path)
    from conclave_os.models import Role
    assert svc.role_agents[Role.critic] == "codex"
    assert Role.knowledge_retriever in svc.role_agents
    assert Role.fact_validator in svc.role_agents


def test_explicit_backend_arg_wins(tmp_path):
    save_settings(Settings(backend="cli"), tmp_path)
    svc = ConclaveService(data_dir=tmp_path, backend="mock")
    assert svc.backend == "mock"


def test_update_settings_persists_and_rederives(tmp_path):
    svc = ConclaveService(data_dir=tmp_path)
    svc.update_settings({"ui": {"poll_interval_ms": 7000}})
    assert load_settings(tmp_path).ui.poll_interval_ms == 7000
    assert svc.settings.ui.poll_interval_ms == 7000
    # nested composer still merges (partial patch keeps other composer keys)
    svc.update_settings({"composer": {"reserved_calls": 5}})
    assert svc.settings.composer.reserved_calls == 5
    assert svc.settings.ui.poll_interval_ms == 7000


def test_role_agents_replace_not_merge(tmp_path):
    """role_agents is the complete intended set — a new map replaces the old, so
    resetting (or shrinking) the mapping actually takes effect."""
    svc = ConclaveService(data_dir=tmp_path)
    svc.update_settings({"role_agents": {"researcher": "gemini", "critic": "codex"}})
    assert svc.settings.role_agents == {"researcher": "gemini", "critic": "codex"}
    svc.update_settings({"role_agents": {"researcher": "claude"}})  # replaces, not merges
    assert svc.settings.role_agents == {"researcher": "claude"}


# ---- Endpoints ---------------------------------------------------------------


@pytest.fixture()
def client(tmp_path):
    from conclave_os import main as main_mod

    main_mod.service = ConclaveService(data_dir=tmp_path)
    return TestClient(main_mod.app)


def test_get_settings(client):
    r = client.get("/settings")
    assert r.status_code == 200
    data = r.json()
    assert "backend" in data
    assert "resolved_role_agents" in data
    assert "critic" in data["resolved_role_agents"]
    assert "role_catalog" in data
    assert "knowledge_retriever" in data["role_catalog"]
    assert "fact_validator" in data["role_catalog"]


def test_put_settings_persists_and_reflects(client):
    r = client.put("/settings", json={"ui": {"poll_interval_ms": 4500}})
    assert r.status_code == 200
    assert r.json()["ui"]["poll_interval_ms"] == 4500
    again = client.get("/settings").json()
    assert again["ui"]["poll_interval_ms"] == 4500


def test_put_settings_role_mapping(client):
    r = client.put("/settings", json={"role_agents": {"researcher": "gemini"}})
    assert r.status_code == 200
    assert r.json()["resolved_role_agents"]["researcher"] == "gemini"


def test_seats_lists_cli_and_openrouter_agents(client):
    r = client.get("/settings/seats")
    assert r.status_code == 200
    body = r.json()
    by_name = {s["name"]: s for s in body["seats"]}
    # local CLI seats
    assert {"claude", "codex", "gemini"} <= set(by_name)
    assert all(by_name[a]["kind"] == "cli" for a in ("claude", "codex", "gemini"))
    # OpenRouter seats present, disabled by default, unavailable without a key
    for name in ("deepseek", "glm", "qwen", "kimi"):
        assert by_name[name]["kind"] == "openrouter"
        assert by_name[name]["enabled"] is False
        assert by_name[name]["available"] is False
    assert body["openrouter_key"] is False
