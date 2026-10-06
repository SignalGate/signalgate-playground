"""Schema tests for the ``--ci`` JSON-lines renderer."""

from __future__ import annotations

import asyncio
import io
import json
import os
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from playground.copy.blocks import block_of
from playground.copy.en import text
from playground.machine.events import (
    AppFailed,
    AppReady,
    AwaitInput,
    DeliverProgress,
    Exit,
    RunProgress,
    Say,
    ShowLogo,
)
from playground.traffic.local_constants import DELIVER_REPORT_EVERY_S
from playground.tui.jsonl import JsonLinesRenderer
from tests.tui.fixtures import FIXTURE_EVENTS, ONLINE_FIXTURE_EVENTS

REPO_ROOT = Path(__file__).resolve().parents[2]

# How long the daemon-thread-close test polls for a background effect (an
# excepthook call, or stderr output) that a fixed regression would produce
# almost instantly; the bound is generous because nothing should ever fire.
_LOOP_CLOSE_POLL_S = 0.01
_LOOP_CLOSE_POLL_ATTEMPTS = 50

# The child process below never blocks on real work once its input arrives,
# so this only needs to be long enough to guard against a genuine hang: a
# regression that parks the read in the default executor, or makes the
# reader thread non-daemon, would otherwise wait minutes (or forever).
_CI_CHILD_EXIT_BOUND_S = 5.0

# An outer bound for in-process reads whose input is already all there; a
# regression that leaves a read waiting would otherwise hang the test.
_READ_BOUND_S = 5.0

# The subprocess child for the "--ci reader never uses the default executor,
# keeps stdin's write end open, and still exits promptly" test below. It:
#  1. reads one line the parent already wrote ("first"),
#  2. starts a second read, confirms it is genuinely pending, cancels it,
#  3. only then does the parent write "second" and "third" (confirmed by the
#     "cancelled" marker line below, so nothing can be written early),
#  4. reads them back, and lets ``asyncio.run`` return on its own.
# The parent process keeps the write end of the child's stdin open right up
# to (and past) the point the child exits, which is exactly the case this
# guards against: nothing here ever closes stdin to force an EOF shortcut.
_CI_CHILD_SCRIPT = """
import asyncio
import sys

from playground.tui.jsonl import JsonLinesRenderer


async def main() -> None:
    loop = asyncio.get_running_loop()

    def _forbidden(*_args, **_kwargs):
        raise AssertionError("the --ci reader must not use the default executor")

    loop.run_in_executor = _forbidden

    renderer = JsonLinesRenderer(sys.stdout, sys.stdin)

    first = await renderer.read_line()
    print("first:" + first, flush=True)

    pending = asyncio.ensure_future(renderer.read_line())
    await asyncio.sleep(0)
    assert not pending.done()
    pending.cancel()
    try:
        await pending
    except asyncio.CancelledError:
        pass
    print("cancelled", flush=True)

    second = await renderer.read_line()
    print("second:" + second, flush=True)
    third = await renderer.read_line()
    print("third:" + third, flush=True)


asyncio.run(main())
print("done", flush=True)
"""


def _lines(out: io.StringIO) -> list[dict]:
    return [json.loads(line) for line in out.getvalue().splitlines()]


def test_say_emits_v1_say_with_rendered_text():
    out = io.StringIO()
    renderer = JsonLinesRenderer(out, io.StringIO())
    renderer.show(Say("ch1.intro", {"total": 10_000}))
    (line,) = _lines(out)
    assert line == {
        "v": 1,
        "type": "say",
        "key": "ch1.intro",
        "text": text("ch1.intro", total=10_000),
        "block": "info",
    }
    assert line["text"].startswith("I'm sending 10,000 simulated visitors")


def test_a_say_names_its_block_and_its_text_has_no_bold_marks():
    out = io.StringIO()
    renderer = JsonLinesRenderer(out, io.StringIO())
    renderer.show(Say("ch1.close", {"conversion": 0.2, "attack_in_ten": 7}))
    renderer.show(Say("ch3.lesson"))
    renderer.show(Say("ch1.close", {"conversion": 0.2, "attack_in_ten": 7}, block="info"))
    close, lesson, overridden = _lines(out)
    assert close["block"] == "ask"
    assert close["text"].endswith("Ready to connect SignalGate? (yes/no)")
    assert "**" not in close["text"]
    assert lesson["block"] == "note"
    assert overridden["block"] == "info"


