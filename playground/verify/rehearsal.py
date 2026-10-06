"""The rehearsal gate: your check() gate against answers made on the laptop.

Every envelope here is a dummy made on the laptop (random bytes in the
public shape) and registered with the sample app as a canary, so the kit
answers it locally and nothing is ever sent to SignalGate. One action per
answer:

- ``block``: 403, the action doesn't happen, and nothing is logged;
- ``dry_run_block`` and ``admin_alert``: the request passes through, the
  action happens and its log follows;
- a 422 rejection: the app still answers (no 5xx);
- probe 8: while one check() answer is held back on the laptop, the app's
  health route keeps answering promptly (a blocking check() inside an
  ``async def`` would stall every other request).

The static checks on ``app/routes.py`` run too. A rehearsal needs no real
event: its answers are the public verdict strings only.
"""

from __future__ import annotations

import asyncio
import random
import time
from pathlib import Path
from typing import Any

import httpx

from playground.supervisor.control_client import ControlClient, ControlError
from playground.supervisor.drain import drain
from playground.traffic.local_constants import (
    APP_HEALTH_POLL_S,
    PROBE_CANARY_DELAY_MS,
    PROBE_HEALTH_BUDGET_S,
)
from playground.traffic.sampler import Sampler
from playground.verify import findings as F
from playground.verify.evidence import (
    AppLink,
    Evidence,
    EvidenceUnavailable,
    Sources,
    collect,
    connect,
    mark,
    new_run_id,
)
from playground.verify.model import Check, GateResult, not_run
from playground.verify.probes import (
    ACTION_METHOD,
    CHECK_FIELD,
    LOG_FIELD,
    PASS_THROUGH,
    Step,
    dummy_envelope,
    judge_check_field,
    not_ready,
    raised_site,
    routes_digest,
    send,
    step,
)
from playground.verify.static import SourceUnreadable, call_line, check_source

# The canary answers each rehearsal action gets, in order.
ANSWERS = ("block", "dry_run_block", "admin_alert", "422")
# Probe 8 runs once more when it finds the app slow, so one slow moment on
# a busy laptop never fails correct code.
PROBE_8_TRIES = 2


async def _canary_action(
    control: ControlClient,
    sampler: Sampler,
    run: str,
    name: str,
    answer: str,
    *,
    delay_ms: int = 0,
) -> tuple[Step, dict[str, Any]]:
    user, ip = sampler.sample()
    check, log = dummy_envelope(), dummy_envelope()
    items: list[dict[str, Any]] = [
        {"nonce": check["nonce"], "answer": answer, "delay_ms": delay_ms},
        {"nonce": log["nonce"], "answer": PASS_THROUGH},
    ]
    await control.canary(items)
    request = step(name, "action", run, user, ip, signalgate=check, signalgate_log=log)
    return request, {"user": user, CHECK_FIELD: check, LOG_FIELD: log}


async def _probe_8(
    visitor: httpx.AsyncClient, control: ControlClient, sampler: Sampler, run: str
) -> tuple[Step, float]:
    """Send an action whose check() answer is held back, and time health meanwhile.

    Returns the last attempt and the slowest health answer seen during it.
    """
    attempt = 0
    while True:
        request, body = await _canary_action(
            control,
            sampler,
            run,
            f"p8_{attempt}",
            PASS_THROUGH,
            delay_ms=PROBE_CANARY_DELAY_MS,
        )
        pending = asyncio.ensure_future(send(visitor, request, body, run))
        slowest = 0.0
        while not pending.done():
            started = time.perf_counter()
            await control.health()
            slowest = max(slowest, time.perf_counter() - started)
            # Any health call sent while the app is stalled waits the stall out,
            # so a short pause between calls never hides one.
            await asyncio.wait({pending}, timeout=APP_HEALTH_POLL_S)
        await pending
        attempt += 1
        if slowest < PROBE_HEALTH_BUDGET_S or attempt >= PROBE_8_TRIES:
            return request, slowest


def _on_block(request: Step, ev: Evidence, sources: Sources, perform: Any) -> Check:
    name = "rehearsal_block"
    if request.status is None:
        return Check.skipped(name, "app")
    wrong = judge_check_field(request, ev, sources)
    if wrong:
        return Check.judged(name, wrong)
    checks = ev.calls("check", ref=request.ref)
    actions = ev.entries("action", ref=request.ref)
    params = {"user": request.user, "status": request.status}
    if actions:
        if not checks:
            return Check.judged(name, [F.finding(F.CHECK_MISSING, perform, **params)])
        site = sources.of(checks[0])
        if checks[0].get("seq", 0) > actions[0].get("seq", 0):
            return Check.judged(name, [F.finding(F.CHECK_AFTER_ACTION, site, **params)])
        return Check.judged(name, [F.finding(F.VERDICT_IGNORED, site, **params)])
    logs = ev.calls("log", ref=request.ref)
    if logs:
        return Check.judged(
            name, [F.finding(F.BLOCKED_ACTION_LOGGED, sources.of(logs[0]), **params)]
        )
    if request.status != 403:
        site = sources.of(checks[0]) if checks else None
        return Check.judged(name, [F.finding(F.NO_403_ON_BLOCK, site, **params)])
    return Check.judged(name, [])


