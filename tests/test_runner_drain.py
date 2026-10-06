"""The runner's side of the wait for the app's queued log events, and its clock.

The session runs over the fakes of ``tests/test_runner_reload.py`` (the
sample app, the watcher, the offline run and the input line), with a
drain that reports a shrinking backlog and a renderer that records the
three ``drain_*`` calls. Checked: which line a wait begins with and whether
it may be redrawn in place, that the counts only grow and end whole, the
line that ends each kind of wait, and the fallback for a renderer that has
no ``drain_*`` methods.

The same session also carries the active console time: every result the
machine gets after ``/start`` is stamped with it, and a resumed session
carries on from the time its saved progress holds.
"""

from __future__ import annotations

import asyncio
import itertools
import signal
import time
from typing import Any

import pytest

from playground import runner
from playground.api import key as api_key
from playground.machine.events import (
    AppReady,
    DeliverProgress,
    RunFinished,
    Say,
    StepDone,
)
from playground.machine.machine import Machine
from playground.progress import eta_seconds
from playground.supervisor.drain import DrainResult
from playground.supervisor.launcher import StopResult
from playground.traffic.local_constants import DRAIN_MAX_S
from tests.test_runner_reload import (
    POLL_S,
    VISITORS,
    WITHIN_S,
    FakeApp,
    Recorder,
    Session,
    _maybe_await,
    _run,
    until,
)

# What the scripted drains report, one count per report, oldest first.
BACKLOG = [922, 800, 640, 480, 300, 120]


class DrainRecorder(Recorder):
    """A renderer that also records the wait for the app's queued log events."""

    def __init__(self) -> None:
        super().__init__()
        self.calls: list[tuple[str, Any]] = []

    def show(self, event: Any) -> None:
        super().show(event)
        if isinstance(event, Say):
            self.calls.append(("say", event.key))

    def drain_begin(self, first: Say, *, inline: bool = True) -> None:
        self.calls.append(("begin", (first, inline)))

    def drain_progress(self, event: DeliverProgress) -> None:
        self.calls.append(("progress", event))

    def drain_end(self) -> None:
        self.calls.append(("end", None))

    def named(self, name: str) -> list[Any]:
        return [value for call, value in self.calls if call == name]

    def progress_events(self) -> list[DeliverProgress]:
        return self.named("progress")

    def after(self, name: str) -> list[tuple[str, Any]]:
        """Every call after the last ``name`` call."""
        index = max(i for i, (call, _) in enumerate(self.calls) if call == name)
        return self.calls[index + 1 :]


class ScriptedDrain:
    """Replaces the runner's drain: reports each count in turn, then ends.

    ``outcome`` is how it ends once the counts are used up; ``hold`` makes
    it wait there instead, until released or cancelled (a skip).
    """

    def __init__(self, counts: list[int], *, outcome: str = "drained", hold: bool = False) -> None:
        self.counts = list(counts)
        self.outcome = outcome
        self.hold = hold
        self.release = asyncio.Event()
        self.holding = asyncio.Event()
        self.results: list[DrainResult] = []

    async def __call__(self, control, *, cancel=None, on_wait=None) -> DrainResult:
        last = 0
        for remaining in self.counts:
            if cancel is not None and cancel.is_set():
                break
            last = remaining
            if on_wait is not None:
                await _maybe_await(on_wait(remaining))
            await asyncio.sleep(POLL_S)
        if self.hold:
            self.holding.set()
            waiters = {asyncio.ensure_future(self.release.wait())}
            if cancel is not None:
                waiters.add(asyncio.ensure_future(cancel.wait()))
            _done, pending = await asyncio.wait(waiters, return_when=asyncio.FIRST_COMPLETED)
            for waiter in pending:
                waiter.cancel()
        if cancel is not None and cancel.is_set():
            result = DrainResult("skipped", last, 0.0)
        elif self.outcome == "drained":
            result = DrainResult("drained", 0, 0.0)
        else:
            result = DrainResult(self.outcome, last, 0.0)  # type: ignore[arg-type]
        self.results.append(result)
        return result


