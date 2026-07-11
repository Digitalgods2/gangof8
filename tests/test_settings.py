"""Settings / preferences.

Covers the Settings model + precedence (settings.json › env › config default),
service consumption (no settings.json ⇒ unchanged behaviour), the settings
endpoints, and the local-CLI seat listing.
"""

import json

import pytest
from fastapi.testclient import TestClient

from gangof8 import config
from gangof8.models import (
    IntegrationProposal,
    ProposedAction,
    Role,
    SESSION_SCHEMA_VERSION,
    SessionStatus,
)
from gangof8.service import GangOf8Service
from gangof8.sessions import migrate_session_data
from gangof8.settings import Settings, load_settings, save_settings


class _UnauthenticatedPanelAdapter:
    name = "authless"

    def auth_status(self):
        return False, "not logged in"

    def call(self, role, prompt, timeout_s, images=None):
        raise AssertionError("preflight must remove this seat before it is called")


# ---- Settings model + persistence + precedence -------------------------------


def test_defaults_when_no_file(tmp_path):
    s = load_settings(tmp_path)
    assert s.settings_version == 1
    assert s.backend == config.BACKEND
    assert s.role_agents == {}
    assert s.risk_boundary == config.RISK_BOUNDARY.value
    assert s.composer.prose_min_chars == config.COMPOSER_PROSE_MIN_CHARS
    assert s.ui.poll_interval_ms == 3000
    assert s.ui.collapse_finished is True


def test_preflight_removes_unauthenticated_panel_seat(tmp_path):
    svc = GangOf8Service(data_dir=tmp_path, panel=["authless", "mock"])
    svc.registry.register(_UnauthenticatedPanelAdapter())
    session = svc._open("Explain SQLite.", source="test", budgets=None)

    assert session.panel == ["mock"]
    assert any("authless" in note and "unavailable before run" in note
               for note in session.unresolved)
    timeline = svc.timeline(session.session_id)["events"]
    assert any(event["event"] == "panel_seat_preflight_failed" for event in timeline)


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
    monkeypatch.setenv("GANGOF8_BACKEND", "cli")
    import importlib

    from gangof8 import config as cfg
    importlib.reload(cfg)
    import gangof8.settings as settings_mod
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
        monkeypatch.delenv("GANGOF8_BACKEND", raising=False)
        importlib.reload(cfg)
        importlib.reload(settings_mod)


def test_corrupt_file_falls_back(tmp_path):
    (tmp_path / "settings.json").write_text("{ not json", encoding="utf-8")
    s = load_settings(tmp_path)
    assert s.backend == config.BACKEND


def test_settings_migration_stamps_current_version(tmp_path):
    (tmp_path / "settings.json").write_text('{"backend":"mock"}', encoding="utf-8")
    assert load_settings(tmp_path).settings_version == 1


def test_session_migration_stamps_current_version():
    data = {"session_id": "s_1", "task": {"task_id": "t_1", "session_id": "s_1", "text": "x"}}
    assert migrate_session_data(data)["schema_version"] == SESSION_SCHEMA_VERSION


# ---- Service consumption -----------------------------------------------------


def test_service_unchanged_without_settings_file(tmp_path):
    svc = GangOf8Service(data_dir=tmp_path)
    assert svc.backend == config.BACKEND
    assert svc.role_agents == config.ROLE_AGENTS_BY_BACKEND[config.BACKEND]
    assert "mock" in svc.registry.names()


def test_integration_review_defaults_on_and_is_stamped_on_new_sessions(tmp_path):
    svc = GangOf8Service(data_dir=tmp_path)
    assert svc.settings.integration_review_enabled is True
    assert svc._open("build a thing", "test", None).integration_review_enabled is True


def test_human_can_adopt_or_keep_the_voted_winner(tmp_path, monkeypatch):
    """The integration proposal cannot replace the winner without this choice."""
    from gangof8 import loop
    import gangof8.service as service_mod

    svc = GangOf8Service(data_dir=tmp_path)
    session = svc._open("build a thing", "test", None)
    svc.manager.transition(session, SessionStatus.classified)
    svc.manager.transition(session, SessionStatus.deliberating)
    session.proposed_actions.append(ProposedAction(
        session_id=session.session_id, kind="write_file", role=Role.implementer,
        filename="game.html", content="voted winner", args={"filename": "game.html", "content": "voted winner"},
    ))
    proposal = IntegrationProposal(
        filename="game.html", content="integrated candidate", rationale="combines the best details",
        source_candidates=["Candidate 1", "Candidate 2"],
    )
    loop._pause_for_integration_decision(session, svc.manager, svc.store, proposal)
    req = session.input_requests[-1]
    monkeypatch.setattr(service_mod, "resume_deliberation", lambda session, *_args, **_kwargs: session)

    decided = svc.answer(session.session_id, req.input_id, "use integration")
    write = next(a for a in decided.proposed_actions if a.kind == "write_file")
    assert write.content == "integrated candidate"
    assert decided.integration_proposal.status == "adopted"


