"""Join helpers over recorded calls and attempts, for tests only.

This module is test-only. It proves the recorded fields are enough to join
a check or a log call to the attempts it made, and to tell a probable
duplicate or a local answer apart.

Every helper here reads plain record dicts (as returned by ``CallLog.read_page``
or the ``/calls`` route) and never writes anything back.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

Record = Mapping[str, Any]


def join_checks(records: Iterable[Record]) -> dict[int, list[Record]]:
    """Every check ``call`` record's ``seq``, mapped to its attempts, in order.

    Joins by ``attempt.call_seq == call.seq``. A check call with no
    matching attempt (for example, one the guard never let through the SDK
    at all) still gets an empty list.
    """
    calls = [r for r in records if r.get("kind") == "call" and r.get("op") == "check"]
    attempts = [r for r in records if r.get("kind") == "attempt"]
    joined: dict[int, list[Record]] = {call["seq"]: [] for call in calls}
    for attempt in attempts:
        call_seq = attempt.get("call_seq")
        if call_seq in joined:
            joined[call_seq].append(attempt)
    for seq, bucket in joined.items():
        joined[seq] = sorted(bucket, key=lambda a: a["attempt"])
    return joined


def join_logs(records: Iterable[Record]) -> dict[int, list[Record]]:
    """Every log ``call`` record's ``log_job``, mapped to its attempts, in order.

    Joins by ``attempt.log_job == call.log_job``. A log call with no
    ``log_job`` (it never enqueued) is left out.
    """
    calls = [
        r
        for r in records
        if r.get("kind") == "call" and r.get("op") == "log" and r.get("log_job") is not None
    ]
    attempts = [r for r in records if r.get("kind") == "attempt"]
    joined: dict[int, list[Record]] = {call["log_job"]: [] for call in calls}
    for attempt in attempts:
        log_job = attempt.get("log_job")
        if log_job in joined:
            joined[log_job].append(attempt)
    for job, bucket in joined.items():
        joined[job] = sorted(bucket, key=lambda a: a["attempt"])
    return joined


def is_local(record: Record) -> bool:
    """Whether an attempt was answered on the laptop: ``local in ("guard", "canary")``."""
    return record.get("kind") == "attempt" and record.get("local") in ("guard", "canary")


def probable_duplicate(attempts: Iterable[Record]) -> bool:
    """Whether one job's attempts, taken in ``attempt`` order, are a probable duplicate.

    True iff an earlier attempt has ``timed_out`` true and a later attempt in
    the same job has status 422. The later attempt is then a retry of a
    delivery that may already have landed. Anything else is not a probable
    duplicate: a single attempt, a 422 with no timed-out attempt before it,
    or a timed-out attempt followed only by other statuses.
    """
    ordered = sorted(attempts, key=lambda a: a["attempt"])
    timed_out_seen = False
    for attempt in ordered:
        if timed_out_seen and attempt.get("status") == 422:
            return True
        if attempt.get("timed_out"):
            timed_out_seen = True
    return False


__all__ = ["Record", "is_local", "join_checks", "join_logs", "probable_duplicate"]
