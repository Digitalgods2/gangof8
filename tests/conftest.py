"""Shared test fixtures.

The per-session sandbox now lives under a NEUTRAL location (config.SANDBOX_ROOT),
never under a data/project dir. Point it at a fresh temp dir per test so tests
stay isolated and never share scratch state.
"""

import pytest

from conclave_os import config


@pytest.fixture(autouse=True)
def _isolated_sandbox(tmp_path_factory, monkeypatch):
    sandbox = tmp_path_factory.mktemp("sandbox")
    monkeypatch.setattr(config, "SANDBOX_ROOT", sandbox)
    return sandbox