def test_service_consumes_settings_file(tmp_path):
    save_settings(Settings(role_agents={"critic": "codex"}), tmp_path)
    svc = GangOf8Service(data_dir=tmp_path)
    from gangof8.models import Role
    assert svc.role_agents[Role.critic] == "codex"
    assert Role.knowledge_retriever in svc.role_agents
    assert Role.fact_validator in svc.role_agents


def test_explicit_backend_arg_wins(tmp_path):
    save_settings(Settings(backend="cli"), tmp_path)
    svc = GangOf8Service(data_dir=tmp_path, backend="mock")
    assert svc.backend == "mock"


def test_cli_catalog_normalizes_dotted_claude_slugs(tmp_path, monkeypatch):
    # The claude dropdown is fed from OpenRouter's public catalog, which lists
    # Anthropic models with DOTS (claude-opus-4.8) — the claude CLI needs dashes.
    from gangof8 import config
    svc = GangOf8Service(data_dir=tmp_path)
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
    from gangof8 import config
    svc = GangOf8Service(data_dir=tmp_path)
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
    svc = GangOf8Service(data_dir=tmp_path)
    monkeypatch.setattr(svc, "openrouter_vendor_catalog", lambda refresh=False: {
        "deepseek": [{"id": "deepseek/x", "name": "x", "vision": False, "reasoning": False, "tools": True, "ctx": 1}],
        "glm": [], "qwen": [], "kimi": []})
    seats = {s["name"]: s for s in svc.seats()["seats"] if s["kind"] == "openrouter"}
    assert seats["deepseek"]["label"] == "DeepSeek" and seats["deepseek"]["vendor"] == "deepseek"
    assert seats["glm"]["label"] == "z.ai" and seats["qwen"]["label"] == "Alibaba" and seats["kimi"]["label"] == "Moonshot AI"
    assert seats["deepseek"]["models"][0]["id"] == "deepseek/x"


def test_gc_sandboxes_keeps_active_and_recent_deletes_the_rest(tmp_path, monkeypatch):
    import os
    from gangof8 import config
    svc = GangOf8Service(data_dir=tmp_path / "data")
    sbroot = tmp_path / "sandbox"
    sbroot.mkdir()
    monkeypatch.setattr(config, "SANDBOX_ROOT", sbroot)
    (sbroot / "cli-neutral").mkdir()          # shared CLI working dir — must NEVER be GC'd
    for i in range(8):                        # s_00 (oldest) .. s_07 (newest)
        d = sbroot / f"s_{i:02d}"
        d.mkdir()
        os.utime(d, (1000 + i, 1000 + i))
    # s_00 is the OLDEST but still ACTIVE → must be kept regardless of age
    monkeypatch.setattr(svc.store, "list_sessions",
                        lambda limit=500: [{"session_id": "s_00", "status": "deliberating"}])
    out = svc._gc_sandboxes(keep=3)
    remaining = {d.name for d in sbroot.iterdir()}
    assert "cli-neutral" in remaining                          # neutral dir untouched
    assert "s_00" in remaining                                 # active kept despite being oldest
    assert {"s_07", "s_06", "s_05"} <= remaining               # 3 most-recent non-active kept
    assert not ({"s_01", "s_02", "s_03", "s_04"} & remaining)  # older non-active swept
    assert out["removed"] == 4


def test_cli_timeouts_persist_and_flow_onto_the_session(tmp_path):
    svc = GangOf8Service(data_dir=tmp_path)
    svc.update_settings({"cli_timeouts": {"claude": 480, "gemini": 200}})
    assert svc.settings.cli_timeouts == {"claude": 480, "gemini": 200}
    session = svc._open("build a thing", "test", None)
    assert session.cli_timeouts == {"claude": 480, "gemini": 200}


def test_update_settings_persists_and_rederives(tmp_path):
    svc = GangOf8Service(data_dir=tmp_path)
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
    svc = GangOf8Service(data_dir=tmp_path)
    svc.update_settings({"role_agents": {"researcher": "gemini", "critic": "codex"}})
    assert svc.settings.role_agents == {"researcher": "gemini", "critic": "codex"}
    svc.update_settings({"role_agents": {"researcher": "claude"}})  # replaces, not merges
    assert svc.settings.role_agents == {"researcher": "claude"}


# ---- Endpoints ---------------------------------------------------------------


@pytest.fixture()
def client(tmp_path):
    from gangof8 import main as main_mod

    main_mod.service = GangOf8Service(data_dir=tmp_path)
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
    svc = GangOf8Service(data_dir=tmp_path)
    assert svc.api_key_status("gemini")["present"] is False
    status = svc.set_api_key("gemini", "AIza-test-key-1234")
    assert status["present"] is True and status["source"] == "stored"
    assert "AIza-test-key-1234" not in json.dumps(status), "never returns the full key"
    assert svc.secrets.get("gemini") == "AIza-test-key-1234"
    assert svc.clear_api_key("gemini")["present"] is False


