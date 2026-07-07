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


def test_cli_catalog_normalizes_dotted_claude_slugs(tmp_path, monkeypatch):
    # The claude dropdown is fed from OpenRouter's public catalog, which lists
    # Anthropic models with DOTS (claude-opus-4.8) — the claude CLI needs dashes.
    from conclave_os import config
    svc = ConclaveService(data_dir=tmp_path)
    monkeypatch.setattr(config, "WEB_ENABLED", True, raising=False)
    monkeypatch.setattr(svc, "_fetch_public_catalog",
                        lambda: {"claude": ["claude-opus-4.8"], "codex": ["gpt-5.5"]})
    monkeypatch.setattr(svc, "_gemini_sdk_models", lambda: [])
    cat = svc.cli_model_catalog(refresh=True)
    # dotted claude slug corrected to the CLI's dash form; no dotted claude id remains
    assert "claude-opus-4-8" in cat["claude"]
    assert all("." not in m for m in cat["claude"] if m.startswith("claude-"))
    # curated known-good ids still offered; codex dots are NOT touched (they're valid)
    assert "claude-sonnet-5" in cat["claude"]
    assert "gpt-5.5" in cat["codex"]


def test_openrouter_vendor_catalog_maps_vendors_with_capability_flags(tmp_path, monkeypatch):
    from conclave_os import config
    svc = ConclaveService(data_dir=tmp_path)
    monkeypatch.setattr(config, "WEB_ENABLED", True, raising=False)
    monkeypatch.setattr(svc, "_fetch_catalog_raw", lambda: [
        {"id": "deepseek/deepseek-v4-pro", "name": "DeepSeek V4 Pro", "created": 200,
         "context_length": 131072, "architecture": {"input_modalities": ["text", "image"]},
         "supported_parameters": ["reasoning", "tools"]},
        {"id": "moonshotai/kimi-k2.6", "name": "Kimi K2.6", "created": 100,
         "context_length": 200000, "architecture": {"input_modalities": ["text"]},
         "supported_parameters": ["tools"]},
        {"id": "openai/gpt-5", "created": 300},  # not an OpenRouter-seat vendor → ignored
    ])
    cat = svc.openrouter_vendor_catalog(refresh=True)
    ds = next(m for m in cat["deepseek"] if m["id"] == "deepseek/deepseek-v4-pro")
    assert ds["vision"] and ds["reasoning"] and ds["tools"] and ds["ctx"] == 131072
    km = next(m for m in cat["kimi"] if m["id"] == "moonshotai/kimi-k2.6")
    assert not km["vision"] and km["tools"]
    # the non-vendor model was not mis-filed under any seat
    assert all(not m["id"].startswith("openai/") for v in cat.values() for m in v)


def test_seats_openrouter_carry_generic_label_vendor_and_models(tmp_path, monkeypatch):
    svc = ConclaveService(data_dir=tmp_path)
    monkeypatch.setattr(svc, "openrouter_vendor_catalog", lambda refresh=False: {
        "deepseek": [{"id": "deepseek/x", "name": "x", "vision": False, "reasoning": False, "tools": True, "ctx": 1}],
        "glm": [], "qwen": [], "kimi": []})
    seats = {s["name"]: s for s in svc.seats()["seats"] if s["kind"] == "openrouter"}
    assert seats["deepseek"]["label"] == "DeepSeek" and seats["deepseek"]["vendor"] == "deepseek"
    assert seats["glm"]["label"] == "z.ai" and seats["qwen"]["label"] == "Alibaba" and seats["kimi"]["label"] == "Moonshot AI"
    assert seats["deepseek"]["models"][0]["id"] == "deepseek/x"


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


# ---- API keys: gemini is a first-class stored key (env optional) --------------


def _no_gemini_env(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)


def test_gemini_key_stored_in_settings_no_env_needed(tmp_path, monkeypatch):
    _no_gemini_env(monkeypatch)
    svc = ConclaveService(data_dir=tmp_path)
    assert svc.api_key_status("gemini")["present"] is False
    status = svc.set_api_key("gemini", "AIza-test-key-1234")
    assert status["present"] is True and status["source"] == "stored"
    assert "AIza-test-key-1234" not in json.dumps(status), "never returns the full key"
    assert svc.secrets.get("gemini") == "AIza-test-key-1234"
    assert svc.clear_api_key("gemini")["present"] is False


