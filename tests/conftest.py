"""Shared test fixtures.

The per-session sandbox now lives under a NEUTRAL location (config.SANDBOX_ROOT),
never under a data/project dir. Point it at a fresh temp dir per test so tests
stay isolated and never share scratch state.
"""

import pytest

from gangof8 import config


@pytest.fixture(autouse=True)
def _isolated_sandbox(tmp_path_factory, monkeypatch):
    sandbox = tmp_path_factory.mktemp("sandbox")
    monkeypatch.setattr(config, "SANDBOX_ROOT", sandbox)
    # Default web access OFF in tests so the proactive web overview never makes a
    # real network call; tests that exercise web access re-enable it explicitly.
    monkeypatch.setattr(config, "WEB_ENABLED", False)
    return sandbox
