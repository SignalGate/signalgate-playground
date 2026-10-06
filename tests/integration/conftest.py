"""Shared fixtures for the integration tests.

These tests put the pieces together: the real sample app with the real
facade and kit transport, read back through the console's own control
client, or started and stopped by the console's own launcher.
"""

from __future__ import annotations

import pytest

from app import signalgate_client
from app.kit import inbox, lifecycle, self_test, settings
from app.kit.calls import calls
from app.kit.canary import canaries
from app.kit.sequence import ledger

KEY_VARIABLE = "SIGNALGATE_API_KEY"


@pytest.fixture(autouse=True)
def _fresh_app_state(monkeypatch: pytest.MonkeyPatch, tmp_path_factory: pytest.TempPathFactory):
    """Start and end every test with no key, no shared client and no records.

    The key comes only from a file the test writes (``settings.ENV_PATH`` is
    pointed at a temp file), so a developer's own key never reaches a test.
    The shared client is closed before it is forgotten, so its background
    worker never outlives the test.
    """
    monkeypatch.delenv(KEY_VARIABLE, raising=False)
    monkeypatch.setattr(settings, "ENV_PATH", tmp_path_factory.mktemp("key-file") / ".env")
    signalgate_client._reset()
    self_test._reset()
    lifecycle._reset()
    yield
    signalgate_client.close_client()
    signalgate_client._reset()
    calls.reset()
    canaries.clear()
    ledger.reset()
    inbox.clear()
    self_test._reset()
    lifecycle._reset()
