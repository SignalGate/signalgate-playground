"""The data gate, after every traffic run: did the run's events reach SignalGate?

It reads one run's evidence (``evidence.collect`` with the run's id, from a
``mark()`` taken just before the run) and the stored counts SignalGate
reports for the run (the session status's ``stored_since``):

- no ``log()`` event was dropped because the queue was full or the client
  closed (SDK counter deltas);
- every 4xx on a log is a probable duplicate delivery: a 422 on a retry
  whose earlier attempt, with the same key, timed out;
- per method, accepted plus probable duplicates reach ``DELIVERY_MIN_SHARE``
  of the events the app posted;
- per method, the stored count reaches the same share (``wait_for_stored``
  polls for it for up to the server's ``limits.ingest_wait_s``). Still short
  after that wait, the check is NOT RUN, not FAIL: SignalGate accepted the
  events, and storing them is on its side. The finding ID is kept.

A run in which more than ``FAIL_OPEN_MAX_SHARE`` of the checks failed open
still passes, but is labelled degraded. A restart of the app during the run
makes ``collect`` raise instead; the run then doesn't count.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass

from playground.supervisor.control_client import KEY_DROPPED_CLOSED, KEY_DROPPED_QUEUE_FULL
from playground.traffic.local_constants import STORED_POLL_S
from playground.verify import findings as F
from playground.verify.evidence import ACCEPTED, DUPLICATE, REJECTED, Evidence, rejected_status
from playground.verify.model import Check, Finding, GateResult
from playground.verify.probes import ACTION_METHOD, SUCCESS_METHOD

DELIVERY_MIN_SHARE = 0.98
FAIL_OPEN_MAX_SHARE = 0.01

# The status's stored counts are keyed by these names.
KINDS = {"action": ACTION_METHOD, "success": SUCCESS_METHOD}

Counts = Mapping[str, int]


def _enough(got: int, posted: int) -> bool:
    # A hair of slack so float rounding never turns an exact share into a miss.
    return got >= posted * DELIVERY_MIN_SHARE - 1e-9


def posted_counts(ev: Evidence) -> dict[str, int]:
    """How many log events the app enqueued in the run, per kind."""
    counts = dict.fromkeys(KINDS, 0)
    for call in ev.calls("log"):
        for kind, method in KINDS.items():
            if call.get("method") == method and call.get("outcome") == "enqueued":
                counts[kind] += 1
    return counts


@dataclass(frozen=True)
class DataGate:
    """The data gate's checks, the counts behind them, and the degraded label."""

    result: GateResult
    posted: dict[str, int]
    accepted: dict[str, int]
    duplicates: dict[str, int]
    degraded: bool


def data_gate(ev: Evidence, stored: Counts | None) -> DataGate:
    """Judge one run from its evidence and SignalGate's stored counts (None: unknown)."""
    posted = posted_counts(ev)
    accepted = dict.fromkeys(KINDS, 0)
    duplicates = dict.fromkeys(KINDS, 0)
    rejected = 0
    rejected_code: int | None = None
    for call in ev.calls("log"):
        kind = next((k for k, m in KINDS.items() if call.get("method") == m), None)
        if kind is None or call.get("outcome") != "enqueued":
            continue
        outcome = ev.outcome(call)
        if outcome == ACCEPTED:
            accepted[kind] += 1
        elif outcome == DUPLICATE:
            duplicates[kind] += 1
        elif outcome == REJECTED:
            rejected += 1
            rejected_code = rejected_status(ev.attempts(call))

    drops: list[Finding] = []
    for key, variant in ((KEY_DROPPED_QUEUE_FULL, "queue_full"), (KEY_DROPPED_CLOSED, "closed")):
        count = ev.metrics.get(key, 0)
        if count > 0:
            drops.append(F.finding(F.LOG_DROPPED, None, variant, count=count))

    rejections: list[Finding] = []
    if rejected:
        rejections.append(
            F.finding(F.LOG_REJECTED, None, "run", count=rejected, code=rejected_code)
        )
    total_duplicates = sum(duplicates.values())
    if total_duplicates:
        rejections.append(F.finding(F.DUPLICATE_DELIVERY, None, count=total_duplicates))

    checks = [Check.judged("drops", drops), Check.judged("rejections", rejections)]
    if not any(posted.values()):
        checks.append(Check.skipped("delivered", "nothing"))
    else:
        short = [
            F.finding(
                F.LOGS_NOT_DELIVERED,
                None,
                method=KINDS[kind],
                accepted=accepted[kind] + duplicates[kind],
                posted=posted[kind],
            )
            for kind in KINDS
            if not _enough(accepted[kind] + duplicates[kind], posted[kind])
        ]
        checks.append(Check.judged("delivered", short))
    if stored is None:
        checks.append(Check.skipped("stored", "status"))
    else:
        missing = [
            F.finding(
                F.EVENTS_NOT_STORED,
                None,
                method=KINDS[kind],
                stored=stored.get(kind, 0),
                posted=posted[kind],
            )
            for kind in KINDS
            if not _enough(stored.get(kind, 0), posted[kind])
        ]
        if missing:
            checks.append(Check.skipped("stored", "stored", missing))
        else:
            checks.append(Check.judged("stored", missing))

    checks_made = ev.calls("check")
    failed_open = sum(1 for c in checks_made if c.get("outcome") == "failed_open")
    degraded = bool(checks_made) and failed_open > FAIL_OPEN_MAX_SHARE * len(checks_made)
    return DataGate(
        result=GateResult("data", tuple(checks)),
        posted=posted,
        accepted=accepted,
        duplicates=duplicates,
        degraded=degraded,
    )


async def wait_for_stored(
    fetch: Callable[[], Awaitable[Counts | None]],
    posted: Counts,
    wait_s: float,
) -> dict[str, int] | None:
    """Poll ``fetch`` until every kind's stored count is enough, or ``wait_s`` passes.

    ``fetch`` returns the status's ``stored_since`` (or None); ``wait_s`` is
    the session's ``limits.ingest_wait_s``. Returns the last counts seen, or
    None when none could be read.
    """
    deadline = time.monotonic() + wait_s
    latest: dict[str, int] | None = None
    while True:
        try:
            got = await fetch()
        except Exception:  # noqa: BLE001 - a failed read just means "not yet"
            got = None
        if got is not None:
            latest = dict(got)
        if latest is not None and all(_enough(latest.get(k, 0), n) for k, n in posted.items()):
            return latest
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return latest
        await asyncio.sleep(min(STORED_POLL_S, remaining))


__all__ = [
    "DELIVERY_MIN_SHARE",
    "FAIL_OPEN_MAX_SHARE",
    "KINDS",
    "DataGate",
    "data_gate",
    "posted_counts",
    "wait_for_stored",
]
