"""Tests for `RecordingClient`: what it records, how transparent it is, and
the facade-level lifecycle (`close_client`, the taught pattern)."""

from __future__ import annotations

import concurrent.futures
import inspect
import json
from pathlib import Path

import pytest
import signalgate
from fastapi.testclient import TestClient

from app import signalgate_client
from app.kit import calls as calls_module
from app.kit import facade, settings
from app.kit.local_constants import CLIENT_CLOSE_TIMEOUT_S
from app.signalgate_client import sg
from tests.app.fixtures.facade.apps import build_app
from tests.app.fixtures.facade.fake_transport import FakeTransport, raising_responder
from tests.app.fixtures.facade.records import (
    assert_every_record_matches_its_shape,
    read_all_calls,
)
from tests.contract import samples

DUMMY_KEY = "dummy-facade-key-0123456789"


@pytest.fixture(autouse=True)
def _patch_kit_transport(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(facade, "KitTransport", FakeTransport)


@pytest.fixture(autouse=True)
def _every_record_matches_its_shape():
    """Every ``call``/``construct`` record this module's tests produce must
    match its contract shape (``tests/contract/samples.py``), not just the
    handful of tests below that call ``assert_same_shape`` directly.

    This runs after every test in this module, and before the shared
    ``tests/app/conftest.py`` fixture clears the call log for the next test.
    """
    yield
    assert_every_record_matches_its_shape()


def _set_key(value: str = "dummy-test-key-0123456789") -> None:
    settings.ENV_PATH.write_text(f"SIGNALGATE_API_KEY={value}\n", encoding="utf-8")
    signalgate_client._reset()


def _build_client(key: str = DUMMY_KEY) -> tuple[facade.RecordingClient, FakeTransport]:
    """A `RecordingClient` wired to its own `FakeTransport`, outside the singleton."""
    transport = FakeTransport(key)
    inner = signalgate.Client(key, http=transport)
    return facade.RecordingClient(inner, kit=transport), transport


def _sample_event(**overrides: object) -> signalgate.Event:
    fields = {
        "user_id": "user-1",
        "ip": "203.0.113.9",
        "method": "playground.action",
        "timestamp": "2026-01-01T00:00:00+00:00",
        "payload": signalgate.EncryptedPayload(
            encrypted="cipher", timestamp=1_700_000_000, nonce="nonce-1"
        ),
    }
    fields.update(overrides)
    return signalgate.Event(**fields)


def _last_call_record(op: str) -> dict:
    matches = [r for r in read_all_calls() if r["kind"] == "call" and r["op"] == op]
    assert matches, f"no {op!r} call record was appended"
    return matches[-1]


# --- check(): fields, timing, outcomes -------------------------------------------


def test_check_record_fields_match_the_event():
    client, _transport = _build_client()
    event = signalgate.Event(
        user_id="user-77",
        ip="198.51.100.9",
        method="playground.action",
        timestamp="2026-02-02T02:02:02+00:00",
        payload=signalgate.EncryptedPayload(encrypted="c", timestamp=5, nonce="nonce-x"),
    )
    result = client.check(event)
    record = _last_call_record("check")

    assert record["method"] == "playground.action"
    assert record["user_id"] == "user-77"
    assert record["ip"] == "198.51.100.9"
    assert record["timestamp"] == "2026-02-02T02:02:02+00:00"
    assert record["nonce"] == "nonce-x"
    assert record["arg_type"] == "Event"
    assert record["payload_type"] == "EncryptedPayload"
    assert record["outcome"] == "verdict"
    assert record["action"] == result.action == "allow"
    assert record["processing_time_us"] == result.processing_time_us == 0
    assert record["log_job"] is None
    assert record["error"] is None
    samples.assert_same_shape(record, samples.CALL_RECORD)
    client.close()


def test_log_record_fields_match_the_event():
    client, _transport = _build_client()
    event = _sample_event()
    client.log(event)
    record = _last_call_record("log")

    assert record["method"] == "playground.action"
    assert record["user_id"] == "user-1"
    assert record["ip"] == "203.0.113.9"
    assert record["nonce"] == "nonce-1"
    assert record["arg_type"] == "Event"
    assert record["payload_type"] == "EncryptedPayload"
    assert record["outcome"] == "enqueued"
    assert record["log_job"] == 1
    assert record["t0"] is None and record["t1"] is None
    assert record["action"] is None and record["processing_time_us"] is None
    samples.assert_same_shape(record, samples.CALL_RECORD)
    client.close()


def test_check_records_a_delay_of_at_least_x():
    # How long the fake transport pretends one request took: a small
    # fraction of the app's own close grace, kept short so the test stays
    # fast, and derived here rather than written as a bare literal.
    fake_network_delay_s = CLIENT_CLOSE_TIMEOUT_S / 90
    client, transport = _build_client()
    transport.delay_s = fake_network_delay_s
    client.check(_sample_event())
    record = _last_call_record("check")
    assert record["t1"] - record["t0"] >= fake_network_delay_s
    client.close()


def test_a_failed_open_check_records_that_outcome():
    def flaky(url, body):
        raise signalgate.NetworkError("simulated network failure")

    client, transport = _build_client()
    transport.respond = flaky
    result = client.check(_sample_event())
    assert result.failed_open is True
    record = _last_call_record("check")
    assert record["outcome"] == "failed_open"
    client.close()


def test_a_4xx_check_raises_and_still_leaves_a_record():
    def rejecting(url, body):
        raise signalgate.ServerError(400, "TEST_CODE", "no", "req-1")

    client, transport = _build_client()
    transport.respond = rejecting
    with pytest.raises(signalgate.ServerError):
        client.check(_sample_event())
    record = _last_call_record("check")
    assert record["outcome"] == "raised"
    assert record["error"] == {"type": "ServerError", "status": 400, "code": "TEST_CODE"}
    samples.assert_same_shape(record, samples.CALL_RECORD)
    client.close()


def test_check_after_close_raises_config_error_and_leaves_a_record():
    client, _transport = _build_client()
    client.close()
    with pytest.raises(signalgate.ConfigError):
        client.check(_sample_event())
    record = _last_call_record("check")
    assert record["outcome"] == "raised"
    assert record["error"] == {"type": "ConfigError", "status": None, "code": None}


class _UnreadableAttributes(Exception):
    """An exception whose ``status_code`` and ``code`` raise when read."""

    @property
    def status_code(self) -> int:
        raise ValueError("unreadable")

    @property
    def code(self) -> str:
        raise ValueError("unreadable")


_UNREADABLE_ERROR_FIELDS = {"type": "_UnreadableAttributes", "status": None, "code": None}


@pytest.mark.parametrize("fail_open", [False, True], ids=["fail_closed", "fail_open"])
def test_check_reraises_the_same_exception_object_even_if_reading_it_raises(fail_open):
    original = _UnreadableAttributes("simulated")
    transport = FakeTransport(DUMMY_KEY)
    transport.respond = raising_responder(original)
    client = facade.RecordingClient(
        signalgate.Client(DUMMY_KEY, http=transport, fail_open=fail_open), kit=transport
    )

    with pytest.raises(_UnreadableAttributes) as excinfo:
        client.check(_sample_event())

    assert excinfo.value is original
    record = _last_call_record("check")
    assert record["outcome"] == "raised"
    assert record["error"] == _UNREADABLE_ERROR_FIELDS
    client.close()


_LIFECYCLE_CALLS = {
    "close": ("close", lambda client: client.close()),
    "enter": ("__enter__", lambda client: client.__enter__()),
    "exit": ("__exit__", lambda client: client.__exit__(None, None, None)),
}


@pytest.mark.parametrize("op", sorted(_LIFECYCLE_CALLS))
def test_lifecycle_calls_reraise_the_same_exception_object_even_if_reading_it_raises(op):
    member, invoke = _LIFECYCLE_CALLS[op]
    original = _UnreadableAttributes("simulated")
    client, _transport = _build_client()

    def raising(*args: object, **kwargs: object) -> None:
        raise original

    setattr(client._inner, member, raising)
    try:
        with pytest.raises(_UnreadableAttributes) as excinfo:
            invoke(client)
    finally:
        delattr(client._inner, member)

    assert excinfo.value is original
    record = _last_call_record(op)
    assert record["outcome"] == "raised"
    assert record["error"] == _UNREADABLE_ERROR_FIELDS
    client.close()


def test_check_record_site_is_repo_relative_with_the_correct_line():
    client, _transport = _build_client()
    event = _sample_event()
    expected_line = inspect.currentframe().f_lineno + 1
    client.check(event)
    record = _last_call_record("check")
    assert record["site"] == {
        "file": "tests/app/test_facade.py",
        "line": expected_line,
        "func": "test_check_record_site_is_repo_relative_with_the_correct_line",
    }
    client.close()


# --- log(): job numbering, not_enqueued, no counter movement --------------------


def test_log_job_numbering_matches_log_enqueued_total():
    client, _transport = _build_client()
    for index in range(5):
        client.log(_sample_event(payload=signalgate.EncryptedPayload("c", 1, f"nonce-{index}")))
    records = [r for r in read_all_calls() if r["kind"] == "call" and r["op"] == "log"]
    assert [r["log_job"] for r in records] == [1, 2, 3, 4, 5]
    assert [r["outcome"] for r in records] == ["enqueued"] * 5
    client.close()


def test_log_after_close_is_not_enqueued_and_counters_stay_unchanged():
    client, _transport = _build_client()
    client.close()
    before = client.metrics.snapshot_flat()
    client.log(_sample_event())
    after = client.metrics.snapshot_flat()
    assert before == after
    record = _last_call_record("log")
    assert record["outcome"] == "not_enqueued"
    assert record["log_job"] is None


def test_a_malformed_event_makes_log_raise_and_still_leaves_a_record():
    client, _transport = _build_client()
    event = _sample_event(payload=None)
    with pytest.raises(AttributeError):
        client.log(event)
    record = _last_call_record("log")
    assert record["outcome"] == "raised"
    assert record["error"]["type"] == "AttributeError"
    assert record["log_job"] is None
    samples.assert_same_shape(record, samples.CALL_RECORD)
    client.close()


# --- seq: ordering against the ledger, uniqueness under concurrency -------------


def test_log_seq_falls_between_two_actions_seqs(unique_user):
    from app.kit.actions import perform_action
    from app.kit.sequence import ledger

    _set_key()
    client = signalgate_client.get_client()

    client.log(_sample_event())
    before_seq = _last_call_record("log")["seq"]

    perform_action(unique_user)
    entries, _ = ledger.read_from(0)
    action_entry = next(e for e in entries if e.get("user") == unique_user)

    client.log(_sample_event())
    after_seq = _last_call_record("log")["seq"]

    assert before_seq < action_entry["seq"] < after_seq


def test_10000_concurrent_checks_get_unique_and_strictly_increasing_seqs():
    client, _transport = _build_client()
    event = _sample_event()
    total = 10_000

    with concurrent.futures.ThreadPoolExecutor(max_workers=32) as pool:
        list(pool.map(lambda _: client.check(event), range(total)))

    # Records are in append (completion) order.
    records = [r for r in read_all_calls() if r["kind"] == "call" and r["op"] == "check"]
    seqs = [r["seq"] for r in records]
    assert len(seqs) == total
    assert len(set(seqs)) == total
    # Not just unique: the whole run's seqs form one contiguous block, so
    # nothing outside this test's own 10k calls could have been interleaved.
    assert sorted(seqs) == list(range(min(seqs), min(seqs) + total))

    # A single worker thread only ever runs one call at a time, so its own
    # calls complete (and so append their record) in the same order their
    # seq was assigned: within one thread, seq must strictly increase.
    by_thread: dict[str, list[int]] = {}
    for record in records:
        by_thread.setdefault(record["thread"], []).append(record["seq"])
    for thread_name, thread_seqs in by_thread.items():
        assert thread_seqs == sorted(thread_seqs), (
            f"{thread_name}'s own seqs are not strictly increasing in call order"
        )
    client.close()


# --- transparency: same values, or the same exception, as a bare Client --------


def _make_event() -> signalgate.Event:
    return _sample_event()


def _make_dict() -> dict:
    return {
        "user_id": "user-1",
        "ip": "203.0.113.9",
        "method": "playground.action",
        "timestamp": "2026-01-01T00:00:00+00:00",
        "payload": {"encrypted": "cipher", "timestamp": 1, "nonce": "nonce-1"},
    }


def _make_event_payload_none() -> signalgate.Event:
    return _sample_event(payload=None)


def _make_event_payload_dict() -> signalgate.Event:
    return _sample_event(payload={"encrypted": "cipher", "timestamp": 1, "nonce": "nonce-1"})


@pytest.mark.parametrize(
    "make_event",
    [_make_event, _make_dict, _make_event_payload_none, _make_event_payload_dict],
    ids=["event", "dict", "payload_none", "payload_dict"],
)
@pytest.mark.parametrize("op", ["check", "log"])
def test_transparency_same_value_or_same_exception(op, make_event):
    wrapped_transport = FakeTransport(DUMMY_KEY)
    wrapped = facade.RecordingClient(
        signalgate.Client(DUMMY_KEY, http=wrapped_transport), kit=wrapped_transport
    )
    bare_transport = FakeTransport(DUMMY_KEY)
    bare = signalgate.Client(DUMMY_KEY, http=bare_transport)

    def invoke(client: object) -> object:
        return getattr(client, op)(make_event())

    wrapped_value = wrapped_error = None
    try:
        wrapped_value = invoke(wrapped)
    except Exception as exc:  # noqa: BLE001 - comparing exception shape below
        wrapped_error = exc

    bare_value = bare_error = None
    try:
        bare_value = invoke(bare)
    except Exception as exc:  # noqa: BLE001 - comparing exception shape below
        bare_error = exc

    if wrapped_error is None and bare_error is None:
        assert wrapped_value == bare_value
    else:
        assert type(wrapped_error) is type(bare_error)
        assert wrapped_error.args == bare_error.args

    wrapped.close()
    bare.close()


# --- lifecycle: close / enter / exit records ------------------------------------


def test_sg_close_produces_a_close_record():
    _set_key()
    sg().close()
    record = _last_call_record("close")
    assert record["outcome"] == "returned"
    samples.assert_same_shape(record, samples.CALL_RECORD)


def test_sg_is_the_same_object_as_get_client():
    _set_key()
    assert sg() is signalgate_client.get_client()


def test_with_sg_produces_enter_then_exit_records():
    _set_key()
    with sg() as ctx:
        assert ctx is sg()
    records = [r for r in read_all_calls() if r["kind"] == "call" and r["op"] in ("enter", "exit")]
    assert [r["op"] for r in records] == ["enter", "exit"]
    for record in records:
        assert record["outcome"] == "returned"
        samples.assert_same_shape(record, samples.CALL_RECORD)


# --- close_client: idempotent, ordered, unrecorded ------------------------------


def test_close_client_closes_inner_before_the_kit_and_records_nothing():
    _set_key()
    client = signalgate_client.get_client()
    order: list[str] = []
    original_inner_close = client._inner.close
    original_kit_close = client._kit.close

    def inner_close(*args: object, **kwargs: object) -> None:
        order.append("inner")
        original_inner_close(*args, **kwargs)

    def kit_close() -> None:
        order.append("kit")
        original_kit_close()

    client._inner.close = inner_close
    client._kit.close = kit_close

    signalgate_client.close_client()
    signalgate_client.close_client()  # idempotent: raises nothing

    assert order[:2] == ["inner", "kit"]
    close_records = [r for r in read_all_calls() if r["kind"] == "call" and r["op"] == "close"]
    assert close_records == []
    assert signalgate_client.client_state()[1] is True  # still "created in this boot"


def test_close_client_with_no_singleton_makes_get_client_raise():
    signalgate_client.close_client()  # never built anything; still harmless
    with pytest.raises(signalgate.ConfigError):
        signalgate_client.get_client()


def test_the_closed_config_error_message_never_contains_the_key():
    _set_key(DUMMY_KEY)
    signalgate_client.close_client()  # never built anything; still marks closed
    with pytest.raises(signalgate.ConfigError) as excinfo:
        signalgate_client.get_client()
    assert DUMMY_KEY not in str(excinfo.value)


# --- get_client() reaches the builder through the module attribute -------------


def test_get_client_calls_facade_build_through_the_module_attribute(
    monkeypatch: pytest.MonkeyPatch,
):
    # `get_client()` must call `facade.build(...)`, never a name bound once
    # at import time, so a test can swap the builder by patching
    # `app.kit.facade.build`.
    seen: list[str] = []

    def fake_build(key: str) -> facade.RecordingClient:
        seen.append(key)
        transport = FakeTransport(key)
        inner = signalgate.Client(key, http=transport)
        return facade.RecordingClient(inner, kit=transport)

    monkeypatch.setattr(facade, "build", fake_build)
    _set_key("dummy-patched-key-0123456789")

    client = signalgate_client.get_client()

    assert seen == ["dummy-patched-key-0123456789"]
    assert client is signalgate_client.sg()


# --- secrets: the key and exception text never leak -----------------------------


def test_repr_and_str_never_contain_the_key():
    client, _transport = _build_client()
    assert DUMMY_KEY not in repr(client)
    assert DUMMY_KEY not in str(client)
    client.close()


def test_records_never_contain_the_dummy_key():
    _set_key(DUMMY_KEY)
    client = signalgate_client.get_client()
    client.log(_sample_event())
    client.check(_sample_event())

    dumped = json.dumps(read_all_calls())
    assert DUMMY_KEY not in dumped


def test_records_never_contain_str_of_a_transport_exception():
    marker = "leak-marker-should-never-appear-0123456789"

    def rejecting(url, body):
        raise signalgate.ServerError(400, "TEST_CODE", f"{marker} {DUMMY_KEY}", "req-marker")

    client, transport = _build_client()
    transport.respond = rejecting
    exc_text = None
    try:
        client.check(_sample_event())
    except signalgate.ServerError as exc:
        exc_text = str(exc)
    # Sanity: the exception this test raises really does carry the marker
    # and the key, so the assertions below prove something.
    assert exc_text is not None
    assert marker in exc_text
    assert DUMMY_KEY in exc_text

    dumped = json.dumps(read_all_calls())
    assert marker not in dumped
    assert DUMMY_KEY not in dumped
    assert exc_text not in dumped
    client.close()


def test_the_two_taught_comments_are_present_verbatim():
    source = Path(inspect.getfile(signalgate_client)).read_text(encoding="utf-8")
    assert 'In your app: Client(api_key=os.environ["SIGNALGATE_API_KEY"]).' in source
    assert "In production set forwarded_allow_ips to your proxy's address." in source


# --- thread naming and on_loop, through real request handling ------------------


def test_a_def_route_logs_on_an_anyio_worker_thread_with_on_loop_false():
    _set_key()
    client = TestClient(build_app())
    response = client.post("/sync-log")
    assert response.status_code == 200
    record = _last_call_record("log")
    assert record["thread"] == "AnyIO worker thread"
    assert record["on_loop"] is False


def test_an_async_def_route_logs_with_on_loop_true():
    _set_key()
    client = TestClient(build_app())
    response = client.post("/async-log")
    assert response.status_code == 200
    record = _last_call_record("log")
    assert record["on_loop"] is True


# --- the taught pattern never raises ---------------------------------------------


@pytest.mark.parametrize("outcome", ["none", "malformed", "valid"])
def test_the_taught_pattern_never_raises(outcome):
    _set_key()
    client = TestClient(build_app())
    response = client.post(f"/taught/{outcome}")
    assert response.status_code == 200
    assert response.json() == {"ok": True}


# --- nothing is ever printed -----------------------------------------------------


def test_nothing_is_printed(capsys):
    client, _transport = _build_client()
    client.check(_sample_event())
    client.log(_sample_event())
    client.close()
    assert capsys.readouterr() == ("", "")


# --- a failure to record never changes behaviour ----------------------------------


def test_construction_check_and_log_are_unchanged_when_recording_fails(monkeypatch):
    def broken_append(*_args, **_kwargs):
        raise RuntimeError("recording is broken in this test")

    monkeypatch.setattr(calls_module.calls, "append", broken_append)
    before = facade.clients_created()
    client, transport = _build_client()
    assert facade.clients_created() == before + 1
    assert client.check(_sample_event()).action == "allow"
    client.log(
        _sample_event(
            payload=signalgate.EncryptedPayload(
                encrypted="cipher", timestamp=1_700_000_000, nonce="nonce-2"
            )
        )
    )
    client.close()
    assert transport.closed is False  # the facade never closes a transport it did not build
