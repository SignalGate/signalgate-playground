"""The fixture corpus: every wrong routes file fails with its own finding, every right one passes.

Each fixture is served inside the real sample app (see ``harness``) and
checked by the real gate: the code gate with envelopes from a probe lease
on the fake server, or the rehearsal with canaries on the laptop.
"""

from __future__ import annotations

import re

import pytest

from playground.copy.en import text
from playground.verify import Verdict, gate_lines
from playground.verify import findings as F
from playground.verify.messages import MAX_COLUMNS
from tests.verify.harness import FIXTURES, ROOT, code_gate, rehearsal

# fixture name: (gate, chapter, the finding it must fail with)
WRONG = {
    "log_missing": ("code", 3, F.LOG_MISSING),
    "log_before_action": ("code", 3, F.LOG_BEFORE_ACTION),
    "log_in_finally": ("code", 3, F.LOG_WITHOUT_ACTION),
    "success_log_missing": ("code", 3, F.SUCCESS_LOG_MISSING),
    "success_log_on_wrong_token": ("code", 3, F.SUCCESS_LOG_ON_FAILURE),
    "success_logs_action_method": ("code", 3, F.WRONG_METHOD),
    "method_typo": ("code", 3, F.WRONG_METHOD),
    "logged_twice": ("code", 3, F.LOGGED_TWICE),
    "log_reads_check_field": ("code", 3, F.WRONG_ENVELOPE_FIELD),
    "success_reuses_action_envelope": ("code", 3, F.ENVELOPE_FROM_OTHER_REQUEST),
    "constant_user": ("code", 3, F.USER_ID_MISMATCH),
    "server_address_as_ip": ("code", 3, F.IP_NOT_FORWARDED),
    "naive_timestamp": ("code", 3, F.EVENT_TIME_INVALID),
    "changed_envelope": ("code", 3, F.LOG_REJECTED),
    "payload_as_dict": ("code", 3, F.HANDLER_ERROR),
    "handler_raises": ("code", 3, F.HANDLER_ERROR),
    "helper_raises": ("code", 3, F.HANDLER_ERROR),
    "lambda_raises": ("code", 3, F.HANDLER_ERROR),
    "confirm_reads_log_field": ("code", 3, F.CONFIRM_READS_LOG_FIELD),
    "request_failed_status": ("code", 3, F.REQUEST_FAILED),
    "unguarded_log": ("code", 3, F.MISSING_ENVELOPE_BREAKS_REQUEST),
    "bypasses_sg_event": ("code", 3, F.SDK_ERROR_BREAKS_REQUEST),
    "client_per_request": ("code", 3, F.CLIENT_PER_REQUEST),
    "closes_client": ("code", 3, F.CLIENT_CLOSED),
    "check_in_chapter_3": ("code", 3, F.CHECK_TOO_EARLY),
    "envelope_reused": ("code", 5, F.ENVELOPE_REUSED),
    "check_in_async_def": ("code", 5, F.CHECK_BLOCKS_EVENT_LOOP),
    "blocking_helper_in_async_def": ("rehearsal", 5, F.CHECK_BLOCKS_EVENT_LOOP),
    "no_check": ("rehearsal", 5, F.CHECK_MISSING),
    "check_after_action": ("rehearsal", 5, F.CHECK_AFTER_ACTION),
    "verdict_ignored": ("rehearsal", 5, F.VERDICT_IGNORED),
    "gates_on_not_allow": ("rehearsal", 5, F.GATES_ON_WRONG_VERDICT),
    "logs_blocked_action": ("rehearsal", 5, F.BLOCKED_ACTION_LOGGED),
    "unguarded_check": ("rehearsal", 5, F.CHECK_ERROR_BREAKS_REQUEST),
    "block_answers_200": ("rehearsal", 5, F.NO_403_ON_BLOCK),
}

RIGHT_CHAPTER_3 = ("ch3_plain", "ch3_style")
RIGHT_CHAPTER_5 = (
    "ch5_plain",
    "ch5_style",
    "ch5_score_logging",
    "ch5_async_thread",
    "ch5_threadpool",
)

# Findings about a whole run come from recorded evidence (test_data_gate.py);
# an API key in the code is written by test_static.py, never committed.
NOT_FROM_A_ROUTES_FILE = {
    F.KEY_IN_CODE,
    F.LOG_DROPPED,
    F.LOGS_NOT_DELIVERED,
    F.EVENTS_NOT_STORED,
    F.DUPLICATE_DELIVERY,
}


def _assert_renders(result) -> None:
    lines = gate_lines(result)
    assert lines
    assert all(len(line) <= MAX_COLUMNS for line in lines), lines


@pytest.mark.parametrize("name", sorted(WRONG))
def test_each_wrong_fixture_fails_with_its_finding(name, fake):
    gate, chapter, expected = WRONG[name]
    path = FIXTURES / "wrong" / f"{name}.py"
    result = code_gate(path, fake, chapter) if gate == "code" else rehearsal(path)
    assert expected in result.finding_ids, gate_lines(result)
    wanted = Verdict.PASS if expected in F.NOTES else Verdict.FAIL
    assert result.verdict is wanted, gate_lines(result)
    _assert_renders(result)


@pytest.mark.parametrize("name", RIGHT_CHAPTER_3)
def test_right_chapter_3_variants_pass(name, fake):
    result = code_gate(FIXTURES / "right" / f"{name}.py", fake, 3)
    assert result.verdict is Verdict.PASS, gate_lines(result)
    assert result.findings == ()
    assert result.accepted >= 2
    _assert_renders(result)


