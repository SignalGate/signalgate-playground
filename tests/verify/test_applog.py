"""Reading what a handler raised from the sample app's log.

Samples are written the way the app's server logs an exception: its own
log line, then Python's traceback. Absolute paths are built from the test's
temp dir and the interpreter's own paths, never typed.
"""

from __future__ import annotations

import asyncio
import sysconfig
from pathlib import Path

import pytest

from playground.copy.en import text
from playground.verify import probes
from playground.verify.applog import (
    MESSAGE_MAX_CHARS,
    HandlerError,
    log_offset,
    read_handler_errors,
)
from playground.verify.probes import Raised, Step, match_errors, read_raised

HEAD = "ERROR:    Exception in ASGI application\nTraceback (most recent call last):\n"
CAUSE = "\nThe above exception was the direct cause of the following exception:\n\n"
DURING = "\nDuring handling of the above exception, another exception occurred:\n\n"


def _site(name: str = "mod.py") -> Path:
    return Path(sysconfig.get_path("purelib")) / "somepkg" / name


def _frame(path: Path, line: int, function: str, code: str = "pass") -> str:
    return f'  File "{path}", line {line}, in {function}\n    {code}\n'


@pytest.fixture
def root(tmp_path: Path) -> Path:
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "routes.py").write_text("# routes\n")
    return tmp_path


@pytest.fixture
def routes(root: Path) -> Path:
    return root / "app" / "routes.py"


def _read(tmp_path: Path, log: str, routes: Path, offset: int = 0) -> list[HandlerError]:
    path = tmp_path / "app.log"
    path.write_text(log, encoding="utf-8")
    return read_handler_errors(path, offset, routes)


def test_a_single_traceback_gives_its_error_and_routes_line(tmp_path, routes):
    log = (
        HEAD
        + _frame(_site("h11_impl.py"), 403, "run_asgi")
        + _frame(routes, 57, "confirm", "ev = sg_event(request, body.user, body.signalgate_log)")
        + _frame(_site("main.py"), 991, "__getattr__", "raise AttributeError(...)")
        + "AttributeError: 'ConfirmBody' object has no attribute 'signalgate_log'\n"
    )
    assert _read(tmp_path, log, routes) == [
        HandlerError(
            "AttributeError",
            "'ConfirmBody' object has no attribute 'signalgate_log'",
            57,
            "confirm",
        )
    ]


def test_the_innermost_routes_line_and_the_outermost_routes_function_are_kept(tmp_path, routes):
    log = (
        HEAD
        + _frame(routes, 40, "action", "helper(body)")
        + _frame(routes, 12, "helper", "return body.missing")
        + _frame(_site(), 5, "inner")
        + "KeyError: 'missing'\n"
    )
    [error] = _read(tmp_path, log, routes)
    assert (error.line, error.function) == (12, "action")
    assert (error.error, error.message) == ("KeyError", "'missing'")


def test_an_error_in_a_lambda_is_kept_with_the_handler_around_it(tmp_path, routes):
    log = (
        HEAD
        + _frame(_site("routing.py"), 212, "run_endpoint_function")
        + _frame(routes, 18, "confirm", "send(ev)")
        + _frame(routes, 15, "<lambda>", "send = lambda ev: sg().log(ev, timeout=2.0)")
        + "TypeError: log() got an unexpected keyword argument 'timeout'\n"
    )
    [error] = _read(tmp_path, log, routes)
    assert (error.line, error.function) == (15, "confirm")


def test_a_chained_traceback_counts_once_with_its_last_routes_exception(tmp_path, routes):
    log = (
        HEAD
        + _frame(routes, 23, "action", 'raise KeyError("x")')
        + "KeyError: 'x'\n"
        + CAUSE
        + "Traceback (most recent call last):\n"
        + _frame(_site(), 7, "run")
        + _frame(routes, 25, "action", 'raise RuntimeError("wrapped") from e')
        + "RuntimeError: wrapped\n"
    )
    assert _read(tmp_path, log, routes) == [HandlerError("RuntimeError", "wrapped", 25, "action")]


