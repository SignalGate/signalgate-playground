"""Tests for the client-construction hook: counting, recording, and the
read-only probes the boot self-test is built on."""

from __future__ import annotations

import threading

import pytest
import signalgate
from fastapi.testclient import TestClient

from app import signalgate_client
from app.kit import facade, settings
from app.kit.calls import calls
from tests.app.fixtures.facade.apps import build_app
from tests.app.fixtures.facade.fake_transport import FakeTransport
from tests.app.fixtures.facade.records import (
    assert_every_record_matches_its_shape,
    read_all_calls,
)
from tests.contract import samples

_DIRECT_KEY = "dummy-direct-key-0123456789"


@pytest.fixture(autouse=True)
def _patch_kit_transport(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(facade, "KitTransport", FakeTransport)


@pytest.fixture(autouse=True)
def _every_record_matches_its_shape():
    """After every test here, every ``construct`` (and ``call``) record the
    hook and the facade emitted must match its contract shape."""
    yield
    assert_every_record_matches_its_shape()


def _set_key(value: str = "dummy-test-key-0123456789") -> None:
    settings.ENV_PATH.write_text(f"SIGNALGATE_API_KEY={value}\n", encoding="utf-8")
    signalgate_client._reset()


# --- install_client_hook: installed once, idempotent ---------------------------


def test_the_hook_is_already_installed_by_module_import():
    assert facade.client_hook_installed() is True


def test_a_second_install_keeps_the_same_wrapper():
    before = signalgate.Client.__init__
    facade.install_client_hook()
    facade.install_client_hook()
    assert signalgate.Client.__init__ is before


def test_a_fresh_install_wraps_the_true_original_and_counts_the_next_construction(
    monkeypatch: pytest.MonkeyPatch,
):
    # `__wrapped__` (functools.wraps) is the true, unwrapped `Client.__init__`
    # this process started with.
    original = signalgate.Client.__init__.__wrapped__
    monkeypatch.setattr(signalgate.Client, "__init__", original)
    assert facade.client_hook_installed() is False

    facade.install_client_hook()
    assert facade.client_hook_installed() is True

    before = facade.clients_created()
    client = signalgate.Client("some-key-0123456789")
    try:
        assert facade.clients_created() == before + 1
    finally:
        client.close()


# --- the facade probe -----------------------------------------------------------


def test_missing_members_is_empty_against_the_pinned_sdk():
    assert facade.missing_members() == ()


# --- is_recording_client ---------------------------------------------------------


def test_is_recording_client_tells_sg_apart_from_a_bare_client():
    _set_key()
    assert facade.is_recording_client(signalgate_client.sg()) is True

    bare = signalgate.Client("another-key-0123456789")
    try:
        assert facade.is_recording_client(bare) is False
    finally:
        bare.close()

    assert facade.is_recording_client(object()) is False
    assert facade.is_recording_client(None) is False


# --- counting and recording: the singleton vs a direct construction ------------


def test_a_direct_construction_counts_and_records_without_the_singleton():
    app = build_app()
    client = TestClient(app)
    before = facade.clients_created()

    for _ in range(3):
        response = client.post("/direct-client")
        assert response.status_code == 200

    assert facade.clients_created() == before + 3

    records = read_all_calls()
    constructs = [record for record in records if record["kind"] == "construct"]
    assert len(constructs) == 3
    for record in constructs:
        samples.assert_same_shape(record, samples.CONSTRUCT_RECORD)
        assert record["singleton"] is False
        assert record["kit_transport"] is False


def test_the_singleton_alone_gives_one_construct_record():
    _set_key()
    first = signalgate_client.get_client()
    second = signalgate_client.get_client()
    assert first is second

    records = read_all_calls()
    constructs = [record for record in records if record["kind"] == "construct"]
    assert len(constructs) == 1
    assert constructs[0]["singleton"] is True
    assert constructs[0]["kit_transport"] is True
    samples.assert_same_shape(constructs[0], samples.CONSTRUCT_RECORD)


def test_50_concurrent_first_calls_build_exactly_one_client():
    _set_key()
    start = threading.Barrier(50)
    results: list[object] = []
    lock = threading.Lock()

    def call() -> None:
        start.wait()
        client = signalgate_client.get_client()
        with lock:
            results.append(client)

    threads = [threading.Thread(target=call) for _ in range(50)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(results) == 50
    assert len({id(r) for r in results}) == 1
    assert facade.clients_created() == 1
    assert signalgate_client.sg() is results[0]


# --- the autouse shape check itself ---------------------------------------------


def test_the_shape_check_fails_a_construct_record_off_its_spec():
    # Proves the autouse check above is not vacuous: the real construct record
    # passes, and the same record with a field missing is caught.
    _set_key()
    signalgate_client.get_client()
    assert assert_every_record_matches_its_shape() == 1

    record = next(r for r in read_all_calls() if r["kind"] == "construct")
    del record["kit_transport"]
    calls.append(record)
    with pytest.raises(AssertionError):
        assert_every_record_matches_its_shape()
    calls.reset()  # leave nothing off-spec behind for the autouse check
