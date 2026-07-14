"""Throughput fixes that preserve the full configured council.

Latency comes from parallel agent calls, early-stop judge waves, and compact
round-zero context — never by silently convening fewer of the requested seats.
"""

import pytest

from gangof8 import loop
from gangof8.logstore import LogStore
from gangof8.models import (Classification, Complexity, Risk, TaskType)
from gangof8.sessions import SessionManager


@pytest.fixture()
def store(tmp_path) -> LogStore:
    return LogStore(tmp_path)


def _session(store, panel, complexity=Complexity.standard, risk=Risk.none):
    s = SessionManager(store).create("build game.html in full", source="test")
    s.classification = Classification(task_type=TaskType.code,
                                      complexity=complexity, risk=risk,
                                      produces_output=True)
    s.panel = list(panel)
    return s


# --- full roster: configured seats are never tiered or auto-benched ----------

SEVEN = ["claude", "codex", "gemini", "deepseek", "glm", "qwen", "kimi"]


@pytest.mark.parametrize("complexity,risk", [
    (Complexity.trivial, Risk.none),
    (Complexity.standard, Risk.none),
    (Complexity.complex, Risk.none),
    (Complexity.standard, Risk.high),
])
def test_every_task_convenes_the_complete_configured_roster(store, complexity, risk):
    s = _session(store, SEVEN, complexity=Complexity.standard)
    s.classification.complexity = complexity
    s.classification.risk = risk
    assert loop._select_panel_seats(s, store) == SEVEN


def test_persisted_health_history_never_removes_a_configured_seat(store):
    (store.data_dir / "seat_bench.json").write_text(
        '{"codex": {"drops": 99, "sat_out": 0}}', encoding="utf-8")
    s = _session(store, ["claude", "codex", "gemini"], complexity=Complexity.complex)
    assert loop._select_panel_seats(s, store) == ["claude", "codex", "gemini"]


# --- per-kind concurrency: HTTP seats never queue behind CLI subprocesses -------


def test_adapters_declare_their_process_kind():
    from gangof8.adapters.cli import CliAdapter
    from gangof8.adapters.mock import MockAdapter
    from gangof8.adapters.openrouter import OpenRouterAdapter

    assert CliAdapter.local_process is True
    assert OpenRouterAdapter.local_process is False
    assert MockAdapter.local_process is False


def test_semaphore_choice_by_adapter_kind(store):
    from gangof8.adapters.mock import MockAdapter
    from gangof8.registry import AgentRegistry

    reg = AgentRegistry()
    reg.register(MockAdapter())
    assert loop._agent_semaphore(reg, "mock") is loop._API_SEMAPHORE
    # an unregistered/unknown agent counts as local — the conservative side
    assert loop._agent_semaphore(reg, "mystery") is loop._CLI_SEMAPHORE


# --- API surface: the contract reaches round 0 whole ----------------------------

SHELL = "\n".join(
    ["<!doctype html><html><head><style>", "body { background: #000; }"]
    + [f".pad-{i} {{ margin: {i}px; }}" for i in range(200)]  # pushes JS past any head cap
    + ["</style></head><body><script>",
       "class Game {",
       "  constructor(ctx) { this.ctx = ctx; }",
       "}",
       "function boot(canvas) { return new Game(canvas); }",
       "var portal = {};",
       "portal.register = function (id, name, GameClass) {};",
       "SoundFX.prototype.shoot = function () {};",
       "ARCADE.register('invaders', 'SPACE INVADERS', SpaceInvaders);",
       "const tick = (dt) => dt;",
       "</script></body></html>"])


def test_api_surface_extracts_the_contract():
    surface = loop._api_surface(SHELL)
    assert "class Game" in surface
    assert "function boot(canvas)" in surface
    assert "SoundFX.prototype.shoot" in surface
    assert "ARCADE.register('invaders'" in surface
    assert ".pad-7" not in surface, "styling noise is not a declaration"


def test_api_surface_skips_trivially_small_files():
    assert loop._api_surface("function only() {}") == ""


def test_truncated_task_named_source_carries_its_api_surface(store, tmp_path):
    """The live failure: the overview truncated shell.html right at the engine
    namespace, so every seat's first act was a SKILL chain re-reading it. A
    truncated task-named source now carries its full declaration list."""
    (tmp_path / "shell.html").write_text(SHELL, encoding="utf-8")
    s = SessionManager(store).create(
        "extend the game menu in shell.html with a frogger entry", source="test")
    s.classification = Classification(task_type=TaskType.code,
                                      complexity=Complexity.standard, risk=Risk.none,
                                      produces_output=True)
    s.established_root = str(tmp_path)

    import gangof8.loop as l
    orig = l._SOURCE_DIGEST_MAX_CHARS
    l._SOURCE_DIGEST_MAX_CHARS = 500  # force truncation without a 40KB fixture
    try:
        overview = loop._established_overview(s, store.data_dir)
    finally:
        l._SOURCE_DIGEST_MAX_CHARS = orig
    assert "API SURFACE" in overview
    assert "ARCADE.register('invaders'" in overview, \
        "the registration contract survives the head cap"