def _session(tmp_path, monkeypatch, drain: ScriptedDrain | None = None, **kwargs) -> Session:
    monkeypatch.delenv(api_key.KEY_NAME, raising=False)
    session = Session(tmp_path, monkeypatch, **kwargs)
    session.renderer = DrainRecorder()
    session.renderer.app = session.app
    if drain is not None:
        monkeypatch.setattr(runner, "drain", drain)
    return session


def _check_counts(events: list[DeliverProgress]) -> None:
    """Counts only ever grow, and what is sent never passes what was queued."""
    assert events
    for before, after in itertools.pairwise(events):
        assert after.done >= before.done and after.total >= before.total
    assert all(0 <= event.done <= event.total for event in events)


# -- the wait after a run ----------------------------------------------------------------


def test_a_run_drain_begins_once_counts_up_and_ends_whole(tmp_path, monkeypatch):
    drain = ScriptedDrain(BACKLOG)

    async def scenario() -> None:
        session = _session(tmp_path, monkeypatch, drain)
        session.start()
        await session.opened()
        session.keyboard.type("/start")
        await session.said("ch1.close")

        renderer: DrainRecorder = session.renderer  # type: ignore[assignment]
        ((first, inline),) = renderer.named("begin")
        assert first == Say("drain.waiting", {"remaining": BACKLOG[0]})
        assert inline is True  # no input line is open during a run's wait

        events = renderer.progress_events()
        _check_counts(events)
        assert events[0] == DeliverProgress(0, BACKLOG[0], None)
        reported = events[: len(BACKLOG)]
        assert [event.done for event in reported] == [BACKLOG[0] - n for n in BACKLOG]
        # Once two counts are in, the rate is measured and an estimate given.
        assert all(event.eta_s is not None and event.eta_s > 0 for event in reported[1:])

        # Drained: one last whole count, the line ends, then its last line.
        assert renderer.after("progress")[:2] == [("end", None), ("say", "drain.done")]
        assert events[-1] == DeliverProgress(BACKLOG[0], BACKLOG[0], None)
        assert len(renderer.named("end")) == 1
        keys = session.renderer.say_keys()
        assert keys.index("drain.done") < keys.index("ch1.close")
        assert "drain.waiting" not in keys  # it went to drain_begin, not show

        session.keyboard.type(None)
        assert await session.finish() == 0

    _run(scenario)


def test_a_first_ctrl_c_skips_the_wait_and_the_line_says_so(tmp_path, monkeypatch):
    drain = ScriptedDrain(BACKLOG[:2], hold=True)

    async def scenario() -> None:
        session = _session(tmp_path, monkeypatch, drain)
        session.start()
        await session.opened()
        session.keyboard.type("/start")
        renderer: DrainRecorder = session.renderer  # type: ignore[assignment]
        await until(lambda: len(renderer.progress_events()) == 2)
        signal.raise_signal(signal.SIGINT)
        await session.said("drain.skipped")
        await session.said("ch1.close")  # the session goes on

        assert renderer.after("progress")[:2] == [("end", None), ("say", "drain.skipped")]
        assert "drain.done" not in renderer.say_keys()
        # Skipped, so the count is never made whole.
        assert renderer.progress_events()[-1].done < renderer.progress_events()[-1].total
        _check_counts(renderer.progress_events())

        session.keyboard.type(None)
        assert await session.finish() == 0

    _run(scenario)


