"""Reading back, and shape-checking, the records the facade's tests produce.

Each facade test module runs `assert_every_record_matches_its_shape()` from
an autouse fixture after every test, so every `call` and `construct` record
the real facade emits is checked against its contract shape, not only the
ones a test happens to check by hand.
"""

from __future__ import annotations

from typing import Any

from app.kit.calls import calls
from app.kit.local_constants import PAGE_MAX
from tests.contract import samples

SHAPE_CHECKED_KINDS = ("call", "construct")


def read_all_calls() -> list[dict[str, Any]]:
    """Every record in the call log, oldest first, across as many pages as it takes."""
    cursor, seen = 0, []
    while True:
        records, cursor, more, *_ = calls.read_page(cursor, PAGE_MAX)
        seen.extend(records)
        if not more:
            return seen


def assert_every_record_matches_its_shape() -> int:
    """Check every `call` and `construct` record in the log against its spec.

    Also fails when a record could not be stored at all, since such a record
    never reaches the log to be checked. Returns how many records it checked.
    """
    assert calls.record_errors == 0, "a record could not be stored in the call log"
    checked = 0
    for record in read_all_calls():
        if record["kind"] in SHAPE_CHECKED_KINDS:
            samples.assert_same_shape(record, samples.RECORD_SPECS[record["kind"]])
            checked += 1
    return checked


__all__ = ["SHAPE_CHECKED_KINDS", "assert_every_record_matches_its_shape", "read_all_calls"]
