"""Reading a window of evidence back through the control routes, and joining it.

The control routes are answered by an ``httpx.MockTransport`` here, so each
edge (paging, a restart, dropped records) can be set up exactly.
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest

from playground.supervisor.control_client import ControlClient
from playground.verify.evidence import (
    ACCEPTED,
    DUPLICATE,
    LOCAL,
    REJECTED,
    UNREACHED,
    EvidenceUnavailable,
    collect,
    job_outcome,
    mark,
)
from tests.verify.recordings import (
    check_attempt,
    check_call,
    evidence,
    log_attempt,
    log_call,
    timed_out,
)

BOOT = "boot-1"


@pytest.mark.parametrize(
    ("attempts", "outcome"),
    [
        ([], UNREACHED),
        ([log_attempt(1)], ACCEPTED),
        ([timed_out(1), log_attempt(1, attempt=2)], ACCEPTED),
        ([timed_out(1), log_attempt(1, attempt=2, status=422)], DUPLICATE),
        ([log_attempt(1, status=422)], REJECTED),
        ([log_attempt(1, status=503)], UNREACHED),
        ([timed_out(1)], UNREACHED),
        ([log_attempt(1, local="canary")], LOCAL),
        ([log_attempt(1, status=400, local="guard")], LOCAL),
    ],
)
def test_how_one_job_ended(attempts, outcome):
    assert job_outcome(attempts) == outcome


def test_calls_join_their_own_attempts():
    ev = evidence(
        [
            check_call(1, "ref-a"),
            check_attempt(1, verdict="block"),
            log_call(2, "ref-a", 7),
            timed_out(7),
            log_attempt(7, attempt=2),
            log_attempt(8),
        ]
    )
    check, log = ev.calls("check")[0], ev.calls("log")[0]
    assert ev.verdict(check) == "block"
    assert [a["attempt"] for a in ev.attempts(log)] == [1, 2]
    assert ev.outcome(log) == ACCEPTED
    assert ev.calls(ref="ref-b") == []


def _control(pages: list[dict[str, Any]], *, boots: list[str], ledger: list[dict[str, Any]]):
    """A control client whose /calls answers ``pages`` in turn and /health each of ``boots``."""
    seen: dict[str, Any] = {"ledger_params": []}
    health_answers = iter(boots)
    page_answers = iter(pages)

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/health"):
            body = {
                "boot_id": next(health_answers),
                "sdk": "signalgate",
                "sdk_version": "0.3.2",
                "methods": ["playground.action", "playground.success"],
                "clients_created": 1,
                "client_ready": True,
                "self_test": {"ok": True, "failed": [], "checks": {}},
                "key": "set",
            }
            return httpx.Response(200, json=body)
        if path.endswith("/calls"):
            return httpx.Response(200, json=next(page_answers))
        if path.endswith("/metrics"):
            return httpx.Response(200, json={"log_sent_total": 5})
        if path.endswith("/ledger"):
            seen["ledger_params"].append(dict(request.url.params))
            return httpx.Response(
                200, json={"entries": ledger, "cursor": len(ledger), "more": False}
            )
        return httpx.Response(404)

    client = ControlClient("http://127.0.0.1:1", "token", transport=httpx.MockTransport(handler))
    return client, seen


def _page(records, cursor, *, more=False, truncated=False, boot=BOOT) -> dict[str, Any]:
    return {
        "v": 1,
        "boot_id": boot,
        "records": records,
        "cursor": cursor,
        "more": more,
        "oldest_cursor": 0,
        "truncated": truncated,
        "record_errors": 0,
    }


def test_a_window_reads_every_page_since_its_mark():
    first, second = log_call(1, "r", 1), log_attempt(1)
    pages = [
        _page([check_call(9, "old")], 1),  # mark: the end of what was there
        _page([first], 2, more=True),
        _page([second], 3),
    ]
    control, seen = _control(pages, boots=[BOOT, BOOT], ledger=[])

    async def scenario():
        start = await mark(control)
        return start, await collect(control, start, run="run-1")

    start, ev = asyncio.run(scenario())
    assert start.cursor == 1
    assert ev.records == (first, second)
    assert ev.metrics == {"log_sent_total": 0}
    assert seen["ledger_params"] == [{"run": "run-1", "cursor": "0"}]


@pytest.mark.parametrize(
    ("pages", "boots", "reason"),
    [
        ([_page([], 0), _page([], 0)], [BOOT, "boot-2"], "restarted"),
        ([_page([], 0), _page([], 0, boot="boot-2")], [BOOT, BOOT], "restarted"),
        ([_page([], 0), _page([], 0, truncated=True)], [BOOT, BOOT], "records"),
    ],
)
def test_a_window_that_cant_be_read_whole_says_why(pages, boots, reason):
    control, _ = _control(pages, boots=boots, ledger=[])

    async def scenario():
        start = await mark(control)
        await collect(control, start, run="run-1")

    with pytest.raises(EvidenceUnavailable) as raised:
        asyncio.run(scenario())
    assert raised.value.reason == reason