def test_the_task_block_is_its_whole_plain_text():
    out = io.StringIO()
    renderer = JsonLinesRenderer(out, io.StringIO())
    renderer.show(Say("ch3.task"))
    (line,) = _lines(out)
    assert line["block"] == "task"
    assert line["text"] == text("ch3.task")
    assert "**" not in line["text"]


def test_show_logo_emits_nothing():
    out = io.StringIO()
    renderer = JsonLinesRenderer(out, io.StringIO())
    renderer.show(ShowLogo())
    assert out.getvalue() == ""


def test_await_input_emits_expect():
    out = io.StringIO()
    renderer = JsonLinesRenderer(out, io.StringIO())
    renderer.show(AwaitInput("yes_no"))
    renderer.show(AwaitInput("continue"))
    assert _lines(out) == [
        {"v": 1, "type": "await", "expect": "yes_no"},
        {"v": 1, "type": "await", "expect": "continue"},
    ]


def test_exit_emits_code():
    out = io.StringIO()
    renderer = JsonLinesRenderer(out, io.StringIO())
    renderer.show(Exit(0))
    (line,) = _lines(out)
    assert line == {"v": 1, "type": "exit", "code": 0}


def test_app_ready_and_app_failed_emit_say_lines():
    out = io.StringIO()
    renderer = JsonLinesRenderer(out, io.StringIO())
    renderer.show(AppReady())
    renderer.show(AppFailed("app.failed"))
    lines = _lines(out)
    assert [line["type"] for line in lines] == ["say", "say"]
    assert lines[0]["key"] == "app.ready"
    assert lines[0]["block"] == block_of("app.ready") == "info"
    assert lines[1]["key"] == "app.failed"
    assert lines[1]["block"] == block_of("app.failed") == "result"


def test_progress_emits_counters():
    out = io.StringIO()
    renderer = JsonLinesRenderer(out, io.StringIO())
    renderer.progress(RunProgress(done=100, total=10_000, actions=95, successes=19))
    (line,) = _lines(out)
    assert line == {
        "v": 1,
        "type": "progress",
        "done": 100,
        "total": 10_000,
        "actions": 95,
        "successes": 19,
    }


def test_every_line_is_a_single_json_object_with_v1():
    out = io.StringIO()
    renderer = JsonLinesRenderer(out, io.StringIO())
    renderer.show(Say("reply.no", {}))
    renderer.show(AwaitInput("any"))
    renderer.progress(RunProgress(done=1, total=2, actions=1, successes=0))
    renderer.show(Exit(0))
    for line in _lines(out):
        assert line["v"] == 1
        assert line["type"] in {"say", "await", "progress", "exit"}


def test_every_fixture_say_emits_a_say_line_and_the_types_stay_the_same():
    """Every copy key goes through the same generic ``Say`` branch, so drive
    every fixture event through it and check each emitted line exactly, not
    just the few keys the tests above use.
    """
    out = io.StringIO()
    renderer = JsonLinesRenderer(out, io.StringIO())
    for event in FIXTURE_EVENTS:
        renderer.show(event)
    lines = _lines(out)

    assert lines, "no lines emitted for the fixture events"
    assert {line["type"] for line in lines} <= {"say", "await", "progress", "exit"}

    say_events = [event for event in FIXTURE_EVENTS if isinstance(event, Say)]
    assert say_events, "the fixtures have no Say events to check"
    for event in say_events:
        expected = {
            "v": 1,
            "type": "say",
            "key": event.key,
            "text": text(event.key, **dict(event.params)),
            "block": block_of(event.key),
        }
        assert expected in lines, f"missing or malformed say line for {event.key!r}"


def test_every_online_fixture_say_has_a_block_and_no_bold_marks():
    out = io.StringIO()
    renderer = JsonLinesRenderer(out, io.StringIO())
    for event in ONLINE_FIXTURE_EVENTS:
        renderer.show(event)
    lines = _lines(out)
    assert len(lines) == len(ONLINE_FIXTURE_EVENTS)
    for event, line in zip(ONLINE_FIXTURE_EVENTS, lines):
        assert line["key"] == event.key
        assert line["block"] == block_of(event.key)
        assert "**" not in line["text"], event.key


