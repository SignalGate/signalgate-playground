"""Hand-built recordings for judging runs from evidence alone, with no app or server.

Every record starts from the contract's own samples and is checked against
its spec, so a gate that only works on wishful records fails here too.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from playground.verify.evidence import Evidence
from tests.contract.samples import (
    LEDGER_ENTRY,
    SAMPLE_ATTEMPT,
    SAMPLE_CALL,
    assert_same_shape,
    record_spec,
)

RUN = "run-recorded"
BOOT = "boot-recorded"


def _checked(record: dict[str, Any]) -> dict[str, Any]:
    assert_same_shape(record, record_spec(record))
    return record


def log_call(seq: int, ref: str, job: int, *, method: str = "playground.action", **extra: Any):
    return _checked(
        {
            **SAMPLE_CALL,
            "op": "log",
            "seq": seq,
            "ref": ref,
            "run": RUN,
            "method": method,
            "nonce": f"nonce-{seq}",
            "t0": None,
            "t1": None,
            "outcome": "enqueued",
            "action": None,
            "processing_time_us": None,
            "log_job": job,
            **extra,
        }
    )


def log_attempt(job: int, *, attempt: int = 1, status: int | None = 200, **extra: Any):
    return _checked(
        {
            **SAMPLE_ATTEMPT,
            "endpoint": "log",
            "call_seq": None,
            "log_job": job,
            "attempt": attempt,
            "status": status,
            "verdict": None,
            "processing_time_us": None,
            **extra,
        }
    )


def timed_out(job: int, *, attempt: int = 1) -> dict[str, Any]:
    return log_attempt(
        job,
        attempt=attempt,
        status=None,
        error_class="TimeoutError",
        timed_out=True,
        timeout_phase="read",
    )


def check_call(seq: int, ref: str, *, action: str = "allow", **extra: Any) -> dict[str, Any]:
    return _checked({**SAMPLE_CALL, "seq": seq, "ref": ref, "run": RUN, "action": action, **extra})


def check_attempt(call_seq: int, *, verdict: str | None = "allow", **extra: Any):
    return _checked({**SAMPLE_ATTEMPT, "call_seq": call_seq, "verdict": verdict, **extra})


def action_entry(seq: int, ref: str) -> dict[str, Any]:
    entry = {
        "kind": "action",
        "seq": seq,
        "ref": ref,
        "user": "user-recorded",
        "client_ip": "198.51.100.7",
        "run": RUN,
    }
    assert_same_shape(entry, LEDGER_ENTRY)
    return entry


def evidence(
    records: Iterable[Mapping[str, Any]],
    ledger: Iterable[Mapping[str, Any]] = (),
    metrics: Mapping[str, int] | None = None,
) -> Evidence:
    return Evidence(
        boot_id=BOOT,
        records=tuple(records),
        ledger=tuple(ledger),
        metrics=dict(metrics or {}),
        clients_created=0,
    )


def accepted_logs(
    count: int, *, method: str = "playground.action", start: int = 1
) -> list[dict[str, Any]]:
    """``count`` log calls, each with one accepted attempt."""
    records: list[dict[str, Any]] = []
    for job in range(start, start + count):
        records.append(log_call(job * 10, f"ref-{job}", job, method=method))
        records.append(log_attempt(job))
    return records


__all__ = [
    "BOOT",
    "RUN",
    "accepted_logs",
    "action_entry",
    "check_attempt",
    "check_call",
    "evidence",
    "log_attempt",
    "log_call",
    "timed_out",
]