@pytest.mark.parametrize("outcome", ["stalled", "capped"])
def test_a_wait_that_gives_up_ends_the_line_then_says_so(tmp_path, monkeypatch, outcome):
    drain = ScriptedDrain(BACKLOG[:3], outcome=outcome)

    async def scenario() -> None:
        session = _session(tmp_path, monkeypatch, drain)
        session.start()
        await session.opened()
        session.keyboard.type("/start")
        await session.said("ch1.close")

        renderer: DrainRecorder = session.renderer  # type: ignore[assignment]
        assert renderer.after("progress")[:2] == [("end", None), ("say", "drain.gave_up")]
        assert "drain.done" not in renderer.say_keys()
        assert len(renderer.named("end")) == 1

        session.keyboard.type(None)
        assert await session.finish() == 0

    _run(scenario)


def test_no_backlog_means_no_line_at_all(tmp_path, monkeypatch):
    drain = ScriptedDrain([])

    async def scenario() -> None:
        session = _session(tmp_path, monkeypatch, drain)
        session.start()
        await session.opened()
        session.keyboard.type("/start")
        await session.said("ch1.close")

        renderer: DrainRecorder = session.renderer  # type: ignore[assignment]
        assert renderer.named("begin") == renderer.named("end") == []
        assert "drain.done" not in renderer.say_keys()

        session.keyboard.type(None)
        assert await session.finish() == 0

    _run(scenario)


def test_a_renderer_without_drain_methods_shows_the_first_line_only(tmp_path, monkeypatch):
    drain = ScriptedDrain(BACKLOG)

    async def scenario() -> None:
        session = Session(tmp_path, monkeypatch)
        monkeypatch.setattr(runner, "drain", drain)
        session.start()
        await session.opened()
        session.keyboard.type("/start")
        await session.said("ch1.close")

        says = session.renderer.says()
        waiting = [say for say in says if say.key.startswith("drain.")]
        assert waiting == [
            Say("drain.waiting", {"remaining": BACKLOG[0]}),
            Say("drain.done"),
        ]

        session.keyboard.type(None)
        assert await session.finish() == 0

    _run(scenario)


# -- the wait before a reload, and on the way out ------------------------------------------


def test_a_reload_wait_with_the_prompt_open_is_never_redrawn_in_place(tmp_path, monkeypatch):
    drain = ScriptedDrain(BACKLOG[:3])

    async def scenario() -> None:
        session = _session(tmp_path, monkeypatch, drain)
        session.start()
        await session.opened()

        session.watcher.change()
        # At the opening a reload prints no notice: follow the restart itself.
        await session.restarted()

        renderer: DrainRecorder = session.renderer  # type: ignore[assignment]
        ((first, inline),) = renderer.named("begin")
        assert first == Say("drain.waiting_quit", {"remaining": BACKLOG[0]})
        assert inline is False
        # The wait ended, with its last line, before the restart was asked for.
        assert renderer.restarts_when("drain.done") == 0
        _check_counts(renderer.progress_events())

        session.keyboard.type(None)
        assert await session.finish() == 0

    _run(scenario)


def test_a_reload_wait_without_a_prompt_may_be_redrawn(tmp_path, monkeypatch):
    drain = ScriptedDrain(BACKLOG[:2])

    async def scenario() -> None:
        session = _session(tmp_path, monkeypatch, drain, interactive=False)
        session.start()
        await session.opened()

        session.watcher.change()
        await session.restarted()
        renderer: DrainRecorder = session.renderer  # type: ignore[assignment]
        ((first, inline),) = renderer.named("begin")
        assert (first.key, inline) == ("drain.waiting", True)

        session.keyboard.type("/quit")
        assert await session.finish() == 0

    _run(scenario)


class DrainingApp(FakeApp):
    """The app on the way out: its stop reports a shrinking backlog, then drains."""

    async def stop(self, *, drain=False, cancel=None, on_drain=None):
        self.calls.append(("stop", drain))
        if not self.alive:
            return None
        result = None
        if drain and on_drain is not None:
            for remaining in BACKLOG:
                await _maybe_await(on_drain(remaining))
                await asyncio.sleep(POLL_S)
            result = DrainResult("drained", 0, 0.0)
        self.alive = False
        return StopResult(drain=result, shutdown=None, killed=False, returncode=0)


