"""Tests for the drain gate against a scripted control client."""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import json
import socket
import time
from collections.abc import Callable

import httpx
import pytest

import playground.supervisor.drain as drain_module
from playground.supervisor.control_client import METRICS_PATH, ControlClient
from playground.supervisor.drain import drain
from tests.supervisor._clock import RecordingClock


def _client(handler) -> ControlClient:
    transport = httpx.MockTransport(handler)
    return ControlClient("http://127.0.0.1:1", "tok", transport=transport)


def _sequence(*snapshots):
    remaining = list(snapshots)

    def handler(request: httpx.Request) -> httpx.Response:
        body = remaining.pop(0) if len(remaining) > 1 else remaining[0]
        return httpx.Response(200, json=body, request=request)

    return handler


@pytest.fixture(autouse=True)
def _small_constants(monkeypatch):
    """Patch every drain constant small, so these tests run fast and
    deterministically -- never a raw literal at a call site below."""
    monkeypatch.setattr(drain_module, "DRAIN_POLL_S", 0.01)
    monkeypatch.setattr(drain_module, "DRAIN_REPORT_EVERY_S", 0.03)
    monkeypatch.setattr(drain_module, "DRAIN_STALL_S", 0.05)
    monkeypatch.setattr(drain_module, "DRAIN_REQUEST_TIMEOUT_S", 0.05)
    monkeypatch.setattr(drain_module, "DRAIN_FAILURES_MAX", 3)
    # Generous next to a run of failed polls (each up to one request timeout)
    # either side of a reset, so a capacity test never mistakes those for the
    # cap kicking in early.
    monkeypatch.setattr(
        drain_module,
        "DRAIN_MAX_S",
        drain_module.DRAIN_REQUEST_TIMEOUT_S * drain_module.DRAIN_FAILURES_MAX * 10,
    )


def _outer_bound_s() -> float:
    """A generous ceiling for one drain() call in these tests, derived from
    the (already patched, tiny) constants -- so a genuine hang fails the
    test promptly instead of stalling the suite."""
    return drain_module.DRAIN_MAX_S * 43


def _http_ok(body: dict) -> bytes:
    payload = json.dumps(body).encode("utf-8")
    header = (
        "HTTP/1.1 200 OK\r\n"
        "Content-Type: application/json\r\n"
        f"Content-Length: {len(payload)}\r\n"
        "Connection: close\r\n\r\n"
    ).encode("ascii")
    return header + payload


async def _serve_raw(on_request: Callable[[int], bytes | None]):
    """A minimal raw HTTP/1.1 server for exactly one route.

    ``on_request(count)`` is called once per connection with a 1-based
    count; returning ``None`` means this request never gets an answer (so
    httpx's own read timeout -- which a mocked transport can't exercise at
    all -- has something real to fire against), and returning bytes sends
    them back verbatim.

    Returns ``(base_url, aclose)``: a still-hanging connection would
    otherwise make a plain ``server.wait_closed()`` hang too, so ``aclose``
    releases every such handler first.
    """
    count = 0
    released = asyncio.Event()

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        nonlocal count
        count += 1
        this_count = count
        buffer = b""
        while b"\r\n\r\n" not in buffer:
            chunk = await reader.read(4096)
            if not chunk:
                break
            buffer += chunk

        response = on_request(this_count)
        if response is None:
            await released.wait()  # hang until the test tears the server down
        else:
            writer.write(response)
            await writer.drain()
        writer.close()
        with contextlib.suppress(OSError):
            await writer.wait_closed()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]

    async def aclose() -> None:
        released.set()
        server.close()
        await server.wait_closed()

    return f"http://127.0.0.1:{port}", aclose


def _counting(body: dict, paths: list[str]):
    """Answer every request with ``body``, recording each request's path."""

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        return httpx.Response(200, json=body, request=request)

    return handler


def test_empty_snapshot_drains_on_the_first_poll():
    paths: list[str] = []
    client = _client(_counting({}, paths))
    result = asyncio.run(drain(client))
    assert result.outcome == "drained"
    assert result.queued == 0
    assert paths == [METRICS_PATH]  # exactly one poll, no second look


def test_absent_keys_count_as_zero_so_an_unused_client_drains_at_once():
    # No enqueued/sent/dropped keys at all: still reads as queued == 0.
    paths: list[str] = []
    client = _client(_counting({"some_other_key": 1}, paths))
    result = asyncio.run(drain(client))
    assert result.outcome == "drained"
    assert result.queued == 0
    assert paths == [METRICS_PATH]


def test_frozen_counters_become_stalled():
    snapshot = {"log_enqueued_total": 5, "log_sent_total": 1}
    client = _client(_sequence(snapshot))
    result = asyncio.run(drain(client))
    assert result.outcome == "stalled"
    assert result.queued == 4


