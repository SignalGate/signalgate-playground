"""Workflow gates G1-G4 from status answers, readiness and the tripwire from recorded evidence.

Statuses are built with the fake server's own workflow helpers, so they
have the contract's shape. Readiness rounds are recorded by hand, then one
is driven for real through the sample app against the fake.
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest

from playground.supervisor.control_client import ControlClient
from playground.supervisor.drain import drain
from playground.traffic.driver import RUN_HEADER
from playground.verify import (
    Verdict,
    collect,
    gate_g1,
    gate_g2,
    gate_g3,
    gate_g4,
    mark,
    readiness,
    readiness_next,
    tripwire,
)
from playground.verify import workflow as W
from playground.verify.evidence import new_run_id
from tests.contract.playground_api import STATUS
from tests.contract.samples import assert_same_shape
from tests.fake_server.script import PREDICTION, name_only, no_workflow, playground_flow
from tests.verify.harness import FIXTURES, lease, load_router, serve
from tests.verify.recordings import action_entry, check_attempt, check_call, evidence


def _status(workflow: dict[str, Any], received: int = 0) -> dict[str, Any]:
    status = {
        "workflow": workflow,
        "window": {"action_received": received, "success_received": 0},
        "stored_since": None,
    }
    assert_same_shape(status, STATUS)
    return status


def test_g1_needs_a_thousand_actions_and_says_how_many_are_missing():
    assert gate_g1(_status(no_workflow(), 1000)).verdict is Verdict.PASS
    short = gate_g1(_status(no_workflow(), 940))
    assert (short.verdict, short.reason, short.shortfall) == (Verdict.FAIL, "too_few", 60)
    assert gate_g1(None).verdict is Verdict.NOT_RUN


@pytest.mark.parametrize(
    ("workflow", "reason"),
    [
        (no_workflow(), "no_workflow"),
        (name_only("ready"), "not_playground"),
        (playground_flow("ready", prediction=PREDICTION, method_ok=False), "method_mismatch"),
        (playground_flow("draft"), "draft"),
        (playground_flow("queued"), "dry_run_pending"),
        (playground_flow("dry_running", stage=2), "dry_run_pending"),
        (playground_flow("ready"), "no_prediction"),
    ],
)
def test_g2_waits_for_the_tutorials_own_dry_run(workflow, reason):
    gate = gate_g2(_status(workflow))
    assert (gate.verdict, gate.reason) == (Verdict.FAIL, reason)


def test_g2_passes_with_the_prediction_to_freeze_and_names_the_wrong_method():
    gate = gate_g2(_status(playground_flow("ready", prediction=PREDICTION)))
    assert (gate.verdict, gate.prediction) == (Verdict.PASS, PREDICTION)
    wrong = gate_g2(_status(playground_flow("ready", target_ok=False)))
    assert (wrong.method_ok, wrong.target_ok) == (True, False)
    assert gate_g2({"window": {}}).verdict is Verdict.NOT_RUN


@pytest.mark.parametrize(
    ("workflow", "reason"),
    [
        (playground_flow("ready", prediction=PREDICTION), "not_active"),
        (playground_flow("active", action="dry_run_block"), "not_block"),
        (playground_flow("paused"), "paused"),
        (name_only("active"), "not_playground"),
    ],
)
def test_g3_needs_an_active_block_workflow(workflow, reason):
    gate = gate_g3(_status(workflow))
    assert (gate.verdict, gate.reason) == (Verdict.FAIL, reason)


def test_g3_and_g4_pass_once_active_and_protecting():
    active = playground_flow("active", prediction=PREDICTION)
    assert gate_g3(_status(active)).verdict is Verdict.PASS
    assert gate_g4(_status(active)).reason == "not_protecting"
    live = playground_flow("active", prediction=PREDICTION, protecting=True, protection_stage=1)
    assert gate_g4(_status(live)).verdict is Verdict.PASS
    assert gate_g4(_status(name_only("active"))).reason == "not_playground"
    assert gate_g4(_status(playground_flow("paused"))).reason == "paused"


# --- readiness from recorded rounds ---------------------------------------------------------


def _round(real_blocked: int, attack_blocked: int, *, real: int = 22, attack: int = 58):
    visits = [{"ref": f"real-{n}", "class": "real"} for n in range(real)]
    visits += [{"ref": f"attack-{n}", "class": "attack"} for n in range(attack)]
    records: list[dict[str, Any]] = []
    blocked = {f"real-{n}" for n in range(real_blocked)} | {
        f"attack-{n}" for n in range(attack_blocked)
    }
    for seq, visit in enumerate(visits, start=1):
        verdict = "block" if visit["ref"] in blocked else "allow"
        records.append(check_call(seq, visit["ref"], action=verdict))
        records.append(check_attempt(seq, verdict=verdict))
    return visits, evidence(records)


@pytest.mark.parametrize(
    ("real_blocked", "attack_blocked", "verdict"),
    [
        (0, 58, W.LIVE),
        (1, 35, W.LIVE),
        (0, 34, W.WEAK),
        (2, 58, W.OVER_BLOCKING),
        (2, 0, W.OVER_BLOCKING),
        (0, 2, W.ARMING),
        (0, 0, W.ARMING),
        (1, 3, W.WEAK),
    ],
)
def test_readiness_verdicts_follow_their_rules_in_order(real_blocked, attack_blocked, verdict):
    visits, ev = _round(real_blocked, attack_blocked)
    ready = readiness(visits, ev)
    assert ready.verdict == verdict
    assert (ready.real_blocked, ready.real_total) == (real_blocked, 22)
    assert (ready.attack_blocked, ready.attack_total) == (attack_blocked, 58)


def test_readiness_reads_the_verdict_the_transport_recorded():
    visits = [{"ref": "attack-0", "class": "attack"}]
    # The call record says allow; SignalGate's recorded reply says block.
    ev = evidence([check_call(1, "attack-0", action="allow"), check_attempt(1, verdict="block")])
    assert readiness(visits, ev).attack_blocked == 1


def test_a_round_with_no_check_verdict_has_no_readiness_verdict():
    visits, _ = _round(0, 0)
    assert readiness(visits, evidence([])).verdict is None


@pytest.mark.parametrize(
    ("verdicts", "step"),
    [
        ([W.OVER_BLOCKING], W.STOP),
        ([W.LIVE], W.CONTINUE),
        ([W.ARMING], W.RETRY),
        ([W.ARMING, W.ARMING], W.DIAGNOSE),
        ([W.WEAK], W.RETRY),
        ([W.WEAK, W.WEAK], W.CONTINUE_WEAK),
        ([W.ARMING, W.WEAK], W.CONTINUE_WEAK),
        ([None], W.RETRY),
    ],
)
def test_the_step_after_each_readiness_round(verdicts, step):
    assert readiness_next(verdicts, max_attempts=2) == step


def test_the_tripwire_names_a_blocked_visit_whose_action_still_happened():
    ev = evidence(
        [
            check_call(1, "blocked-and-done", action="block"),
            check_attempt(1, verdict="block"),
            check_call(3, "blocked-and-stopped", action="block"),
            check_attempt(3, verdict="block"),
        ],
        ledger=[action_entry(2, "blocked-and-done"), action_entry(4, "allowed")],
    )
    assert tripwire(ev) == ["blocked-and-done"]
    assert tripwire(evidence([])) == []


# --- one readiness round through the app ----------------------------------------------------


async def _drive(link, visits, run: str) -> None:
    async with httpx.AsyncClient(base_url=link.base_url, trust_env=False) as client:
        for visit in visits:
            headers = {
                "X-Playground-Ref": visit["ref"],
                "X-Forwarded-For": visit["client_ip"],
                RUN_HEADER: run,
            }
            envelopes = visit["envelopes"]
            body = {
                "user": visit["user_id"],
                "signalgate": envelopes["action_check"],
                "signalgate_log": envelopes["action_log"],
            }
            await client.post("/api/action", json=body, headers=headers)


def _readiness_round(fixture: str, fake):
    fake.script.limits["lease_max"] = 80
    visits = lease(fake, "readiness", 80)

    async def scenario(link):
        control = ControlClient(link.base_url, link.token)
        try:
            start = await mark(control)
            run = new_run_id("readiness")
            await _drive(link, visits, run)
            await drain(control)
            return await collect(control, start, run=run)
        finally:
            await control.aclose()

    with serve(load_router(FIXTURES / fixture)) as link:
        ev = asyncio.run(scenario(link))
    return visits, ev


def test_a_readiness_round_through_the_gated_app_is_live(fake):
    visits, ev = _readiness_round("right/ch5_plain.py", fake)
    ready = readiness(visits, ev)
    assert (ready.real_total, ready.attack_total) == (22, 58)
    assert ready.verdict == W.LIVE
    assert tripwire(ev) == []


def test_the_tripwire_catches_a_gate_that_ignores_block(fake):
    visits, ev = _readiness_round("wrong/verdict_ignored.py", fake)
    attack = {v["ref"] for v in visits if v["class"] == "attack"}
    assert set(tripwire(ev)) == attack