def test_the_wait_on_the_way_out_ends_with_all_logs_sent(tmp_path, monkeypatch):
    async def scenario() -> None:
        session = _session(tmp_path, monkeypatch)
        session.app = DrainingApp()
        session.start()
        await session.opened()
        session.keyboard.type("/quit")
        assert await session.finish() == 0

        renderer: DrainRecorder = session.renderer  # type: ignore[assignment]
        ((first, inline),) = renderer.named("begin")
        assert first == Say("drain.waiting_quit", {"remaining": BACKLOG[0]})
        assert inline is True  # the input line is closed by then
        _check_counts(renderer.progress_events())
        assert renderer.progress_events()[-1] == DeliverProgress(BACKLOG[0], BACKLOG[0], None)
        keys = renderer.say_keys()
        assert keys[-2:] == ["console.bye", "drain.done"]

    _run(scenario)


def test_a_quiet_session_never_begins_a_line_it_cannot_take_down(tmp_path, monkeypatch):
    drain = ScriptedDrain(BACKLOG[:2], hold=True)

    async def scenario() -> None:
        session = _session(tmp_path, monkeypatch, drain)
        session.start()
        await session.opened()
        session.renderer.broken = True  # the terminal hangs up: the renderer goes quiet
        session.keyboard.type("/start")
        await asyncio.wait_for(drain.holding.wait(), WITHIN_S)
        signal.raise_signal(signal.SIGINT)  # skips the wait
        await until(lambda: session.keyboard.reads >= 2)  # back at a question
        session.keyboard.type(None)
        assert await session.finish() == 0
        renderer: DrainRecorder = session.renderer  # type: ignore[assignment]
        assert renderer.named("begin") == renderer.named("end") == []
        assert drain.results[0].outcome == "skipped"

    _run(scenario)


# -- how the counts are kept -----------------------------------------------------------


def test_counts_only_grow_even_when_new_events_join_the_queue():
    shown = runner._Drain()
    assert shown.update(0.0, 10) == DeliverProgress(0, 10, None)
    grew = shown.update(1.0, 12)  # two more joined, none sent
    assert (grew.done, grew.total, grew.eta_s) == (0, 12, None)
    fell = shown.update(2.0, 5)
    assert (fell.done, fell.total) == (7, 12)
    assert fell.eta_s is not None and fell.eta_s > 0
    rose = shown.update(3.0, 9)  # four more joined while it was sending
    assert (rose.done, rose.total) == (7, 16)


def test_the_estimate_is_what_is_left_at_the_rate_it_fell():
    shown = runner._Drain()
    assert shown.update(100.0, 600).eta_s is None  # one count: no rate yet
    fell = shown.update(101.0, 300)  # 300 sent in one second
    assert fell == DeliverProgress(300, 600, eta_seconds(300, 300.0))
    assert fell.eta_s == pytest.approx(1.0)


def test_the_estimate_never_says_more_than_the_wait_has_left():
    shown = runner._Drain(began=0.0)
    shown.update(0.0, 1_000)
    slow = shown.update(1.0, 990)  # 10 a second: 99 s to go, more than is left
    assert slow.eta_s == pytest.approx(DRAIN_MAX_S - 1.0)
    assert shown.update(DRAIN_MAX_S - 0.5, 600).eta_s == pytest.approx(0.5)
    assert shown.update(DRAIN_MAX_S + 1.0, 500).eta_s is None  # the wait is over


def test_the_estimate_counts_from_the_first_count_when_no_start_is_given():
    shown = runner._Drain()
    shown.update(500.0, 1_000)
    assert shown.update(501.0, 990).eta_s == pytest.approx(DRAIN_MAX_S - 1.0)


def test_an_empty_count_is_zero_of_zero_with_no_estimate():
    assert runner._Drain().update(0.0, 0) == DeliverProgress(0, 0, None)


