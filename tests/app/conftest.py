"""Shared fixtures for the sample app's tests."""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient

from app import signalgate_client
from app.kit import inbox, lifecycle, self_test, settings
from app.kit.calls import calls
from app.kit.canary import canaries
from app.kit.sequence import ledger
from app.main import app

CONTROL_TOKEN = "test-control-token"
KEY_VARIABLE = "SIGNALGATE_API_KEY"


@pytest.fixture(autouse=True)
def _reset_kit_state(monkeypatch: pytest.MonkeyPatch, tmp_path_factory: pytest.TempPathFactory):
    """Start and end every test with no API key, no shared client and no records.

    The key sources are process-wide and read into a snapshot, so before
    each test ``SIGNALGATE_API_KEY`` is removed from the environment,
    ``settings.ENV_PATH`` is pointed at a file that doesn't exist yet (its
    directory does), and ``_reset()`` re-reads both. A developer's own key,
    in ``app/.env`` or in their shell, never reaches a test. A test that
    needs a key writes ``settings.ENV_PATH`` or sets the variable, then
    calls ``signalgate_client._reset()`` itself.

    Also resets the self-test's cache and the exit-request once-guard,
    process-wide state the control routes read, so one test monkeypatching a
    probe or calling ``/shutdown`` never changes what the next test sees.

    After each test, still with both sources empty, ``_reset()`` runs again
    and the call records, the canaries, the ledger and the inbox are
    cleared, so no test's leftovers leak into the next.

    The key-loading and facade tests rely on this: keep it when adding fixtures.
    """
    monkeypatch.delenv(KEY_VARIABLE, raising=False)
    monkeypatch.setattr(settings, "ENV_PATH", tmp_path_factory.mktemp("key-file") / ".env")
    signalgate_client._reset()
    self_test._reset()
    lifecycle._reset()
    yield
    signalgate_client._reset()
    calls.reset()
    canaries.clear()
    ledger.reset()
    inbox.clear()
    self_test._reset()
    lifecycle._reset()


@pytest.fixture
def control_token(monkeypatch: pytest.MonkeyPatch) -> str:
    """Set PLAYGROUND_CONTROL_TOKEN for the duration of a test."""
    monkeypatch.setenv("PLAYGROUND_CONTROL_TOKEN", CONTROL_TOKEN)
    return CONTROL_TOKEN


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


@pytest.fixture
def unique_ref() -> str:
    """A fresh ref, so tests never collide over shared, in-memory state."""
    return f"ref-{uuid.uuid4().hex}"


@pytest.fixture
def unique_user() -> str:
    """A fresh, valid user id, so tests never collide over shared state."""
    return f"user-{uuid.uuid4().hex[:16]}"
