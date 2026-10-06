"""Waits for the sample app's queued ``log()`` events to leave before it stops.

Polls the app's own counters over loopback HTTP -- never SignalGate directly,
never a clock that pretends to know how long a round trip there takes. The
wait is bounded three separate ways: a per-poll request timeout, a stall
detector (progress stopped moving), and a hard cap on the whole wait.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Literal, TypeVar

import httpx

from playground.supervisor.control_client import (
    KEY_DROPPED_RETRY,
    KEY_SENT,
    ControlClient,
    counter,
    http_errors_total,
    queued,
)
from playground.traffic.local_constants import (
    DRAIN_FAILURES_MAX,
    DRAIN_MAX_S,
    DRAIN_POLL_S,
    DRAIN_REPORT_EVERY_S,
    DRAIN_REQUEST_TIMEOUT_S,
    DRAIN_STALL_S,
)

Outcome = Literal["unreachable", "drained", "stalled", "capped", "skipped"]
OnWait = Callable[[int], "Awaitable[None] | None"]

T = TypeVar("T")
_CANCELLED = object()


@dataclass(frozen=True)
class DrainResult:
    """How a drain ended: ``outcome``, the last known ``queued`` count, elapsed time."""

    outcome: Outcome
    queued: int
    elapsed_s: float


async def _call_maybe_async(on_wait: OnWait, remaining: int) -> None:
    result = on_wait(remaining)
    if inspect.isawaitable(result):
        await result


async def _race_cancel(awaitable: Awaitable[T], cancel: asyncio.Event | None) -> T | object:
    """Run ``awaitable`` to completion, or return ``_CANCELLED`` if ``cancel`` fires
    first. Never uses ``wait_for``, so a 3.10 cancellation race can't swallow the
    cancel: the loser of the race is cancelled and awaited before returning.
    """
    task = asyncio.ensure_future(awaitable)
    if cancel is None:
        return await task

    cancel_task = asyncio.ensure_future(cancel.wait())
    done, _pending = await asyncio.wait({task, cancel_task}, return_when=asyncio.FIRST_COMPLETED)
    if task in done:
        cancel_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await cancel_task
        return task.result()

    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    return _CANCELLED


async def drain(
    control: ControlClient,
    *,
    cancel: asyncio.Event | None = None,
    on_wait: OnWait | None = None,
) -> DrainResult:
    """Wait for the app's queued ``log()`` events to leave; never raises."""
    start = time.monotonic()
    last_queued = 0
    consecutive_failures = 0
    last_progress: tuple[int, int, int] | None = None
    last_progress_at = start
    last_reported_at: float | None = None

    while True:
        if cancel is not None and cancel.is_set():
            return DrainResult("skipped", last_queued, time.monotonic() - start)

        try:
            snapshot = await _race_cancel(
                control.metrics(timeout_s=DRAIN_REQUEST_TIMEOUT_S), cancel
            )
        except httpx.ConnectError:
            # A refused loopback connection means the process itself is gone;
            # there's nothing left to wait for.
            return DrainResult("unreachable", last_queued, time.monotonic() - start)
        except Exception:  # noqa: BLE001 - any other failure counts toward the cap below
            snapshot = None
        else:
            if snapshot is _CANCELLED:
                return DrainResult("skipped", last_queued, time.monotonic() - start)

        now = time.monotonic()

        # A snapshot that isn't a usable mapping (a malformed body, or one
        # whose shape `queued()` can't read) counts as a failed poll, not a
        # crash: this whole block never lets an exception escape the drain.
        polled_queued: int | None = None
        if snapshot is not None:
            try:
                polled_queued = queued(snapshot)
            except Exception:  # noqa: BLE001 - malformed body, treated as a failed poll
                polled_queued = None

        if polled_queued is None:
            consecutive_failures += 1
            if consecutive_failures >= DRAIN_FAILURES_MAX:
                return DrainResult("unreachable", last_queued, now - start)
        else:
            consecutive_failures = 0
            last_queued = polled_queued
            if last_queued <= 0:
                return DrainResult("drained", 0, now - start)

            try:
                progress = (
                    counter(snapshot, KEY_SENT),
                    counter(snapshot, KEY_DROPPED_RETRY),
                    http_errors_total(snapshot),
                )
            except Exception:  # noqa: BLE001 - progress bookkeeping never fails the drain
                progress = None
            if progress is not None:
                if progress != last_progress:
                    last_progress = progress
                    last_progress_at = now
                elif now - last_progress_at >= DRAIN_STALL_S:
                    # Stalling is about a live app whose own progress has
                    # stopped; a run of failed polls is judged solely by the
                    # failure cap above, never by this clock.
                    return DrainResult("stalled", last_queued, now - start)

            # Only a poll that actually found a positive backlog is worth
            # reporting; a failed poll must never show a stale or zeroed
            # count while it's throttled quiet.
            if on_wait is not None and (
                last_reported_at is None or now - last_reported_at >= DRAIN_REPORT_EVERY_S
            ):
                with contextlib.suppress(Exception):
                    await _call_maybe_async(on_wait, last_queued)
                last_reported_at = now

        if now - start >= DRAIN_MAX_S:
            return DrainResult("capped", last_queued, now - start)

        outcome = await _race_cancel(asyncio.sleep(DRAIN_POLL_S), cancel)
        if outcome is _CANCELLED:
            return DrainResult("skipped", last_queued, time.monotonic() - start)


__all__ = ["DrainResult", "Outcome", "drain"]