def test_read_line_returns_input_line():
    # An in-memory reader with no file descriptor at all.
    renderer = JsonLinesRenderer(io.StringIO(), io.StringIO("yes\n"))
    assert asyncio.run(renderer.read_line()) == "yes"


def test_read_line_reads_a_regular_file_in_order_then_quits_at_its_end(tmp_path):
    """``--ci`` input can be a regular file (``< answers.txt``), not only a
    pipe or a terminal: its lines come back in order, and its end answers
    ``/quit``, every time it is asked.
    """
    answers = ["yes", "no", "/start"]
    path = tmp_path / "answers.txt"
    path.write_text("".join(f"{answer}\n" for answer in answers))

    async def scenario(inp: io.TextIOBase) -> list[str]:
        renderer = JsonLinesRenderer(io.StringIO(), inp)
        return [await renderer.read_line() for _ in range(len(answers) + 2)]

    with path.open() as inp:
        assert stat.S_ISREG(os.fstat(inp.fileno()).st_mode)
        read_back = asyncio.run(asyncio.wait_for(scenario(inp), timeout=_READ_BOUND_S))
    assert read_back == [*answers, "/quit", "/quit"]


def test_read_line_returns_quit_on_eof():
    renderer = JsonLinesRenderer(io.StringIO(), io.StringIO(""))
    assert asyncio.run(renderer.read_line()) == "/quit"


def test_read_line_keeps_answering_quit_after_eof():
    renderer = JsonLinesRenderer(io.StringIO(), io.StringIO(""))
    assert asyncio.run(renderer.read_line()) == "/quit"
    assert asyncio.run(renderer.read_line()) == "/quit"


class _RaisingReadable:
    def readline(self) -> str:
        raise OSError(5, "Input/output error")


def test_read_line_treats_a_read_error_as_eof():
    renderer = JsonLinesRenderer(io.StringIO(), _RaisingReadable())
    assert asyncio.run(renderer.read_line()) == "/quit"


class _BadlyDecodedReadable:
    """Stands in for stdin hitting bytes that don't decode as text."""

    def readline(self) -> str:
        raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte")


def test_read_line_treats_a_decode_error_as_eof_not_a_hang():
    """A non-``OSError`` read failure (bad bytes on stdin) must still answer
    ``/quit`` instead of leaving ``read_line`` waiting forever: a broken
    reader means quit, never a hang or a traceback.
    """
    renderer = JsonLinesRenderer(io.StringIO(), _BadlyDecodedReadable())
    assert asyncio.run(renderer.read_line()) == "/quit"


def test_ci_reader_thread_is_a_daemon_and_never_uses_the_default_executor():
    """A non-daemon reader thread blocked on an open pipe would keep the
    whole interpreter alive at shutdown even after every coroutine has
    finished; a thread parked in the default executor would keep
    ``asyncio.run`` itself waiting. Neither must ever happen.
    """
    read_fd, write_fd = os.pipe()
    inp = os.fdopen(read_fd, "r")
    writer = os.fdopen(write_fd, "w")

    async def scenario() -> str:
        loop = asyncio.get_running_loop()

        def _forbidden(*_args: object, **_kwargs: object) -> None:
            raise AssertionError("the --ci reader must not use the default executor")

        loop.run_in_executor = _forbidden  # type: ignore[method-assign]

        renderer = JsonLinesRenderer(io.StringIO(), inp)
        return await renderer.read_line()

    try:
        writer.write("first\n")
        writer.flush()
        assert asyncio.run(scenario()) == "first"

        readers = [
            thread for thread in threading.enumerate() if thread.name == "jsonl-stdin-reader"
        ]
        assert len(readers) == 1
        assert readers[0].daemon is True
    finally:
        writer.close()
        inp.close()


