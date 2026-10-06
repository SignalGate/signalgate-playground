"""Runs a visitor plan against the sample app.

One ``httpx.AsyncClient`` drives the whole plan with bounded concurrency: a
semaphore gates the HTTP calls only, never the wait for a visitor's success
delay, so many visitors can be "waiting to confirm" at once without pinning
down a connection. The run reports progress as it goes and can be told to
stop early: visitors already under way finish, no new ones start.

A visitor leased from SignalGate carries its envelopes, and they go where a
frontend would put them: ``signalgate`` (the check envelope) and
``signalgate_log`` on the action, ``signalgate`` (the success envelope) on
the confirm. An offline visitor (chapter 1) has none.
"""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

import httpx

from playground.traffic.local_constants import (
    LOOPBACK_REQUEST_TIMEOUT_S,
    OFFLINE_CONCURRENCY,
    PROGRESS_EVERY_VISITS,
)
from playground.traffic.plan import Visit

ACTION_PATH = "/api/action"
CONFIRM_PATH = "/api/confirm"
INBOX_PATH = "/__playground/inbox"

# Optional per-visit header naming which run a request belongs to; the
# app-side accessor for the same header lives in ``app/kit/context.py``.
RUN_HEADER = "X-Playground-Run"

# Where each envelope role goes in the request bodies.
CHECK_FIELD = "signalgate"
LOG_FIELD = "signalgate_log"

STATUS_SUCCESS = "success"
STATUS_NO_SUCCESS = "no_success"
STATUS_ACTION_FAILED = "action_failed"
STATUS_CONFIRM_FAILED = "confirm_failed"
STATUS_ERROR = "error"


@dataclass(frozen=True)
class Progress:
    """A run snapshot, shaped like ``playground.machine.events.RunProgress``."""

    done: int
    total: int
    actions: int
    successes: int


@dataclass(frozen=True)
class RunResult:
    """The final tally of an offline run, complete or stopped early."""

    total: int
    done: int
    actions: int
    successes: int
    attack: int
    real: int
    stopped: bool
    statuses: dict[str, str] = field(default_factory=dict)
    # The action's HTTP status per visitor ref (absent when it never answered).
    codes: dict[str, int] = field(default_factory=dict)


OnProgress = Callable[[Progress], Awaitable[None] | None]


class _Tally:
    """Mutable counters shared by every visit coroutine in one run."""

    def __init__(self, total: int) -> None:
        self.total = total
        self.started = 0
        self.done = 0
        self.actions = 0
        self.successes = 0
        self.statuses: dict[str, str] = {}
        self.codes: dict[str, int] = {}


async def _call_progress(on_progress: OnProgress, progress: Progress) -> None:
    result = on_progress(progress)
    if inspect.isawaitable(result):
        await result


def _visit_headers(visit: Visit, run_id: str | None, *, with_xff: bool) -> dict[str, str]:
    """Headers common to a visit's action and confirm calls."""
    headers = {"X-Playground-Ref": visit.ref}
    if with_xff:
        headers["X-Forwarded-For"] = visit.client_ip
    if run_id is not None:
        headers[RUN_HEADER] = run_id
    return headers