def test_a_chain_whose_last_part_never_reached_routes_keeps_the_part_that_did(tmp_path, routes):
    log = (
        HEAD
        + _frame(routes, 30, "action", "sg().log(ev)")
        + _frame(_site(), 12, "log")
        + "ValueError: bad event\n"
        + DURING
        + "Traceback (most recent call last):\n"
        + _frame(_site("errors.py"), 88, "__call__")
        + "RuntimeError: middleware failed\n"
    )
    assert _read(tmp_path, log, routes) == [HandlerError("ValueError", "bad event", 30, "action")]


def test_each_logged_exception_is_read_in_order(tmp_path, routes):
    log = (
        HEAD
        + _frame(routes, 18, "action")
        + "TypeError: first\n"
        + HEAD
        + _frame(routes, 33, "confirm")
        + "LookupError: second\n"
        + "INFO:     127.0.0.1:5000 - something else\n"
    )
    errors = _read(tmp_path, log, routes)
    assert [(e.error, e.line, e.function) for e in errors] == [
        ("TypeError", 18, "action"),
        ("LookupError", 33, "confirm"),
    ]


def test_a_traceback_with_no_routes_frame_is_not_a_handler_error(tmp_path, routes):
    log = HEAD + _frame(_site(), 9, "run") + "OSError: something outside the routes\n"
    assert _read(tmp_path, log, routes) == []
    other = routes.parent / "other.py"
    assert _read(tmp_path, HEAD + _frame(other, 3, "action") + "ValueError: no\n", routes) == []


def test_envelope_like_runs_are_cut_and_keys_masked(tmp_path, routes):
    envelope = "QUJD" * 16
    key = "pk_live_" + "Zz9_" * 8
    message = f"bad payload {{'encrypted': '{envelope}', 'nonce': 'n-1'}} with {key}"
    log = HEAD + _frame(routes, 20, "action") + f"ValueError: {message}\n"
    [error] = _read(tmp_path, log, routes)
    assert envelope not in error.message
    assert key not in error.message
    assert text("verify.ellipsis") in error.message
    assert text("verify.key_masked") in error.message
    assert "'nonce': 'n-1'" in error.message


def test_paths_under_the_repo_root_are_shortened(tmp_path, root, routes):
    client = root / "app" / "signalgate_client.py"
    message = f"cannot import name 'x' from 'app.signalgate_client' ({client})"
    log = (
        HEAD
        + _frame(routes, 4, "action", "from app.signalgate_client import x")
        + (f"ImportError: {message}\n")
    )
    [error] = _read(tmp_path, log, routes)
    assert error.message == (
        "cannot import name 'x' from 'app.signalgate_client' (app/signalgate_client.py)"
    )


def test_a_long_message_is_capped(tmp_path, routes):
    words = " ".join(["word"] * 100)
    log = HEAD + _frame(routes, 20, "action") + f"ValueError: {words}\n"
    [error] = _read(tmp_path, log, routes)
    assert len(error.message) == MESSAGE_MAX_CHARS
    assert error.message.endswith(text("verify.ellipsis"))


def test_an_exception_raised_with_no_message(tmp_path, routes):
    log = HEAD + _frame(routes, 20, "action", "raise RuntimeError") + "RuntimeError\n"
    assert _read(tmp_path, log, routes) == [HandlerError("RuntimeError", "", 20, "action")]


def test_only_what_was_logged_after_the_offset_is_read(tmp_path, routes):
    before = HEAD + _frame(routes, 11, "action") + "ValueError: before\n"
    after = HEAD + _frame(routes, 22, "action") + "ValueError: after\n"
    path = tmp_path / "app.log"
    path.write_text(before, encoding="utf-8")
    offset = log_offset(path)
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(after)
    assert [e.message for e in read_handler_errors(path, offset, routes)] == ["after"]
    assert [e.message for e in read_handler_errors(path, 0, routes)] == ["before", "after"]


def test_a_log_that_was_started_again_is_read_from_its_start(tmp_path, routes):
    log = HEAD + _frame(routes, 22, "action") + "ValueError: new\n"
    assert [e.message for e in _read(tmp_path, log, routes, offset=10**6)] == ["new"]


def test_a_missing_or_unreadable_log_reads_as_no_errors(tmp_path, routes):
    missing = tmp_path / "missing.log"
    assert log_offset(missing) == 0
    assert read_handler_errors(missing, 0, routes) == []
    assert read_handler_errors(tmp_path, 0, routes) == []  # a directory, not a file


