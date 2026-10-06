"""The data gate, judged from recorded evidence, and once from a real run through the app.

The recorded cases follow the SDK's own counting: a single 4xx bumps both
an HTTP error counter and a retry-exhausted drop, so drops are read only
for a full queue or a closed client, and 4xx answers only from attempts.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
import pytest

from playground.copy.en import TEXT
from playground.supervisor.control_client import (
    KEY_DROPPED_CLOSED,
    KEY_DROPPED_QUEUE_FULL,
    KEY_DROPPED_RETRY,
    ControlClient,
)
from playground.supervisor.drain import drain
from playground.traffic.driver import RUN_HEADER
from playground.traffic.local_constants import APP_HEALTH_POLL_S
from playground.verify import (
    Verdict,
    collect,
    data_gate,
    delivery,
    gate_lines,
    mark,
    posted_counts,
    wait_for_stored,
)
from playground.verify import findings as F
from playground.verify.evidence import new_run_id
from tests.contract.playground_api import SESSIONS_PATH
from tests.verify.harness import FIXTURES, load_router, open_session, serve
from tests.verify.recordings import (
    accepted_logs,
    check_attempt,
    check_call,
    evidence,
    log_attempt,
    log_call,
    timed_out,
)

ENOUGH = {"action": 100, "success": 30}


def _run(actions: int = 100, successes: int = 30) -> list[dict[str, Any]]:
    return accepted_logs(actions) + accepted_logs(
        successes, method="playground.success", start=actions + 1
    )


def _verdicts(result) -> dict[str, Verdict]:
    return {check.name: check.verdict for check in result.checks}


def test_a_clean_run_passes_and_counts_per_method():
    gate = data_gate(evidence(_run()), ENOUGH)
    assert gate.result.verdict is Verdict.PASS
    assert gate.posted == {"action": 100, "success": 30}
    assert gate.accepted == {"action": 100, "success": 30}
    assert gate.degraded is False


@pytest.mark.parametrize(
    ("key", "variant"), [(KEY_DROPPED_QUEUE_FULL, "queue_full"), (KEY_DROPPED_CLOSED, "closed")]
)
def test_a_full_queue_or_a_closed_client_fails_the_run(key, variant):
    gate = data_gate(evidence(_run(), metrics={key: 3}), ENOUGH)
    assert gate.result.verdict is Verdict.FAIL
    (found,) = gate.result.findings
    assert (found.id, found.variant, found.params["count"]) == (F.LOG_DROPPED, variant, 3)


def test_retry_exhausted_drops_alone_are_not_read_as_drops():
    # A 4xx also counts as retry-exhausted; the attempts decide, not this counter.
    gate = data_gate(evidence(_run(), metrics={KEY_DROPPED_RETRY: 2}), ENOUGH)
    assert _verdicts(gate.result)["drops"] is Verdict.PASS


def test_a_422_after_a_timed_out_attempt_is_a_probable_duplicate_and_forgiven():
    records = _run(actions=99)
    records += [
        log_call(9990, "ref-dup", 999),
        timed_out(999),
        log_attempt(999, attempt=2, status=422),
    ]
    gate = data_gate(evidence(records), ENOUGH)
    assert gate.result.verdict is Verdict.PASS
    assert gate.duplicates["action"] == 1
    assert [f.id for f in gate.result.findings] == [F.DUPLICATE_DELIVERY]
    assert gate.result.findings[0].blocks is False


def test_any_other_4xx_on_a_log_fails_the_run():
    records = _run(actions=99) + [log_call(9990, "ref-bad", 999), log_attempt(999, status=422)]
    gate = data_gate(evidence(records), ENOUGH)
    assert gate.result.verdict is Verdict.FAIL
    found = [f for f in gate.result.findings if f.id == F.LOG_REJECTED]
    assert [(f.variant, f.params["count"], f.params["code"]) for f in found] == [("run", 1, 422)]


def test_delivery_below_the_share_fails_per_method_and_at_the_share_passes():
    unreached = [log_call(9000 + n, f"ref-lost-{n}", 900 + n) for n in range(2)]
    at_share = data_gate(evidence(_run(actions=98) + unreached), {"action": 100, "success": 30})
    assert _verdicts(at_share.result)["delivered"] is Verdict.PASS
    one_more = unreached + [log_call(9100, "ref-lost-x", 910)]
    below = data_gate(evidence(_run(actions=98) + one_more), {"action": 101, "success": 30})
    assert _verdicts(below.result)["delivered"] is Verdict.FAIL
    (found,) = [f for f in below.result.findings if f.id == F.LOGS_NOT_DELIVERED]
    assert found.params == {"method": "playground.action", "accepted": 98, "posted": 101}


def test_stored_counts_still_short_are_not_run_and_keep_their_finding_id():
    # SignalGate accepted the events; storing them is on its side, not the user's code.
    short = data_gate(evidence(_run()), {"action": 97, "success": 30})
    assert _verdicts(short.result)["stored"] is Verdict.NOT_RUN
    assert short.result.verdict is Verdict.NOT_RUN
    assert short.result.finding_ids == (F.EVENTS_NOT_STORED,)
    (found,) = short.result.findings
    assert found.params == {"method": "playground.action", "stored": 97, "posted": 100}
    lines = gate_lines(short.result)
    assert lines[0] == "◌ " + TEXT["verify.not_run.stored"]
    assert "Press Enter to try again." in lines[0]
    assert not any(line.startswith("✗") for line in lines), lines
    assert short.result.counts()["failed"] == 0


def test_unknown_stored_counts_are_not_run():
    unknown = data_gate(evidence(_run()), None)
    assert unknown.result.verdict is Verdict.NOT_RUN
    assert _verdicts(unknown.result)["stored"] is Verdict.NOT_RUN
    assert unknown.result.findings == ()


def test_a_run_with_no_logs_is_not_run():
    gate = data_gate(evidence([]), {"action": 0, "success": 0})
    assert _verdicts(gate.result)["delivered"] is Verdict.NOT_RUN


def test_too_many_fail_opens_label_the_run_degraded_but_still_pass():
    checks = [check_call(5000 + n, f"ref-c{n}") for n in range(100)]
    fine = checks[:99] + [check_call(5999, "ref-open", outcome="failed_open")]
    assert data_gate(evidence(_run() + fine), ENOUGH).degraded is False
    two_open = fine[:98] + [
        check_call(5998, "ref-open-2", outcome="failed_open"),
        check_call(5999, "ref-open", outcome="failed_open"),
    ]
    gate = data_gate(evidence(_run() + two_open), ENOUGH)
    assert gate.degraded is True
    assert gate.result.verdict is Verdict.PASS


def test_the_transports_verdicts_join_checks_without_changing_the_counts():
    records = _run() + [check_call(7000, "ref-v"), check_attempt(7000)]
    assert data_gate(evidence(records), ENOUGH).posted == posted_counts(evidence(_run()))


def test_wait_for_stored_returns_as_soon_as_every_count_is_enough(monkeypatch):
    monkeypatch.setattr(delivery, "STORED_POLL_S", APP_HEALTH_POLL_S)
    answers = iter([None, {"action": 50, "success": 30}, {"action": 99, "success": 30}])

    async def fetch():
        return next(answers)

    got = asyncio.run(
        wait_for_stored(fetch, {"action": 100, "success": 30}, APP_HEALTH_POLL_S * 20)
    )
    assert got == {"action": 99, "success": 30}


def test_wait_for_stored_gives_up_with_the_last_counts_it_saw(monkeypatch):
    monkeypatch.setattr(delivery, "STORED_POLL_S", APP_HEALTH_POLL_S)
    calls = 0

    async def fetch():
        nonlocal calls
        calls += 1
        if calls == 2:
            raise httpx.ConnectError("gone")
        return {"action": 10, "success": 0}

    got = asyncio.run(wait_for_stored(fetch, {"action": 100, "success": 0}, APP_HEALTH_POLL_S * 3))
    assert got == {"action": 10, "success": 0}
    assert calls >= 2


# --- one real run through the app --------------------------------------------------------


async def _learning_run(link, visits, run: str) -> None:
    async with httpx.AsyncClient(base_url=link.base_url, trust_env=False) as client:
        control = {"Authorization": f"Bearer {link.token}"}
        for visit in visits:
            headers = {
                "X-Playground-Ref": visit["ref"],
                "X-Forwarded-For": visit["client_ip"],
                RUN_HEADER: run,
            }
            envelopes = visit["envelopes"]
            body = {"user": visit["user_id"], "signalgate_log": envelopes["action_log"]}
            response = await client.post("/api/action", json=body, headers=headers)
            assert response.status_code == 200
            if not visit["script"]["success"]:
                continue
            inbox = await client.get(
                "/__playground/inbox", params={"ref": visit["ref"]}, headers=control
            )
            token = inbox.json()["tokens"][visit["ref"]]
            body = {
                "user": visit["user_id"],
                "token": token,
                "signalgate": envelopes["success_log"],
            }
            response = await client.post("/api/confirm", json=body, headers=headers)
            assert response.status_code == 200


def test_a_learning_run_through_the_app_passes_the_data_gate(fake):
    api, sid = open_session(fake)
    since = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
    response = api.post(
        f"{SESSIONS_PATH}/{sid}/leases", json={"phase": "learn", "count": 40, "seq": 1}
    )
    visits = response.json()["visits"]

    async def fetch_stored():
        status = api.get(f"{SESSIONS_PATH}/{sid}", params={"since": since}).json()
        return status["stored_since"]

    async def scenario(link):
        control = ControlClient(link.base_url, link.token)
        try:
            start = await mark(control)
            run = new_run_id("learn")
            await _learning_run(link, visits, run)
            await drain(control)
            ev = await collect(control, start, run=run)
        finally:
            await control.aclose()
        stored = await wait_for_stored(
            fetch_stored, posted_counts(ev), fake.script.limits["ingest_wait_s"]
        )
        return ev, stored

    with serve(load_router(FIXTURES / "right" / "ch3_plain.py")) as link:
        ev, stored = asyncio.run(scenario(link))
    gate = data_gate(ev, stored)
    successes = sum(v["script"]["success"] for v in visits)
    assert gate.posted == {"action": len(visits), "success": successes}
    assert gate.accepted == gate.posted
    assert stored == {"action": len(visits), "success": successes}
    assert gate.result.verdict is Verdict.PASS
