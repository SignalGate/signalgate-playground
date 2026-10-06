"""Tests for the in-memory call records and the helpers that fill them."""

from __future__ import annotations

import asyncio
import enum
import inspect
import json
import os
import threading
from pathlib import Path

import pytest
import signalgate

from app.kit import calls as calls_module
from app.kit import context
from app.kit.calls import CallLog, bind_call, calls, capture, current_call, j
from app.kit.local_constants import PAGE_MAX, RECORD_STR_MAX

REPO_ROOT = Path(__file__).resolve().parents[2]
THIS_FILE = "tests/app/test_calls.py"


def _record(n: int) -> dict:
    return {"kind": "call", "seq": n, "ref": f"ref-{n}", "site": None}


def _read_all(log: CallLog, limit: int) -> tuple[list[dict], int]:
    cursor, seen = 0, []
    while True:
        records, cursor, more, _, truncated = log.read_page(cursor, limit)
        assert not truncated
        assert len(records) <= limit
        seen.extend(records)
        if not more:
            return seen, cursor


# --- CallLog -----------------------------------------------------------------


def test_the_module_keeps_one_shared_log():
    assert isinstance(calls, CallLog)


def test_records_come_back_in_append_order():
    log = CallLog()
    for n in range(1, 4):
        log.append(_record(n))
    records, next_cursor, more, oldest, truncated = log.read_page(0)
    assert records == [_record(1), _record(2), _record(3)]
    assert (next_cursor, more, oldest, truncated) == (3, False, 0, False)
    assert len(log) == 3


def test_a_record_is_stored_as_it_was_when_appended():
    log = CallLog()
    record = {"kind": "call", "seq": 1, "site": {"file": "app/routes.py", "line": 3}}
    log.append(record)
    record["seq"] = 2
    record["site"]["line"] = 4
    (stored,), *_ = log.read_page(0)
    assert stored == {"kind": "call", "seq": 1, "site": {"file": "app/routes.py", "line": 3}}


def test_paging_yields_every_record_exactly_once_in_order():
    log = CallLog()
    expected = [_record(n) for n in range(25)]
    for record in expected:
        log.append(record)
    seen, final_cursor = _read_all(log, limit=7)
    assert seen == expected
    assert final_cursor == len(expected)


def test_a_page_says_whether_more_records_follow():
    log = CallLog()
    for n in range(5):
        log.append(_record(n))
    records, next_cursor, more, *_ = log.read_page(0, 2)
    assert [r["seq"] for r in records] == [0, 1] and next_cursor == 2 and more
    records, next_cursor, more, *_ = log.read_page(4, 2)
    assert [r["seq"] for r in records] == [4] and next_cursor == 5 and not more


def test_reading_at_the_end_returns_an_empty_page():
    log = CallLog()
    log.append(_record(0))
    assert log.read_page(1) == ([], 1, False, 0, False)
    assert CallLog().read_page(0) == ([], 0, False, 0, False)


def test_the_default_limit_is_the_page_max(monkeypatch):
    monkeypatch.setattr(calls_module, "PAGE_MAX", 4)
    log = CallLog()
    for n in range(10):
        log.append(_record(n))
    records, next_cursor, more, *_ = log.read_page(0)
    assert len(records) == 4 and next_cursor == 4 and more


@pytest.mark.parametrize(
    ("cursor", "limit"),
    [
        (-1, None),
        (True, None),
        ("0", None),
        (0.0, None),
        (None, None),
        (0, 0),
        (0, -1),
        (0, PAGE_MAX + 1),
        (0, True),
        (0, "5"),
        (3, None),
    ],
)
def test_a_bad_cursor_or_limit_raises_value_error(cursor, limit):
    log = CallLog()
    log.append(_record(0))
    log.append(_record(1))
    with pytest.raises(ValueError):
        log.read_page(cursor, limit)


def test_the_cap_drops_the_oldest_records_and_reports_truncation(monkeypatch):
    monkeypatch.setattr(calls_module, "CALLS_MAX_RECORDS", 3)
    log = CallLog()
    for n in range(1, 6):
        log.append(_record(n))
    assert len(log) == 3

    records, next_cursor, more, oldest, truncated = log.read_page(0)
    assert [r["seq"] for r in records] == [3, 4, 5]
    assert (next_cursor, more, oldest, truncated) == (5, False, 2, True)

    records, next_cursor, more, oldest, truncated = log.read_page(2)
    assert [r["seq"] for r in records] == [3, 4, 5]
    assert truncated is False

    records, *_ = log.read_page(3)
    assert [r["seq"] for r in records] == [4, 5]