# -- the active console time -------------------------------------------------------------


class FedMachine(Machine):
    """The real machine, keeping every result the runner feeds it."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.fed: list[Any] = []

    def on_event(self, event: Any) -> list[Any]:
        self.fed.append(event)
        return super().on_event(event)

    def stamps(self) -> list[tuple[str, float | None]]:
        return [
            (type(event).__name__, event.elapsed_s)
            for event in self.fed
            if isinstance(event, (StepDone, RunFinished))
        ]


def test_every_result_after_start_carries_the_time_since_start(tmp_path, monkeypatch):
    async def scenario() -> None:
        session = _session(tmp_path, monkeypatch)
        session.machine = FedMachine(offline_total=VISITORS)
        session.start()
        await session.opened()
        before_start = time.monotonic()
        session.keyboard.type("/start")
        await session.said("ch1.close")
        session.keyboard.type("yes")
        await session.said("ch2.key.ask")
        for times in (1, 2):
            await asyncio.sleep(POLL_S * 5)
            session.keyboard.type("yes")  # no key yet: the check says so
            await session.said("ch2.key.missing", times=times)
        after = time.monotonic()
        session.keyboard.type(None)
        assert await session.finish() == 0

        stamps = session.machine.stamps()
        assert [name for name, _ in stamps] == ["RunFinished", "StepDone", "StepDone"]
        times = [stamp for _, stamp in stamps]
        assert all(stamp is not None for stamp in times)
        assert 0 <= times[0] < times[1] < times[2] <= after - before_start
        assert times[2] - times[1] >= POLL_S * 5

    _run(scenario)


def test_results_before_any_start_carry_no_time(tmp_path, monkeypatch):
    async def scenario() -> None:
        session = _session(tmp_path, monkeypatch)
        session.machine = FedMachine(offline_total=VISITORS)
        session.start()
        await session.opened()
        session.keyboard.type(None)
        assert await session.finish() == 0
        assert session.machine.stamps() == []
        assert session.machine.fed  # it was fed results (the app's start)

    _run(scenario)


def _saved_at_the_key_question() -> dict[str, Any]:
    machine = Machine(offline_total=VISITORS)
    machine.start()
    machine.on_event(AppReady())
    machine.handle("/start")
    machine.on_event(RunFinished(VISITORS, VISITORS, 2, 8, 2))
    machine.handle("yes")
    return machine.to_dict()


def test_a_resumed_session_carries_on_from_its_saved_time(tmp_path, monkeypatch):
    carried = 100.0
    # Until the machine keeps the time itself, a stand-in says what was saved.
    monkeypatch.setattr(Machine, "elapsed_s", property(lambda self: carried))
    held_s = POLL_S * 20

    async def scenario() -> None:
        session = _session(tmp_path, monkeypatch)
        session.machine = FedMachine(offline_total=VISITORS, saved=_saved_at_the_key_question())
        before = time.monotonic()
        session.start()
        await session.said("continue.offer")
        await until(lambda: session.keyboard.reads >= 1)
        await asyncio.sleep(held_s)  # time at the prompt before /continue counts too
        session.keyboard.type("/continue")
        await session.said("ch2.key.ask")
        session.keyboard.type("yes")
        await session.said("ch2.key.missing")
        await asyncio.sleep(POLL_S * 5)
        session.keyboard.type("yes")
        await session.said("ch2.key.missing", times=2)
        after = time.monotonic()
        session.keyboard.type(None)
        assert await session.finish() == 0

        stamps = [stamp for name, stamp in session.machine.stamps() if name == "StepDone"]
        assert len(stamps) == 2
        assert all(stamp is not None for stamp in stamps)
        # Counted from when this console started, on top of the saved time.
        assert carried + held_s <= stamps[0] < stamps[1] <= carried + (after - before)

    _run(scenario)