def _passes_through(request: Step, answer: str, ev: Evidence, sources: Sources) -> Check:
    name = f"rehearsal_{answer}"
    if request.status is None:
        return Check.skipped(name, "app")
    wrong = judge_check_field(request, ev, sources)
    if wrong:
        return Check.judged(name, wrong)
    checks = ev.calls("check", ref=request.ref)
    actions = ev.entries("action", ref=request.ref)
    params = {"user": request.user, "status": request.status, "verdict": answer}
    if not actions:
        site = sources.of(checks[0]) if checks else None
        return Check.judged(name, [F.finding(F.GATES_ON_WRONG_VERDICT, site, **params)])
    logs = [
        c
        for c in ev.calls("log", ref=request.ref)
        if c.get("method") == ACTION_METHOD and c.get("seq", 0) > actions[0].get("seq", 0)
    ]
    if not logs:
        return Check.judged(name, [F.finding(F.LOG_MISSING, None, "rehearsal", **params)])
    return Check.judged(name, [])


def _on_rejection(request: Step, ev: Evidence, sources: Sources) -> Check:
    name = "rehearsal_422"
    if request.status is None:
        return Check.skipped(name, "app")
    if request.status >= 500:
        site = raised_site(ev, request.ref, sources)
        params = {"user": request.user, "status": request.status}
        return Check.judged(name, [F.finding(F.CHECK_ERROR_BREAKS_REQUEST, site, **params)])
    return Check.judged(name, [])


def _probe_8_check(request: Step, slowest: float, ev: Evidence, sources: Sources) -> Check:
    name = "probe_8"
    if request.status is None:
        return Check.skipped(name, "app")
    checks = ev.calls("check", ref=request.ref)
    if slowest >= PROBE_HEALTH_BUDGET_S:
        site = sources.of(checks[0]) if checks else None
        return Check.judged(name, [F.finding(F.CHECK_BLOCKS_EVENT_LOOP, site, "probe")])
    if not checks:
        return Check.skipped(name, "depends")
    return Check.judged(name, [])


async def run_rehearsal(link: AppLink, *, routes: Path, root: Path) -> GateResult:
    """Run the rehearsal gate against the app at ``link`` (chapter 5).

    ``routes`` is the user's ``app/routes.py`` and ``root`` the repo root.
    Never raises for a problem with the app: that makes the gate NOT RUN.
    """
    sources = Sources(root)
    try:
        digest = routes_digest(routes)
        static = check_source(routes, chapter=5, sources=sources)
    except (OSError, SourceUnreadable):
        return not_run("rehearsal", "routes")
    line = call_line(routes, "perform_action")
    perform = sources.site(sources.relative(routes), line) if line else None
    # Visitors' ids and addresses, from the same sampler every visitor uses.
    sampler = Sampler(random.Random())
    try:
        async with connect(link) as (visitor, control):
            health = await control.health()
            reason = not_ready(health)
            if reason is not None:
                return not_run("rehearsal", reason)
            start = await mark(control, health)
            run = new_run_id("rehearsal")
            sent: dict[str, Step] = {}
            for answer in ANSWERS:
                request, body = await _canary_action(control, sampler, run, answer, answer)
                sent[answer] = await send(visitor, request, body, run)
            p8, slowest = await _probe_8(visitor, control, sampler, run)
            await drain(control)
            ev = await collect(control, start, run=run)
    except EvidenceUnavailable as unavailable:
        return not_run("rehearsal", unavailable.reason)
    except (httpx.HTTPError, ControlError):
        return not_run("rehearsal", "app")
    checks: list[Check] = [
        Check.judged("static", static),
        _on_block(sent["block"], ev, sources, perform),
        _passes_through(sent["dry_run_block"], "dry_run_block", ev, sources),
        _passes_through(sent["admin_alert"], "admin_alert", ev, sources),
        _on_rejection(sent["422"], ev, sources),
        _probe_8_check(p8, slowest, ev, sources),
    ]
    return GateResult("rehearsal", tuple(checks), routes_sha256=digest)


__all__ = ["ANSWERS", "run_rehearsal"]
