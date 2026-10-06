"""Shared fixtures for the verifier's tests.

Every test starts and ends with no shared client and no records. The
sample app's key is a dummy value in a temp key file, never a developer's
own; the control token is set for the test; and the SDK's HTTP layer
forwards to a fresh fake server (``fake``) in process.
"""

from __future__ import annotations

import pytest

from app import signalgate_client
from app.kit import inbox, lifecycle, self_test, settings
from app.kit.calls import calls
from app.kit.canary import canaries
from app.kit.sequence import ledger
from tests.fake_server import FakeServer, Script
from tests.support.sdk_stub import forward_to, install_sdk_stub
from tests.verify.harness import CONTROL_TOKEN, DUMMY_KEY

KEY_VARIABLE = "SIGNALGATE_API_KEY"


@pytest.fixture(autouse=True)
def _fresh_app_state(monkeypatch: pytest.MonkeyPatch, tmp_path_factory: pytest.TempPathFactory):
    monkeypatch.delenv(KEY_VARIABLE, raising=False)
    monkeypatch.setenv("PLAYGROUND_CONTROL_TOKEN", CONTROL_TOKEN)
    key_file = tmp_path_factory.mktemp("key-file") / ".env"
    key_file.write_text(f"{KEY_VARIABLE}={DUMMY_KEY}\n")
    monkeypatch.setattr(settings, "ENV_PATH", key_file)
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


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeServer:
    """A fresh fake server that the sample app's SDK calls reach, workflow active and protecting."""
    script = Script()
    script.go_to("active", protection_stage=1)
    server = FakeServer(script)
    install_sdk_stub(monkeypatch, forward_to(server.app))
    return server