@pytest.mark.parametrize("name", RIGHT_CHAPTER_5)
def test_right_chapter_5_variants_pass_both_gates(name, fake):
    path = FIXTURES / "right" / f"{name}.py"
    code = code_gate(path, fake, 5)
    assert code.verdict is Verdict.PASS, gate_lines(code)
    assert code.findings == ()
    rehearsed = rehearsal(path)
    assert rehearsed.verdict is Verdict.PASS, gate_lines(rehearsed)
    assert rehearsed.findings == ()


def test_every_fixture_file_is_in_the_corpus():
    wrong = {path.stem for path in (FIXTURES / "wrong").glob("*.py")}
    right = {path.stem for path in (FIXTURES / "right").glob("*.py")}
    assert wrong == set(WRONG)
    assert right == set(RIGHT_CHAPTER_3) | set(RIGHT_CHAPTER_5)


def test_every_finding_a_routes_file_can_cause_has_a_fixture():
    covered = {expected for _, _, expected in WRONG.values()}
    assert covered == set(F.ALL) - NOT_FROM_A_ROUTES_FILE


def _display(name: str) -> str:
    return (FIXTURES / "wrong" / f"{name}.py").relative_to(ROOT).as_posix()


def _line_of(name: str, needle: str) -> int:
    lines = (FIXTURES / "wrong" / f"{name}.py").read_text(encoding="utf-8").splitlines()
    return next(number for number, line in enumerate(lines, 1) if needle in line)


def _flat(lines: list[str]) -> str:
    return re.sub(r"\s+", " ", " ".join(lines))


def test_a_confirm_that_reads_the_log_field_is_named_with_its_line(fake):
    name = "confirm_reads_log_field"
    result = code_gate(FIXTURES / "wrong" / f"{name}.py", fake, 3)
    lines = gate_lines(result)
    assert result.verdict is Verdict.FAIL, lines
    ids = [f.id for f in result.findings]
    assert F.CONFIRM_READS_LOG_FIELD in ids, lines
    assert F.REQUEST_FAILED not in ids, lines

    title = text("finding.CONFIRM_READS_LOG_FIELD.title")
    assert _flat(lines).count(title) == 1, lines
    line = _line_of(name, "body.signalgate_log")
    where = f"  {_display(name)}:{line}   "
    assert [entry for entry in lines if entry.startswith(where)] != [], lines
    assert sum(entry.startswith(f"  {_display(name)}:") for entry in lines) == 1, lines
    sent = "Your app answered 500: AttributeError: 'ConfirmBody' object has no attribute"
    assert sent in _flat(lines), lines
    _assert_renders(result)


def test_a_handler_error_shows_the_error_and_where_it_was_raised(fake):
    name = "handler_raises"
    result = code_gate(FIXTURES / "wrong" / f"{name}.py", fake, 3)
    lines = gate_lines(result)
    found = [f for f in result.findings if f.id == F.HANDLER_ERROR]
    assert len(found) == 1, lines
    line = _line_of(name, "timeout=")
    assert found[0].site is not None
    assert (found[0].site.file, found[0].site.line) == (_display(name), line)
    assert found[0].params["handler"] == "action"
    assert found[0].params["location"] == f"{_display(name)}:{line}"
    assert found[0].params["error"].startswith("TypeError: "), found[0].params
    title = text("finding.HANDLER_ERROR.title", handler="action")
    assert text("verify.title", mark=text("mark.fail"), title=title) in lines, lines
    assert any(entry.startswith("  Error: TypeError: ") for entry in lines), lines
    _assert_renders(result)


@pytest.mark.parametrize("name", ["helper_raises", "lambda_raises"])
def test_an_error_raised_below_the_handler_names_the_handler_and_the_line(name, fake):
    result = code_gate(FIXTURES / "wrong" / f"{name}.py", fake, 3)
    lines = gate_lines(result)
    assert F.REQUEST_FAILED not in result.finding_ids, lines
    found = [f for f in result.findings if f.id == F.HANDLER_ERROR]
    assert len(found) == 1, lines
    # Paired with the handler the request entered; the line is where it was raised.
    line = _line_of(name, "timeout=")
    assert found[0].site is not None
    assert (found[0].site.file, found[0].site.line) == (_display(name), line)
    assert found[0].params["handler"] == "action"
    assert found[0].params["location"] == f"{_display(name)}:{line}"
    assert found[0].params["error"].startswith("TypeError: "), found[0].params
    _assert_renders(result)


def test_an_sdk_error_with_a_readable_traceback_is_a_handler_error(fake):
    result = code_gate(FIXTURES / "wrong" / "payload_as_dict.py", fake, 3)
    found = [f for f in result.findings if f.id == F.HANDLER_ERROR]
    assert len(found) == 1, gate_lines(result)
    assert found[0].params["error"].split(":", 1)[0], found[0].params
    assert any(entry.startswith("  Error: ") for entry in gate_lines(result))


def test_a_500_without_a_traceback_stays_a_failed_request(fake):
    result = code_gate(FIXTURES / "wrong" / "request_failed_status.py", fake, 3)
    ids = result.finding_ids
    assert F.REQUEST_FAILED in ids, gate_lines(result)
    assert F.HANDLER_ERROR not in ids
    assert text("finding.REQUEST_FAILED.fix") in _flat(gate_lines(result))