async def _run_one(
    visit: Visit,
    *,
    client: httpx.AsyncClient,
    control_token: str,
    semaphore: asyncio.Semaphore,
    stop: asyncio.Event,
    tally: _Tally,
    on_progress: OnProgress,
    run_id: str | None,
) -> None:
    started = False
    status = STATUS_ERROR
    try:
        async with semaphore:
            # Checked only once we hold a slot: a visit still queued behind
            # the concurrency limit when ``stop`` fires never starts, but one
            # that already grabbed a slot always finishes its own lifecycle.
            if stop.is_set():
                return
            started = True
            tally.started += 1
            envelopes = visit.envelopes or {}
            action_body: dict[str, object] = {"user": visit.user_id}
            if envelopes.get("action_check") is not None:
                action_body[CHECK_FIELD] = envelopes["action_check"]
            if envelopes.get("action_log") is not None:
                action_body[LOG_FIELD] = envelopes["action_log"]
            action_response = await client.post(
                ACTION_PATH,
                headers=_visit_headers(visit, run_id, with_xff=True),
                json=action_body,
            )
        tally.codes[visit.ref] = action_response.status_code
        action_ok = action_response.status_code == 200
        if action_ok:
            tally.actions += 1

        if not action_ok:
            status = STATUS_ACTION_FAILED
        elif not visit.success:
            status = STATUS_NO_SUCCESS
        else:
            await asyncio.sleep(visit.success_after_ms / 1000)
            confirmed = False
            async with semaphore:
                inbox_headers = {"Authorization": f"Bearer {control_token}"}
                if run_id is not None:
                    inbox_headers[RUN_HEADER] = run_id
                # Never X-Forwarded-For here: control routes refuse it.
                inbox_response = await client.get(
                    INBOX_PATH,
                    params={"ref": visit.ref},
                    headers=inbox_headers,
                )
                token = None
                if inbox_response.status_code == 200:
                    token = inbox_response.json().get("tokens", {}).get(visit.ref)
                if token:
                    confirm_body: dict[str, object] = {"user": visit.user_id, "token": token}
                    if envelopes.get("success_log") is not None:
                        confirm_body[CHECK_FIELD] = envelopes["success_log"]
                    confirm_response = await client.post(
                        CONFIRM_PATH,
                        headers=_visit_headers(visit, run_id, with_xff=True),
                        json=confirm_body,
                    )
                    confirmed = confirm_response.status_code == 200
            if confirmed:
                tally.successes += 1
                status = STATUS_SUCCESS
            else:
                status = STATUS_CONFIRM_FAILED
    except httpx.HTTPError:
        status = STATUS_ERROR
    finally:
        if started:
            tally.statuses[visit.ref] = status
            tally.done += 1
            if tally.done % PROGRESS_EVERY_VISITS == 0:
                await _call_progress(
                    on_progress,
                    Progress(tally.done, tally.total, tally.actions, tally.successes),
                )


async def run_offline(
    plan: list[Visit],
    base_url: str,
    control_token: str,
    *,
    on_progress: OnProgress,
    stop: asyncio.Event,
    run_id: str | None = None,
) -> RunResult:
    """Run ``plan`` against the app at ``base_url``; honour ``stop`` early.

    ``run_id``, when given, is sent as ``X-Playground-Run`` on every request
    this run makes; omitted entirely when ``run_id`` is None.
    """
    total = len(plan)
    attack = sum(1 for visit in plan if visit.cls == "attack")
    real = total - attack

    tally = _Tally(total)
    semaphore = asyncio.Semaphore(OFFLINE_CONCURRENCY)
    limits = httpx.Limits(
        max_connections=OFFLINE_CONCURRENCY,
        max_keepalive_connections=OFFLINE_CONCURRENCY,
    )

    async with httpx.AsyncClient(
        base_url=base_url,
        trust_env=False,
        limits=limits,
        timeout=LOOPBACK_REQUEST_TIMEOUT_S,
    ) as client:
        tasks = [
            asyncio.ensure_future(
                _run_one(
                    visit,
                    client=client,
                    control_token=control_token,
                    semaphore=semaphore,
                    stop=stop,
                    tally=tally,
                    on_progress=on_progress,
                    run_id=run_id,
                )
            )
            for visit in plan
        ]
        if tasks:
            await asyncio.gather(*tasks)

        await _call_progress(
            on_progress,
            Progress(tally.done, tally.total, tally.actions, tally.successes),
        )

    return RunResult(
        total=total,
        done=tally.done,
        actions=tally.actions,
        successes=tally.successes,
        attack=attack,
        real=real,
        stopped=stop.is_set() and tally.done < total,
        statuses=tally.statuses,
        codes=tally.codes,
    )


__all__ = ["CHECK_FIELD", "LOG_FIELD", "Progress", "RunResult", "run_offline"]