def test_google_api_key_env_also_counts(tmp_path, monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setenv("GOOGLE_API_KEY", "g-env-key")
    svc = GangOf8Service(data_dir=tmp_path)
    st = svc.api_key_status("gemini")
    assert st["present"] is True and st["source"] == "env"


def test_unknown_api_key_name_rejected(tmp_path):
    svc = GangOf8Service(data_dir=tmp_path)
    with pytest.raises(KeyError):
        svc.api_key_status("stripe")


def test_gemini_key_endpoints(tmp_path, monkeypatch):
    from gangof8 import main as main_mod

    _no_gemini_env(monkeypatch)
    main_mod.service = GangOf8Service(data_dir=tmp_path)
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
    svc = GangOf8Service(data_dir=tmp_path)
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
    svc = GangOf8Service(data_dir=tmp_path)
    svc.update_settings({"backend": "cli",
                         "role_models": {"code_generator": "claude-opus-4-8",
                                         "researcher": "gemini-2.5-pro"}})
    svc.update_settings({"role_agents": {"code_generator": "codex"}})
    assert "code_generator" not in svc.settings.role_models, "stale pin dropped"
    assert svc.settings.role_models == {"researcher": "gemini-2.5-pro"}
    assert svc.registry._adapters["codex"].role_models == {}
    # persisted too — a restart must not resurrect the stale pin
    assert GangOf8Service(data_dir=tmp_path) \
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
    svc = GangOf8Service(data_dir=fresh)
    svc.set_api_key("gemini", "AIza-new-install-1")
    svc.set_api_key("openrouter", "sk-or-new-install-2")
    stored = json.loads((fresh / "secrets.json").read_text(encoding="utf-8"))
    assert stored == {"gemini": "AIza-new-install-1",
                      "openrouter": "sk-or-new-install-2"}
    # a restart (new service over the same data dir) still resolves them
    svc2 = GangOf8Service(data_dir=fresh)
    gem = svc2.api_key_status("gemini")
    assert gem["present"] is True and gem["source"] == "stored"
    assert svc2.api_key_status("openrouter")["source"] == "stored"
    assert svc2.reveal_api_key("openrouter")["value"] == "sk-or-new-install-2"


def test_reveal_returns_full_key_only_on_explicit_request(tmp_path, monkeypatch):
    """Status stays masked; the dashboard's eye-reveal fetches the full value
    on demand via the explicit /reveal endpoint (localhost-only app, and the
    key already lives in plaintext in data/secrets.json)."""
    from gangof8 import main as main_mod

    _no_gemini_env(monkeypatch)
    main_mod.service = GangOf8Service(data_dir=tmp_path)
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


# ---- roster model labels: one seat, two roles, two models --------------------


def test_resolved_model_distinguishes_two_roles_on_one_seat(tmp_path):
    """The dashboard-mislabel fix: a seat filling two roles runs two models — the
    claude LEAD on its sonnet role pin, the claude PANELIST on the opus seat pin.
    resolved_model reports each role's real model, not one per-seat guess (which
    showed whichever call reported last, mislabelling the other)."""
    svc = GangOf8Service(data_dir=tmp_path)
    svc.update_settings({
        "backend": "cli",
        "role_agents": {"lead": "claude", "panelist": "codex", "architect": "claude"},
        "cli_models": {"claude": "claude-opus-4.8", "codex": "gpt-5.5"},
        "role_models": {"lead": "claude-sonnet-5", "architect": "claude-opus-4.8"},
    })
    assert svc.resolved_model("lead", "claude") == "claude-sonnet-5"       # role pin wins
    assert svc.resolved_model("panelist", "claude") == "claude-opus-4.8"   # seat pin (no claude panelist pin)
    assert svc.resolved_model("architect", "claude") == "claude-opus-4.8"  # role pin
    assert svc.resolved_model("panelist", "codex") == "gpt-5.5"            # seat pin


def test_annotate_council_models_labels_each_member(tmp_path):
    """The serialized roster gets a per-member `model` so the lead chip shows
    sonnet and the same-seat panelist chip shows opus — instead of both inheriting
    whichever claude call reported last."""
    svc = GangOf8Service(data_dir=tmp_path)
    svc.update_settings({
        "backend": "cli",
        "role_agents": {"lead": "claude", "panelist": "codex"},
        "cli_models": {"claude": "claude-opus-4.8"},
        "role_models": {"lead": "claude-sonnet-5"},
    })
    data = {"council": {"members": [
        {"role": "lead", "agent": "claude", "active": True},
        {"role": "panelist", "agent": "claude", "active": True},
    ]}}
    svc.annotate_council_models(data)
    models = {m["role"]: m["model"] for m in data["council"]["members"]}
    assert models["lead"] == "claude-sonnet-5"      # the sonnet lead is labelled sonnet
    assert models["panelist"] == "claude-opus-4.8"  # the opus panelist is labelled opus
