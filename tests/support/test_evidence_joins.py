"""Tests for the test-only join helpers over hand-built recordings.

Every recording below follows the shapes pinned in ``tests/contract/samples.py``,
so a join helper that only works on wishful records would fail here too.
"""

from __future__ import annotations

from tests.contract.samples import (
    SAMPLE_ATTEMPT,
    SAMPLE_CALL,
    SAMPLE_GUARD_ATTEMPT,
    SAMPLE_LOG_ATTEMPT_TIMED_OUT,
    SAMPLE_LOG_CALL_RAISED,
    assert_same_shape,
    record_spec,
)
from tests.support.evidence_joins import is_local, join_checks, join_logs, probable_duplicate


def _shape_checked(record: dict) -> dict:
    assert_same_shape(record, record_spec(record))
    return record


def _check_call(seq: int) -> dict:
    return _shape_checked({**SAMPLE_CALL, "seq": seq})


def _attempt(call_seq: int | None = None, log_job: int | None = None, **extra) -> dict:
    return _shape_checked({**SAMPLE_ATTEMPT, "call_seq": call_seq, "log_job": log_job, **extra})


def _log_call(log_job: int) -> dict:
    return _shape_checked({**SAMPLE_LOG_CALL_RAISED, "seq": log_job + 100, "log_job": log_job})


# --- join_checks ---------------------------------------------------------------


def test_join_checks_joins_a_calls_own_attempt_by_call_seq():
    call = _check_call(8)
    attempt = _attempt(call_seq=8, attempt=1)
    other_attempt = _attempt(call_seq=9, attempt=1)
    joined = join_checks([call, attempt, other_attempt])
    assert joined == {8: [attempt]}


def test_join_checks_keeps_every_check_call_independent():
    first, second = _check_call(1), _check_call(2)
    first_attempt = _attempt(call_seq=1, attempt=1)
    second_attempt = _attempt(call_seq=2, attempt=1)
    joined = join_checks([first, first_attempt, second, second_attempt])
    assert joined == {1: [first_attempt], 2: [second_attempt]}


def test_join_checks_gives_an_empty_list_for_a_call_with_no_attempt():
    call = _check_call(3)
    assert join_checks([call]) == {3: []}


def test_join_checks_orders_attempts_by_their_own_attempt_number():
    call = _check_call(4)
    first = _attempt(call_seq=4, attempt=1, status=None, error_class="TimeoutError")
    second = _attempt(call_seq=4, attempt=2, status=422)
    # Fed out of order; the join must still sort them.
    joined = join_checks([call, second, first])
    assert joined[4] == [first, second]


def test_join_checks_ignores_log_and_construct_records():
    call = _check_call(5)
    attempt = _attempt(call_seq=5, attempt=1)
    log_call = _log_call(1)
    joined = join_checks([call, attempt, log_call])
    assert joined == {5: [attempt]}


# --- join_logs -------------------------------------------------------------------


def test_join_logs_joins_a_calls_attempts_by_log_job():
    call = _log_call(1)
    timed_out = _shape_checked({**SAMPLE_LOG_ATTEMPT_TIMED_OUT, "log_job": 1, "attempt": 1})
    sent = _attempt(log_job=1, attempt=2, endpoint="log", status=200)
    joined = join_logs([call, timed_out, sent])
    assert joined == {1: [timed_out, sent]}


def test_join_logs_keeps_every_job_independent():
    first_call, second_call = _log_call(1), _log_call(2)
    first_attempt = _attempt(log_job=1, attempt=1, endpoint="log", status=200)
    second_attempt = _attempt(log_job=2, attempt=1, endpoint="log", status=200)
    joined = join_logs([first_call, first_attempt, second_call, second_attempt])
    assert joined == {1: [first_attempt], 2: [second_attempt]}


def test_join_logs_leaves_out_a_call_that_never_enqueued():
    call = {**_log_call(1), "log_job": None, "outcome": "not_enqueued"}
    assert join_logs([call]) == {}


def test_join_logs_ignores_check_and_construct_records():
    call = _log_call(1)
    attempt = _attempt(log_job=1, attempt=1, endpoint="log", status=200)
    check_call = _check_call(9)
    joined = join_logs([call, attempt, check_call])
    assert joined == {1: [attempt]}


# --- is_local --------------------------------------------------------------------


def test_is_local_is_true_for_a_guard_refusal():
    assert is_local(_shape_checked(SAMPLE_GUARD_ATTEMPT)) is True


def test_is_local_is_true_for_a_canary_answer():
    assert is_local(_attempt(local="canary")) is True


def test_is_local_is_false_for_a_forwarded_attempt():
    assert is_local(_attempt(local=None)) is False


def test_is_local_is_false_for_a_non_attempt_record():
    assert is_local(_check_call(1)) is False


# --- probable_duplicate -----------------------------------------------------------


def test_a_timeout_then_a_422_is_a_probable_duplicate():
    timed_out = _attempt(log_job=1, attempt=1, status=None, timed_out=True, timeout_phase="read")
    rejected = _attempt(log_job=1, attempt=2, status=422, timed_out=False)
    assert probable_duplicate([timed_out, rejected]) is True


def test_a_bare_422_on_the_first_attempt_is_not_a_probable_duplicate():
    rejected = _attempt(log_job=1, attempt=1, status=422, timed_out=False)
    assert probable_duplicate([rejected]) is False


def test_a_5xx_then_a_422_is_not_a_probable_duplicate():
    server_error = _attempt(log_job=1, attempt=1, status=500, timed_out=False)
    rejected = _attempt(log_job=1, attempt=2, status=422, timed_out=False)
    assert probable_duplicate([server_error, rejected]) is False


def test_probable_duplicate_reads_attempts_out_of_order():
    timed_out = _attempt(log_job=1, attempt=1, status=None, timed_out=True, timeout_phase="connect")
    rejected = _attempt(log_job=1, attempt=2, status=422, timed_out=False)
    # Fed out of order; the join must still sort them before deciding.
    assert probable_duplicate([rejected, timed_out]) is True


def test_a_lone_timeout_with_no_later_answer_is_not_a_probable_duplicate():
    timed_out = _attempt(log_job=1, attempt=1, status=None, timed_out=True, timeout_phase="read")
    assert probable_duplicate([timed_out]) is False


def test_a_timeout_then_a_200_is_not_a_probable_duplicate():
    timed_out = _attempt(log_job=1, attempt=1, status=None, timed_out=True, timeout_phase="read")
    sent = _attempt(log_job=1, attempt=2, status=200, timed_out=False)
    assert probable_duplicate([timed_out, sent]) is False


def test_a_timeout_then_a_5xx_is_not_a_probable_duplicate():
    timed_out = _attempt(log_job=1, attempt=1, status=None, timed_out=True, timeout_phase="read")
    server_error = _attempt(log_job=1, attempt=2, status=500, timed_out=False)
    assert probable_duplicate([timed_out, server_error]) is False