def test_ci_reader_exits_promptly_as_a_subprocess_with_stdin_held_open():
    """The real regression this guards against: a pending ``read_line`` is
    cancelled, the write end of stdin stays open (never closed to force an
    EOF shortcut), and lines written before and after the cancellation must
    each come back exactly once, in order, while ``asyncio.run`` and then
    the process itself still exit within a short bound.
    """
    proc = subprocess.Popen(
        [sys.executable, "-c", _CI_CHILD_SCRIPT],
        cwd=REPO_ROOT,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )
    try:
        assert proc.stdin is not None
        assert proc.stdout is not None

        proc.stdin.write("first\n")
        proc.stdin.flush()
        assert proc.stdout.readline().strip() == "first:first"
        # The child only cancels its pending read, and only reports having
        # done so, before any of "second"/"third" below are written -- so
        # neither line can possibly be the one that got cancelled.
        assert proc.stdout.readline().strip() == "cancelled"

        started = time.monotonic()
        proc.stdin.write("second\n")
        proc.stdin.write("third\n")
        proc.stdin.flush()

        assert proc.stdout.readline().strip() == "second:second"
        assert proc.stdout.readline().strip() == "third:third"
        assert proc.stdout.readline().strip() == "done"
        elapsed = time.monotonic() - started
        assert elapsed <= _CI_CHILD_EXIT_BOUND_S

        # The write end of the child's stdin is still open right here.
        returncode = proc.wait(timeout=_CI_CHILD_EXIT_BOUND_S)
        assert returncode == 0, proc.stderr.read()
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        if proc.stdin is not None:
            proc.stdin.close()


def test_a_line_after_the_loop_closes_never_reaches_threading_excepthook(monkeypatch):
    """Once ``asyncio.run`` has closed the loop, a line arriving afterwards
    on the still-alive daemon thread must not surface as an unhandled
    ``RuntimeError('Event loop is closed')``: a reader failure means quit,
    never a traceback, even though nobody is left to read the line.
    """
    excepthook_calls: list[object] = []
    monkeypatch.setattr(threading, "excepthook", excepthook_calls.append)

    read_fd, write_fd = os.pipe()
    inp = os.fdopen(read_fd, "r")
    writer = os.fdopen(write_fd, "w")
    try:
        writer.write("first\n")
        writer.flush()
        renderer = JsonLinesRenderer(io.StringIO(), inp)
        assert asyncio.run(renderer.read_line()) == "first"  # starts, then closes, the loop

        # The daemon thread is still alive, blocked on the next readline().
        writer.write("second\n")
        writer.flush()

        for _ in range(_LOOP_CLOSE_POLL_ATTEMPTS):
            time.sleep(_LOOP_CLOSE_POLL_S)
            if excepthook_calls:
                break

        assert excepthook_calls == []
    finally:
        writer.close()
        inp.close()


class _BrokenWritable:
    def write(self, data: str) -> None:
        raise OSError(5, "Input/output error")

    def flush(self) -> None:
        pass


def test_show_progress_and_lifecycle_swallow_oserror_from_a_broken_file():
    renderer = JsonLinesRenderer(_BrokenWritable(), io.StringIO())
    renderer.show(Say("reply.no", {}))  # must not raise
    renderer.run_started(10)  # must not raise
    renderer.progress(RunProgress(done=1, total=2, actions=1, successes=0))  # must not raise
    renderer.run_ended()  # must not raise


def test_unknown_event_type_raises():
    renderer = JsonLinesRenderer(io.StringIO(), io.StringIO())
    with pytest.raises(TypeError):
        renderer.show(object())  # type: ignore[arg-type]


def test_a_run_through_signalgate_adds_its_log_counters_to_progress():
    out = io.StringIO()
    renderer = JsonLinesRenderer(out, io.StringIO())
    renderer.run_started(2_000, title="panel.learn.title")
    renderer.progress(
        RunProgress(
            done=10, total=2_000, actions=10, successes=2, posted=12, delivered=11, accepted=11
        )
    )
    (line,) = _lines(out)
    assert line == {
        "v": 1,
        "type": "progress",
        "done": 10,
        "total": 2_000,
        "actions": 10,
        "successes": 2,
        "posted": 12,
        "delivered": 11,
        "accepted": 11,
    }