def test_a_frame_through_a_symlink_still_resolves_to_routes(tmp_path):
    real_root = tmp_path / "real"
    (real_root / "app").mkdir(parents=True)
    (real_root / "app" / "routes.py").write_text("# routes\n")
    link_root = tmp_path / "link"
    link_root.symlink_to(real_root, target_is_directory=True)
    log = HEAD + _frame(real_root / "app" / "routes.py", 9, "action") + "ValueError: via link\n"
    [error] = _read(tmp_path, log, link_root / "app" / "routes.py")
    assert (error.line, error.message) == (9, "via link")


def test_a_log_line_that_breaks_off_a_traceback_is_not_an_error(tmp_path, routes):
    log = HEAD + _frame(routes, 9, "action") + "not an exception line at all\n"
    assert _read(tmp_path, log, routes) == []


# -- pairing the errors with the requests that got a 5xx ----------------------


def _step(name: str, handler: str, status: int | None) -> Step:
    return Step(name, handler, f"run-{name}", "pg-u-1", "192.0.2.1", status=status)


def test_each_5xx_request_takes_the_next_error_of_its_handler():
    steps = {
        "p1": _step("p1", "action", 500),
        "p4": _step("p4", "confirm", 400),
        "p5": _step("p5", "confirm", 500),
        "p6": _step("p6", "action", 500),
        "p7": _step("p7", "action", 200),
    }
    first = HandlerError("TypeError", "first", 10, "action")
    confirm = HandlerError("AttributeError", "confirm", 20, "confirm")
    second = HandlerError("TypeError", "second", 11, "action")
    matched, complete = match_errors(steps, [first, confirm, second])
    assert complete
    assert matched == {"p1": first, "p5": confirm, "p6": second}


def test_an_error_raised_in_a_helper_pairs_with_the_handler_it_went_through(tmp_path, routes):
    log = (
        HEAD
        + _frame(routes, 20, "action", "log_it(request, body)")
        + _frame(routes, 9, "log_it", "sg().log(ev, timeout=2.0)")
        + "TypeError: unexpected keyword\n"
    )
    errors = _read(tmp_path, log, routes)
    matched, complete = match_errors({"p1": _step("p1", "action", 500)}, errors)
    assert complete
    assert (matched["p1"].line, matched["p1"].function) == (9, "action")


def test_a_5xx_without_an_error_of_its_handler_is_left_unmatched():
    steps = {"p1": _step("p1", "action", 500), "p5": _step("p5", "confirm", 502), "p2": None}
    helper = HandlerError("KeyError", "'x'", 5, "helper")
    matched, complete = match_errors(steps, [helper])
    assert (matched, complete) == ({}, False)


def test_raised_only_answers_for_a_5xx():
    error = HandlerError("TypeError", "x", 10, "action")
    raised = Raised("app/routes.py", {"p1": error, "p2": error})
    assert raised.of(_step("p1", "action", 500)) is error
    assert raised.of(_step("p2", "action", 404)) is None
    assert raised.of(_step("p3", "action", 500)) is None


def test_the_log_is_read_once_more_when_the_traceback_comes_late(monkeypatch, routes, tmp_path):
    late = HandlerError("TypeError", "late", 10, "action")
    answers = [[], [late]]
    calls: list[int] = []

    def fake_read(path: Path, offset: int, routes_path: Path) -> list[HandlerError]:
        calls.append(offset)
        return answers[len(calls) - 1]

    monkeypatch.setattr(probes, "read_handler_errors", fake_read)
    monkeypatch.setattr(probes, "APP_LOG_SETTLE_S", 0)
    steps = {"p1": _step("p1", "action", 500)}
    raised = asyncio.run(read_raised(tmp_path / "app.log", 7, routes, steps, "app/routes.py"))
    assert calls == [7, 7]
    assert raised == Raised("app/routes.py", {"p1": late})


def test_the_log_is_not_read_when_no_request_got_a_5xx(monkeypatch, routes, tmp_path):
    def fail_read(*_args: object) -> list[HandlerError]:
        raise AssertionError("read without a 5xx")

    monkeypatch.setattr(probes, "read_handler_errors", fail_read)
    steps = {"p1": _step("p1", "action", 200), "p5": None}
    raised = asyncio.run(read_raised(tmp_path / "app.log", 0, routes, steps, "app/routes.py"))
    assert raised == Raised("app/routes.py")