def test_google_api_key_env_also_counts(tmp_path, monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setenv("GOOGLE_API_KEY", "g-env-key")
    svc = ConclaveService(data_dir=tmp_path)
    st = svc.api_key_status("gemini")
    assert st["present"] is True and st["source"] == "env"


def test_unknown_api_key_name_rejected(tmp_path):
    svc = ConclaveService(data_dir=tmp_path)
    with pytest.raises(KeyError):
        svc.api_key_status("stripe")


def test_gemini_key_endpoints(tmp_path, monkeypatch):
    from conclave_os import main as main_mod

    _no_gemini_env(monkeypatch)
    main_mod.service = ConclaveService(data_dir=tmp_path)
    client = TestClient(main_mod.app)
    assert client.get("/settings/api-keys/gemini").json()["present"] is False
    r = client.put("/settings/api-keys/gemini", json={"value": "AIza-abc-9999"})
    assert r.status_code == 200 and r.json()["present"] is True
    assert "9999" in (r.json()["masked"] or ""), "masked hint, not the key"
    assert client.delete("/settings/api-keys/gemini").json()["present"] is False
    assert client.get("/settings/api-keys/stripe").status_code == 404


def test_role_model_pins_reach_only_the_mapped_seat(tmp_path):
    """A per-role model pin is handed to the adapter of the seat its role is
    mapped to — and no other — so pinning code_generator to opus never passes
    a claude model id to gemini's CLI."""
    svc = ConclaveService(data_dir=tmp_path)
    svc.update_settings({"backend": "cli",
                         "role_models": {"code_generator": "claude-opus-4-8",
                                         "researcher": "gemini-2.5-pro"}})
    # default cli mapping: code_generator → claude, researcher → gemini
    assert svc.registry._adapters["claude"].role_models == {"code_generator": "claude-opus-4-8"}
    assert svc.registry._adapters["gemini"].role_models == {"researcher": "gemini-2.5-pro"}
    assert svc.registry._adapters["codex"].role_models == {}


def test_remapping_a_role_drops_its_stale_model_pin(tmp_path):
    """An API patch that remaps a role's seat WITHOUT sending role_models must
    drop that role's pin — the old seat's model id would otherwise ride along
    to the new vendor's CLI and kill the seat. Unrelated pins survive."""
    svc = ConclaveService(data_dir=tmp_path)
    svc.update_settings({"backend": "cli",
                         "role_models": {"code_generator": "claude-opus-4-8",
                                         "researcher": "gemini-2.5-pro"}})
    svc.update_settings({"role_agents": {"code_generator": "codex"}})
    assert "code_generator" not in svc.settings.role_models, "stale pin dropped"
    assert svc.settings.role_models == {"researcher": "gemini-2.5-pro"}
    assert svc.registry._adapters["codex"].role_models == {}
    # persisted too — a restart must not resurrect the stale pin
    assert ConclaveService(data_dir=tmp_path) \
        .settings.role_models == {"researcher": "gemini-2.5-pro"}


def test_fresh_install_saves_keys_without_env(tmp_path, monkeypatch):
    """Brand-new machine: no data/ directory yet, no key env vars. Keys pasted
    in Settings must store to data/secrets.json (created on first save),
    resolve as 'stored', and survive a service restart — the env-var override
    is an option, never a requirement."""
    _no_gemini_env(monkeypatch)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    fresh = tmp_path / "data"  # does not exist — first run must create it
    assert not fresh.exists()
    svc = ConclaveService(data_dir=fresh)
    svc.set_api_key("gemini", "AIza-new-install-1")
    svc.set_api_key("openrouter", "sk-or-new-install-2")
    stored = json.loads((fresh / "secrets.json").read_text(encoding="utf-8"))
    assert stored == {"gemini": "AIza-new-install-1",
                      "openrouter": "sk-or-new-install-2"}
    # a restart (new service over the same data dir) still resolves them
    svc2 = ConclaveService(data_dir=fresh)
    gem = svc2.api_key_status("gemini")
    assert gem["present"] is True and gem["source"] == "stored"
    assert svc2.api_key_status("openrouter")["source"] == "stored"
    assert svc2.reveal_api_key("openrouter")["value"] == "sk-or-new-install-2"


def test_reveal_returns_full_key_only_on_explicit_request(tmp_path, monkeypatch):
    """Status stays masked; the dashboard's eye-reveal fetches the full value
    on demand via the explicit /reveal endpoint (localhost-only app, and the
    key already lives in plaintext in data/secrets.json)."""
    from conclave_os import main as main_mod

    _no_gemini_env(monkeypatch)
    main_mod.service = ConclaveService(data_dir=tmp_path)
    client = TestClient(main_mod.app)
    # nothing stored → present False, empty value (never an error)
    r = client.get("/settings/api-keys/gemini/reveal")
    assert r.status_code == 200
    assert r.json() == {"name": "gemini", "present": False, "value": "", "source": None}
    client.put("/settings/api-keys/gemini", json={"value": "AIza-full-key-5555"})
    assert client.get("/settings/api-keys/gemini/reveal").json()["value"] == "AIza-full-key-5555"
    # the plain status endpoint STILL never returns the full key
    assert "AIza-full-key-5555" not in json.dumps(client.get("/settings/api-keys/gemini").json())
    assert client.get("/settings/api-keys/stripe/reveal").status_code == 404