class _Clock:
    """A clock the test moves by hand."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def _deliver_records(out: io.StringIO) -> list[dict]:
    return [line for line in _lines(out) if line.get("phase") == "deliver"]


def test_a_drain_starts_with_its_first_line_as_a_say():
    out = io.StringIO()
    renderer = JsonLinesRenderer(out, io.StringIO())
    renderer.drain_begin(Say("drain.waiting", {"remaining": 922}))
    renderer.drain_begin(Say("drain.waiting_quit", {"remaining": 12}), inline=False)
    first, second = _lines(out)
    assert first == {
        "v": 1,
        "type": "say",
        "key": "drain.waiting",
        "text": text("drain.waiting", remaining=922),
        "block": "info",
    }
    assert second["key"] == "drain.waiting_quit"


def test_drain_progress_is_a_deliver_record_at_most_once_per_interval():
    clock = _Clock()
    out = io.StringIO()
    renderer = JsonLinesRenderer(out, io.StringIO(), clock=clock)
    renderer.drain_begin(Say("drain.waiting", {"remaining": 900}))
    for at, done in ((0.0, 0), (1.0, 100), (5.0, 500)):
        clock.now = at
        renderer.drain_progress(DeliverProgress(done, 900, 30.0))
    assert DELIVER_REPORT_EVERY_S == 4.0  # the times above straddle one interval
    assert _deliver_records(out) == [
        {"v": 1, "type": "progress", "phase": "deliver", "done": 0, "total": 900},
        {"v": 1, "type": "progress", "phase": "deliver", "done": 500, "total": 900},
    ]


def test_drain_end_sends_the_count_held_back():
    clock = _Clock()
    out = io.StringIO()
    renderer = JsonLinesRenderer(out, io.StringIO(), clock=clock)
    renderer.drain_begin(Say("drain.waiting", {"remaining": 900}))
    renderer.drain_progress(DeliverProgress(0, 900))
    clock.now = 1.0
    renderer.drain_progress(DeliverProgress(900, 900))
    assert len(_deliver_records(out)) == 1
    renderer.drain_end()
    assert [(r["done"], r["total"]) for r in _deliver_records(out)] == [(0, 900), (900, 900)]
    renderer.drain_end()  # nothing held back any more: nothing new
    assert len(_deliver_records(out)) == 2


def test_drain_end_after_an_unthrottled_count_adds_nothing():
    clock = _Clock()
    out = io.StringIO()
    renderer = JsonLinesRenderer(out, io.StringIO(), clock=clock)
    renderer.drain_progress(DeliverProgress(3, 10))
    renderer.drain_end()
    assert [(r["done"], r["total"]) for r in _deliver_records(out)] == [(3, 10)]


def test_a_new_drain_reports_its_first_count_at_once():
    clock = _Clock()
    out = io.StringIO()
    renderer = JsonLinesRenderer(out, io.StringIO(), clock=clock)
    renderer.drain_begin(Say("drain.waiting", {"remaining": 10}))
    renderer.drain_progress(DeliverProgress(0, 10))
    renderer.drain_end()
    clock.now = 0.5
    renderer.drain_begin(Say("drain.waiting_quit", {"remaining": 0}))
    renderer.drain_progress(DeliverProgress(0, 0))
    assert [(r["done"], r["total"]) for r in _deliver_records(out)] == [(0, 10), (0, 0)]


def test_run_progress_records_have_no_phase():
    out = io.StringIO()
    renderer = JsonLinesRenderer(out, io.StringIO())
    renderer.progress(RunProgress(10, 100, 10, 2, posted=12, delivered=5, accepted=5, eta_s=4.0))
    (line,) = _lines(out)
    assert "phase" not in line
    assert "eta_s" not in line


def test_drain_methods_swallow_oserror_from_a_broken_file():
    renderer = JsonLinesRenderer(_BrokenWritable(), io.StringIO())
    renderer.drain_begin(Say("drain.waiting", {"remaining": 3}))  # must not raise
    renderer.drain_progress(DeliverProgress(1, 3))  # must not raise
    renderer.drain_end()  # must not raise


def test_a_wait_for_signalgate_is_a_wait_line_with_the_dashboards_words():
    out = io.StringIO()
    renderer = JsonLinesRenderer(out, io.StringIO())
    renderer.wait(Say("wait.dry_running", {"stage": 2, "stages_total": 3}))
    renderer.wait_ended()
    (line,) = _lines(out)
    assert line == {
        "v": 1,
        "type": "wait",
        "key": "wait.dry_running",
        "text": "dry run: stage 2 of 3",
    }