def test_positions_keep_counting_across_many_drops(monkeypatch):
    monkeypatch.setattr(calls_module, "CALLS_MAX_RECORDS", 3)
    log = CallLog()
    for n in range(50):
        log.append(_record(n))
    records, next_cursor, more, oldest, truncated = log.read_page(48)
    assert [r["seq"] for r in records] == [48, 49]
    assert (next_cursor, more, oldest, truncated) == (50, False, 47, False)
    assert len(log) == 3


def test_a_patched_cap_of_zero_keeps_nothing_but_keeps_counting(monkeypatch):
    monkeypatch.setattr(calls_module, "CALLS_MAX_RECORDS", 0)
    log = CallLog()
    log.append(_record(0))
    log.append(_record(1))
    assert len(log) == 0
    assert log.read_page(0) == ([], 2, False, 2, True)


@pytest.mark.parametrize(
    "record",
    [
        {"seq": 1, "tags": {"a", "b"}},
        {"seq": 1, "t0": float("nan")},
        {"seq": 1, "t0": float("inf")},
        {"seq": 1, "value": object()},
        ["not", "a", "mapping"],
        None,
    ],
    ids=["set", "nan", "inf", "object", "list", "none"],
)
def test_an_unstorable_record_is_counted_and_never_raises(record):
    log = CallLog()
    log.append(record)
    assert log.record_errors == 1
    assert len(log) == 0
    log.append(_record(0))
    assert log.read_page(0)[0] == [_record(0)]


def test_reset_forgets_records_and_errors(monkeypatch):
    monkeypatch.setattr(calls_module, "CALLS_MAX_RECORDS", 2)
    log = CallLog()
    for n in range(4):
        log.append(_record(n))
    log.append({"bad": {1}})
    log.reset()
    assert len(log) == 0 and log.record_errors == 0
    assert log.read_page(0) == ([], 0, False, 0, False)
    log.append(_record(9))
    assert log.read_page(0) == ([_record(9)], 1, False, 0, False)