def test_counters_that_keep_retrying_get_capped():
    counters = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        counters["n"] += 1
        # Enqueued grows with every retry, always outpacing sent, so queued
        # never reaches zero; the error count keeps moving too, so this is
        # never mistaken for a stall either -- only the cap can end it.
        body = {
            "log_enqueued_total": 100 + counters["n"],
            "log_sent_total": 1,
            'log_http_error_total{status="422"}': counters["n"],
        }
        return httpx.Response(200, json=body, request=request)

    client = _client(handler)
    result = asyncio.run(drain(client))
    assert result.outcome == "capped"
    assert result.queued > 0


def test_cancel_gives_skipped_promptly():
    asyncio.run(_cancel_gives_skipped_promptly())


async def _cancel_gives_skipped_promptly() -> None:
    client = _client(_sequence({"log_enqueued_total": 5}))
    cancel = asyncio.Event()
    cancel.set()
    result = await asyncio.wait_for(drain(client, cancel=cancel), timeout=_outer_bound_s())
    assert result.outcome == "skipped"


def test_cancel_during_a_poll_is_still_prompt():
    asyncio.run(_cancel_during_a_poll_is_still_prompt())


async def _cancel_during_a_poll_is_still_prompt() -> None:
    hung = asyncio.Event()

    async def slow_handler(request: httpx.Request) -> httpx.Response:
        await hung.wait()
        return httpx.Response(200, json={}, request=request)  # pragma: no cover

    client = ControlClient("http://127.0.0.1:1", "tok", transport=httpx.MockTransport(slow_handler))
    cancel = asyncio.Event()

    async def cancel_soon() -> None:
        await asyncio.sleep(drain_module.DRAIN_POLL_S)
        cancel.set()

    cancel_task = asyncio.ensure_future(cancel_soon())
    started = time.monotonic()
    result = await asyncio.wait_for(drain(client, cancel=cancel), timeout=_outer_bound_s())
    elapsed = time.monotonic() - started
    assert result.outcome == "skipped"
    # Prompt: nowhere near the (huge) per-poll request timeout the stuck
    # handler would otherwise need to hit.
    assert elapsed < drain_module.DRAIN_REQUEST_TIMEOUT_S * 1000
    hung.set()
    await cancel_task


def test_a_dead_app_is_unreachable_at_once():
    asyncio.run(_a_dead_app_is_unreachable_at_once())


async def _a_dead_app_is_unreachable_at_once() -> None:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        dead_port = sock.getsockname()[1]
    # The socket above is closed again on exit, so nothing listens on this
    # port: any connection attempt is refused immediately by the OS.
    client = ControlClient(f"http://127.0.0.1:{dead_port}", "tok")
    started = time.monotonic()
    result = await asyncio.wait_for(drain(client), timeout=_outer_bound_s())
    elapsed = time.monotonic() - started
    assert result.outcome == "unreachable"
    # "At once": far less than one failed poll's own request timeout, let
    # alone DRAIN_FAILURES_MAX of them.
    assert elapsed < drain_module.DRAIN_REQUEST_TIMEOUT_S


def test_a_hanging_metrics_route_gives_unreachable_after_the_failure_cap():
    asyncio.run(_a_hanging_metrics_route_gives_unreachable_after_the_failure_cap())


async def _a_hanging_metrics_route_gives_unreachable_after_the_failure_cap() -> None:
    base_url, aclose = await _serve_raw(lambda _count: None)
    try:
        client = ControlClient(base_url, "tok")
        result = await asyncio.wait_for(drain(client), timeout=_outer_bound_s())
        assert result.outcome == "unreachable"
    finally:
        await aclose()


def test_one_success_between_failures_resets_the_count(monkeypatch):
    # The one successful reply must beat the per-request timeout even on a
    # busy machine, so give this test more room than the module's tiny
    # default; the failed polls still end by timing out, as intended.
    roomy_timeout = drain_module.DRAIN_REQUEST_TIMEOUT_S * 6
    monkeypatch.setattr(drain_module, "DRAIN_REQUEST_TIMEOUT_S", roomy_timeout)
    monkeypatch.setattr(
        drain_module, "DRAIN_MAX_S", roomy_timeout * (drain_module.DRAIN_FAILURES_MAX + 3) * 4
    )
    asyncio.run(_one_success_between_failures_resets_the_count())


async def _one_success_between_failures_resets_the_count() -> None:
    # Fail, fail, succeed (resets the count), then fail all the way to the
    # cap: proof the earlier failures were really forgotten, not just that
    # three in a row never happened.
    pre_reset_failures = 2
    success_at = pre_reset_failures + 1
    seen: list[int] = []

    def on_request(count: int) -> bytes | None:
        seen.append(count)
        return _http_ok({"log_enqueued_total": 1}) if count == success_at else None

    base_url, aclose = await _serve_raw(on_request)
    try:
        client = ControlClient(base_url, "tok")
        result = await asyncio.wait_for(drain(client), timeout=_outer_bound_s())
        assert result.outcome == "unreachable"
        assert len(seen) == success_at + drain_module.DRAIN_FAILURES_MAX
    finally:
        await aclose()


