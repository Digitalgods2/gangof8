"""Milestone 6 Part B: settings / preferences / API keys.

Covers the Settings model + precedence (settings.json › env › config default),
service consumption (no settings.json ⇒ unchanged behaviour), and the new
FastAPI endpoints including the Switchboard-unreachable proxy paths.
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
    assert s.switchboard_url == config.SWITCHBOARD_URL
    assert s.role_agents == {}
    assert s.risk_boundary == config.RISK_BOUNDARY.value
    assert s.composer.prose_min_chars == config.COMPOSER_PROSE_MIN_CHARS
    assert s.ui.poll_interval_ms == 3000
    assert s.ui.collapse_finished is True


def test_round_trip(tmp_path):
    s = Settings(backend="switchboard", role_agents={"critic": "deepseek"})
    s.ui.poll_interval_ms = 5000
    save_settings(s, tmp_path)
    assert (tmp_path / "settings.json").exists()
    loaded = load_settings(tmp_path)
    assert loaded.backend == "switchboard"
    assert loaded.role_agents == {"critic": "deepseek"}
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
    monkeypatch.setenv("CONCLAVE_OS_BACKEND", "switchboard")
    import importlib

    from conclave_os import config as cfg
    importlib.reload(cfg)
    import conclave_os.settings as settings_mod
    importlib.reload(settings_mod)
    try:
        # No settings.json ⇒ env-derived config default wins.
        s = settings_mod.load_settings(tmp_path)
        assert s.backend == "switchboard"
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


def test_explicit_backend_arg_wins(tmp_path):
    save_settings(Settings(backend="switchboard"), tmp_path)
    svc = ConclaveService(data_dir=tmp_path, backend="mock")
    assert svc.backend == "mock"


def test_update_settings_persists_and_rederives(tmp_path):
    svc = ConclaveService(data_dir=tmp_path)
    svc.update_settings({"ui": {"poll_interval_ms": 7000}})
    assert load_settings(tmp_path).ui.poll_interval_ms == 7000
    assert svc.settings.ui.poll_interval_ms == 7000


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


def test_put_settings_persists_and_reflects(client):
    r = client.put("/settings", json={"ui": {"poll_interval_ms": 4500}})
    assert r.status_code == 200
    assert r.json()["ui"]["poll_interval_ms"] == 4500
    again = client.get("/settings").json()
    assert again["ui"]["poll_interval_ms"] == 4500


def test_put_settings_role_mapping(client):
    r = client.put("/settings", json={"role_agents": {"researcher": "qwen"}})
    assert r.status_code == 200
    assert r.json()["resolved_role_agents"]["researcher"] == "qwen"


# Point the Switchboard at an unused port so proxy calls fail fast in CI.
@pytest.fixture()
def offline_client(tmp_path):
    from conclave_os import main as main_mod

    save_settings(Settings(switchboard_url="http://127.0.0.1:1"), tmp_path)
    main_mod.service = ConclaveService(data_dir=tmp_path)
    return TestClient(main_mod.app)


def test_seats_graceful_when_unreachable(offline_client):
    r = offline_client.get("/settings/seats")
    assert r.status_code == 200
    data = r.json()
    assert data["seats"] == []
    assert "error" in data


def test_api_keys_graceful_when_unreachable(offline_client):
    r = offline_client.get("/settings/api-keys")
    assert r.status_code == 200
    data = r.json()
    assert data["keys"] == {}
    assert "error" in data


def test_set_api_key_error_when_unreachable(offline_client):
    r = offline_client.put("/settings/api-keys/openrouter", json={"value": "x"})
    assert r.status_code == 502
    assert "detail" in r.json()