def test_concurrent_appends_are_all_kept_once():
    log = CallLog()
    workers, each = 4, 150

    def append_many(worker: int) -> None:
        for n in range(each):
            log.append({"worker": worker, "n": n})

    threads = [threading.Thread(target=append_many, args=(w,)) for w in range(workers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    seen, final_cursor = _read_all(log, limit=PAGE_MAX)
    assert final_cursor == workers * each
    assert sorted((r["worker"], r["n"]) for r in seen) == [
        (w, n) for w in range(workers) for n in range(each)
    ]


def test_nothing_is_printed(capsys):
    log = CallLog()
    log.append(_record(0))
    log.append({"bad": {1}})
    log.read_page(0)
    assert capsys.readouterr() == ("", "")


# --- bind_call / current_call --------------------------------------------------


def test_bind_call_nests_and_unwinds():
    assert current_call() is None
    with bind_call(5):
        assert current_call() == 5
        with bind_call(6):
            assert current_call() == 6
        assert current_call() == 5
    assert current_call() is None


def test_bind_call_unwinds_when_the_call_raises():
    with pytest.raises(RuntimeError), bind_call(5):
        raise RuntimeError("boom")
    assert current_call() is None


def test_bind_call_belongs_to_the_calling_thread():
    seen: dict[str, int | None] = {}

    def other_thread() -> None:
        seen["before"] = current_call()
        with bind_call(8):
            seen["inside"] = current_call()

    with bind_call(7):
        thread = threading.Thread(target=other_thread)
        thread.start()
        thread.join()
        assert current_call() == 7
    assert seen == {"before": None, "inside": 8}


# --- capture -------------------------------------------------------------------


def test_capture_outside_a_request_has_an_empty_ref_and_run():
    captured = capture()
    assert set(captured) == {"ref", "run", "site", "thread", "on_loop"}
    assert captured["ref"] == "" and captured["run"] == ""


def test_capture_reads_the_ref_and_run_of_the_current_request():
    ref_token = context._ref.set("ref-under-test")
    run_token = context._run.set("run-under-test")
    try:
        captured = capture()
    finally:
        context._ref.reset(ref_token)
        context._run.reset(run_token)
    assert captured["ref"] == "ref-under-test"
    assert captured["run"] == "run-under-test"


def test_capture_names_the_calling_thread():
    seen: dict = {}
    thread = threading.Thread(target=lambda: seen.update(capture()), name="recorder-test-thread")
    thread.start()
    thread.join()
    assert seen["thread"] == "recorder-test-thread"
    assert seen["on_loop"] is False


def test_capture_knows_whether_an_asyncio_loop_is_running():
    async def inside_a_loop() -> dict:
        return capture()

    assert capture()["on_loop"] is False
    assert asyncio.run(inside_a_loop())["on_loop"] is True


def test_capture_site_is_the_caller_as_a_repo_relative_path():
    captured, line = capture(), inspect.currentframe().f_lineno
    site = captured["site"]
    assert site == {
        "file": THIS_FILE,
        "line": line,
        "func": "test_capture_site_is_the_caller_as_a_repo_relative_path",
    }
    assert not site["file"].startswith("/")
    assert ":" not in site["file"]
    assert "site-packages" not in site["file"]


def _call_through(filename: str, target):
    """Call ``target`` from a frame whose code says it lives in ``filename``."""
    namespace = {"target": target}
    code = compile("def relay():\n    return target()\n", filename, "exec")
    exec(code, namespace)  # noqa: S102 - builds a two-line relay under a chosen file name
    return namespace["relay"]()


def _sdk_frame_filename() -> str:
    return os.path.join(os.path.dirname(signalgate.__file__), "relay_for_test.py")


@pytest.mark.parametrize(
    "where",
    ["kit", "client", "sdk", "installed_package"],
)
def test_site_skips_scaffolding_frames(where, tmp_path):
    filename = {
        "kit": str(REPO_ROOT / "app" / "kit" / "relay_for_test.py"),
        "client": str(REPO_ROOT / "app" / "signalgate_client.py"),
        "sdk": _sdk_frame_filename(),
        "installed_package": str(tmp_path / "site-packages" / "pkg" / "relay_for_test.py"),
    }[where]
    site = _call_through(filename, capture)["site"]
    assert site is not None
    assert site["file"] == THIS_FILE
    assert site["func"] == "_call_through"


def test_site_skips_several_scaffolding_frames_in_a_row(tmp_path):
    installed = str(tmp_path / "dist-packages" / "relay_for_test.py")
    kit = str(REPO_ROOT / "app" / "kit" / "relay_for_test.py")
    site = _call_through(kit, lambda: _call_through(installed, capture))["site"]
    assert site["file"] == THIS_FILE


@pytest.mark.parametrize("where", ["outside_repo", "not_a_file"])
def test_site_is_null_when_the_caller_is_outside_the_repo(where, tmp_path):
    filename = {
        "outside_repo": str(tmp_path / "elsewhere.py"),
        "not_a_file": "<string>",
    }[where]
    assert _call_through(filename, capture)["site"] is None


def test_site_counts_app_code_outside_the_kit_as_user_code():
    routes = str(REPO_ROOT / "app" / "routes_relay_for_test.py")
    site = _call_through(routes, capture)["site"]
    assert site == {"file": "app/routes_relay_for_test.py", "line": 2, "func": "relay"}


# --- j() -----------------------------------------------------------------------


class _Colour(enum.IntEnum):
    RED = 3


class _Text(str):
    pass


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("text", "text"),
        (7, 7),
        (2.5, 2.5),
        (True, True),
        (False, False),
        (None, None),
        ({"a": 1}, {"type": "dict"}),
        ([1], {"type": "list"}),
        ((1,), {"type": "tuple"}),
        (b"raw", {"type": "bytes"}),
        (float("inf"), {"type": "float"}),
        (float("nan"), {"type": "float"}),
        (object(), {"type": "object"}),
    ],
)
def test_j_keeps_json_scalars_and_names_everything_else(value, expected):
    result = j(value)
    assert result == expected
    assert type(result) is type(expected)
    json.dumps(result, allow_nan=False)


def test_j_cuts_long_strings_to_the_record_limit(monkeypatch):
    assert len(j("x" * (RECORD_STR_MAX + 10))) == RECORD_STR_MAX
    monkeypatch.setattr(calls_module, "RECORD_STR_MAX", 4)
    assert j("abcdefg") == "abcd"


def test_j_turns_subclasses_of_scalars_into_plain_values():
    assert j(_Colour.RED) == 3 and type(j(_Colour.RED)) is int
    assert j(_Text("hello")) == "hello" and type(j(_Text("hello"))) is str


# --- the run accessor ------------------------------------------------------------


def test_the_run_accessor_defaults_to_no_run():
    assert context.get_run() == ""
    assert context.RUN_HEADER == "x-playground-run"


@pytest.mark.parametrize("value", ["a", "run-1_A", "0123abcdef", "x" * 64])
def test_run_pattern_accepts_short_plain_ids(value):
    assert context.RUN_PATTERN.fullmatch(value)


@pytest.mark.parametrize("value", ["", "x" * 65, "has space", "a/b", "run.1", "ü", "a\n"])
def test_run_pattern_refuses_anything_else(value):
    assert context.RUN_PATTERN.fullmatch(value) is None