def test_on_wait_receives_the_queued_count_and_is_throttled(monkeypatch):
    report_every = drain_module.DRAIN_POLL_S * 13
    monkeypatch.setattr(drain_module, "DRAIN_REPORT_EVERY_S", report_every)
    monkeypatch.setattr(drain_module, "DRAIN_STALL_S", report_every * 3)
    clock = RecordingClock()
    monkeypatch.setattr(drain_module, "time", clock)
    asyncio.run(_on_wait_receives_the_queued_count_and_is_throttled(clock, report_every))


async def _on_wait_receives_the_queued_count_and_is_throttled(
    clock: RecordingClock, report_every: float
) -> None:
    polls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        polls.append(len(polls) + 1)
        return httpx.Response(200, json={"log_enqueued_total": 5}, request=request)

    client = ControlClient("http://127.0.0.1:1", "tok", transport=httpx.MockTransport(handler))
    # (count reported, polls made so far, the drain's own clock at the time)
    reports: list[tuple[int, int, float]] = []

    def on_wait(remaining: int) -> None:
        reports.append((remaining, len(polls), clock.last))

    result = await asyncio.wait_for(drain(client, on_wait=on_wait), timeout=_outer_bound_s())
    assert result.outcome == "stalled"
    assert [count for count, _polls, _at in reports] == [5] * len(reports)
    # The first report comes straight after the first poll that isn't drained.
    assert reports[0][1] == 1
    # Throttled by time, not by poll count: every later report is at least
    # DRAIN_REPORT_EVERY_S after the one before it on the drain's own clock,
    # while the polls in between went unreported.
    assert len(reports) >= 2
    gaps = [later[2] - earlier[2] for earlier, later in itertools.pairwise(reports)]
    assert all(gap >= report_every for gap in gaps)
    assert len(polls) > len(reports)


@pytest.mark.parametrize("status", [401, 404, 500])
def test_a_non_2xx_metrics_answer_is_a_failed_poll_never_drained(status):
    asyncio.run(_a_non_2xx_metrics_answer_is_a_failed_poll_never_drained(status))


async def _a_non_2xx_metrics_answer_is_a_failed_poll_never_drained(status: int) -> None:
    # A JSON object that, read as counters, would say nothing is queued: the
    # status alone must make it a failed poll.
    polls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        polls.append(status)
        return httpx.Response(status, json={"detail": "not here"}, request=request)

    client = ControlClient("http://127.0.0.1:1", "tok", transport=httpx.MockTransport(handler))
    result = await asyncio.wait_for(drain(client), timeout=_outer_bound_s())
    assert result.outcome == "unreachable"
    assert len(polls) == drain_module.DRAIN_FAILURES_MAX


def test_a_non_mapping_snapshot_counts_as_a_failed_poll_not_a_crash():
    asyncio.run(_a_non_mapping_snapshot_counts_as_a_failed_poll_not_a_crash())


async def _a_non_mapping_snapshot_counts_as_a_failed_poll_not_a_crash() -> None:
    # `/metrics` is contractually a flat object, but a caller of `drain()`
    # must never crash on a malformed body -- an oddly-shaped 200 counts
    # exactly like a failed poll, right up to `unreachable` after the cap.
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[1, 2, 3], request=request)

    client = ControlClient("http://127.0.0.1:1", "tok", transport=httpx.MockTransport(handler))
    result = await asyncio.wait_for(drain(client), timeout=_outer_bound_s())
    assert result.outcome == "unreachable"


def test_on_wait_never_sees_a_stale_count_after_a_failed_poll():
    asyncio.run(_on_wait_never_sees_a_stale_count_after_a_failed_poll())


async def _on_wait_never_sees_a_stale_count_after_a_failed_poll() -> None:
    # First poll fails outright; the second succeeds with a real backlog.
    # `on_wait`, whenever it fires, must only ever see that real count --
    # never the 0 a failed first poll would otherwise leave behind.
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.ReadTimeout("slow", request=request)
        return httpx.Response(200, json={"log_enqueued_total": 7}, request=request)

    client = ControlClient("http://127.0.0.1:1", "tok", transport=httpx.MockTransport(handler))
    seen: list[int] = []

    def on_wait(remaining: int) -> None:
        seen.append(remaining)

    result = await asyncio.wait_for(drain(client, on_wait=on_wait), timeout=_outer_bound_s())
    assert result.outcome in ("stalled", "capped")
    assert seen
    assert all(value == 7 for value in seen)
