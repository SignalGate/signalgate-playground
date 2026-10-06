"""Unit tests for the runner's session policy, with a fake app and watcher.

The runner is driven through its public ``run()`` with the real machine and
fakes for everything with a process or a clock behind it: the sample app
(``FakeApp``), the file watcher (``FakeWatcher``), the offline run, the drain
and the input line. Each test scripts one situation (a save while the
console waits, an answer typed during a restart, a crash, a Ctrl-C at a
given moment) and checks what the console says and what it asks the app to
do. A reload outside the steps where the user edits the code says nothing,
so these tests watch the fake app's restarts rather than a reload line. The
real processes and a real terminal are covered under ``tests/e2e``.
"""

from __future__ import annotations

import asyncio
import contextlib
import errno
import inspect
import json
import signal
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from typing import Any

import pytest

from playground import runner
from playground.machine.events import AwaitInput, Exit, Say
from playground.machine.machine import Machine
from playground.supervisor.drain import DrainResult
from playground.supervisor.launcher import AppStartError, AppUnavailable, Boot, StopResult
from playground.traffic.driver import RunResult
from playground.traffic.local_constants import APP_HEALTH_POLL_S

# How long a scripted step may take before a test gives up on it, and how
# often it looks.
WITHIN_S = APP_HEALTH_POLL_S * 100
POLL_S = APP_HEALTH_POLL_S / 10

VISITORS = 10
FINISHED = RunResult(
    total=VISITORS, done=VISITORS, actions=VISITORS, successes=2, attack=8, real=2, stopped=False
)


async def _maybe_await(value: Any) -> None:
    if inspect.isawaitable(value):
        await value


async def until(predicate: Callable[[], bool], within: float = WITHIN_S) -> None:
    """Wait until ``predicate()`` holds, yielding to the session meanwhile."""
    deadline = asyncio.get_running_loop().time() + within
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("timed out waiting for the session")
        await asyncio.sleep(POLL_S)


# -- fakes ----------------------------------------------------------------------------


class FakeHold:
    def __init__(self, app: FakeApp, boot_id: str) -> None:
        self._app = app
        self.boot_id = boot_id
        self.invalidated = asyncio.Event()

    async def intact(self) -> bool:
        app_boot = self._app.boot
        return (
            not self.invalidated.is_set()
            and self._app.alive
            and app_boot is not None
            and app_boot.boot_id == self.boot_id
        )


class FakeApp:
    """Stands in for ``AppProcess``: records what it was asked to do."""

    def __init__(self) -> None:
        self.alive = False
        self.boot: Boot | None = None
        self.number = 0
        self.control: object | None = object()
        self.calls: list[tuple[str, Any]] = []
        self.start_failures: list[AppStartError] = []
        self.hold_error: Exception | None = None
        self.restarting = False
        self.concurrent_restarts = 0
        self.most_concurrent_restarts = 0
        # Run inside restart(), right after before_spawn(): a test's hook.
        self.during_restart: Callable[[int], Awaitable[None] | None] | None = None
        # The exit drain: (queued count, released) when the stop should wait.
        self.exit_backlog: int = 0
        self.exit_drain: DrainResult | None = None
        self._holds: list[FakeHold] = []
        self._exited = asyncio.Event()

    def count(self, name: str) -> int:
        return sum(1 for call, _ in self.calls if call == name)

    async def start(self, *, before_spawn=None):
        self.calls.append(("start", None))
        if before_spawn is not None:
            await _maybe_await(before_spawn())
        if self.start_failures:
            self.alive = False
            raise self.start_failures.pop(0)
        self.number += 1
        self.alive = True
        self._exited.clear()
        self.boot = Boot(
            number=self.number,
            boot_id=f"boot-{self.number}",
            base_url="http://127.0.0.1:1",
            token="t",
            pid=self.number,
            port=1,
        )
        return self.boot.base_url, self.boot.token, self.boot.boot_id

    async def stop(self, *, drain=False, cancel=None, on_drain=None):
        self.calls.append(("stop", drain))
        if not self.alive:
            return None
        result = None
        if drain and self.exit_backlog:
            await _maybe_await(on_drain(self.exit_backlog))
            waiter = asyncio.ensure_future(cancel.wait())
            await asyncio.wait({waiter})
            result = DrainResult("skipped", self.exit_backlog, 0.0)
            self.exit_drain = result
        self.alive = False
        return StopResult(drain=result, shutdown=None, killed=False, returncode=0)

    async def restart(self, *, cancel=None, on_drain=None, before_spawn=None):
        self.calls.append(("restart", None))
        self.concurrent_restarts += 1
        self.most_concurrent_restarts = max(self.most_concurrent_restarts, self.concurrent_restarts)
        self.restarting = True
        try:
            await self.stop(drain=True, cancel=cancel, on_drain=on_drain)

            async def spawn_hook() -> None:
                if before_spawn is not None:
                    await _maybe_await(before_spawn())
                if self.during_restart is not None:
                    await _maybe_await(self.during_restart(self.count("restart")))

            await self.start(before_spawn=spawn_hook)
            assert self.boot is not None
            return self.boot
        finally:
            self.concurrent_restarts -= 1
            self.restarting = False

    @contextlib.asynccontextmanager
    async def hold(self) -> AsyncIterator[FakeHold]:
        if self.hold_error is not None:
            raise self.hold_error
        if not self.alive or self.boot is None:
            raise AppUnavailable("not running")
        the_hold = FakeHold(self, self.boot.boot_id)
        self._holds.append(the_hold)
        try:
            yield the_hold
        finally:
            self._holds.remove(the_hold)

    async def wait_exit(self) -> int | None:
        await self._exited.wait()
        return -9

    def crash(self) -> None:
        """The app process dies on its own."""
        self.alive = False
        for the_hold in self._holds:
            the_hold.invalidated.set()
        self._exited.set()


class FakeWatcher:
    """Stands in for ``AppWatcher``; ``change()`` is a save in ``app/``."""

    def __init__(self) -> None:
        self.dirty = False
        self.generation = 0
        self.baselines = 0
        self.stopped = False
        self._event = asyncio.Event()

    async def run(self, stop: asyncio.Event) -> None:
        await stop.wait()
        self.stopped = True

    async def baseline(self) -> None:
        self.baselines += 1
        self.dirty = False
        self._event.clear()

    async def wait_dirty(self) -> None:
        await self._event.wait()

    async def wait_settled(self) -> None:
        await self.wait_dirty()

    def change(self) -> None:
        self.dirty = True
        self.generation += 1
        self._event.set()


class Recorder:
    """A renderer that keeps everything it is shown, and for each line how
    many restarts the app had been asked for and whether one was going on."""

    def __init__(self) -> None:
        self.events: list[Any] = []
        self.run_started_calls = 0
        self.broken = False
        self.app: FakeApp | None = None
        self.restarts_at: list[tuple[str, int, bool]] = []

    def show(self, event: Any) -> None:
        if self.broken:
            raise OSError(errno.EIO, "terminal hung up")
        self.events.append(event)
        if isinstance(event, Say) and self.app is not None:
            self.restarts_at.append((event.key, self.app.count("restart"), self.app.restarting))

    def restarts_when(self, key: str) -> int:
        """How many restarts the app had been asked for when ``key`` was first shown."""
        return next(count for shown, count, _ in self.restarts_at if shown == key)

    def restarting_when(self, key: str) -> bool:
        """Whether a restart was going on when ``key`` was first shown."""
        return next(going for shown, _, going in self.restarts_at if shown == key)

    def run_started(self, total: int) -> None:
        self.run_started_calls += 1

    def progress(self, event: Any) -> None:
        pass

    def run_ended(self) -> None:
        pass

    def says(self) -> list[Say]:
        return [event for event in self.events if isinstance(event, Say)]

    def say_keys(self) -> list[str]:
        return [event.key for event in self.says()]

    def awaits(self) -> list[str]:
        return [event.expect for event in self.events if isinstance(event, AwaitInput)]


class Keyboard:
    """The input line: a test types answers, or ``None`` for Ctrl-C."""

    def __init__(self) -> None:
        self._answers: asyncio.Queue[Any] = asyncio.Queue()
        self.reads = 0

    def type(self, answer: Any) -> None:
        self._answers.put_nowait(answer)

    async def read_line(self, _expect: str) -> str | None:
        self.reads += 1
        answer = await self._answers.get()
        if isinstance(answer, BaseException):
            raise answer
        return answer


class FakeDrains:
    """Replaces the runner's drain: each drain shows a backlog until released."""

    def __init__(self) -> None:
        self.backlog = 0
        self.results: list[DrainResult] = []
        self.release = asyncio.Event()
        self.running = False

    async def __call__(self, control, *, cancel=None, on_wait=None) -> DrainResult:
        if not self.backlog:
            result = DrainResult("drained", 0, 0.0)
            self.results.append(result)
            return result
        self.running = True
        try:
            if on_wait is not None:
                await _maybe_await(on_wait(self.backlog))
            waiters = {asyncio.ensure_future(self.release.wait())}
            if cancel is not None:
                waiters.add(asyncio.ensure_future(cancel.wait()))
            _done, pending = await asyncio.wait(waiters, return_when=asyncio.FIRST_COMPLETED)
            for waiter in pending:
                waiter.cancel()
            skipped = cancel is not None and cancel.is_set()
            result = DrainResult("skipped" if skipped else "drained", self.backlog, 0.0)
            if not skipped:
                self.backlog = 0
            self.results.append(result)
            return result
        except asyncio.CancelledError:
            self.results.append(DrainResult("skipped", self.backlog, 0.0))
            raise
        finally:
            self.running = False


class Session:
    """One scripted console session over the fakes."""

    def __init__(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        *,
        interactive: bool = True,
        yes: bool = False,
        until: int | None = None,
        save_state: bool = True,
    ) -> None:
        (tmp_path / "app").mkdir(exist_ok=True)
        (tmp_path / "app" / "main.py").write_text("")
        self.root = tmp_path
        self.app = FakeApp()
        self.watcher = FakeWatcher()
        self.renderer = Recorder()
        self.renderer.app = self.app
        self.keyboard = Keyboard()
        self.drains = FakeDrains()
        self.run_script: Callable[..., Awaitable[RunResult]] | None = None
        self.state_path = tmp_path / ".playground" / "state.json" if save_state else None
        self.machine = Machine(offline_total=VISITORS, unattended=yes)
        self.interactive = interactive
        self.yes = yes
        self.until = until
        self.discard_typeahead: Callable[[], None] | None = None
        self.task: asyncio.Task[int] | None = None

        monkeypatch.setattr(runner, "drain", self.drains)
        monkeypatch.setattr(runner, "build_offline_plan", lambda total, seed: [None] * VISITORS)
        monkeypatch.setattr(runner, "run_offline", self._run_offline)

    async def _run_offline(self, plan, base_url, token, *, on_progress, stop, run_id=None):
        assert run_id
        if self.run_script is not None:
            return await self.run_script(stop)
        return FINISHED

    def start(self) -> None:
        self.task = asyncio.ensure_future(
            runner.run(
                self.machine,
                self.renderer,
                self.keyboard.read_line,
                root=self.root,
                yes=self.yes,
                until=self.until,
                state_path=self.state_path,
                interactive=self.interactive,
                app=self.app,  # type: ignore[arg-type]
                watcher=self.watcher,  # type: ignore[arg-type]
                discard_typeahead=self.discard_typeahead,
            )
        )

    async def said(self, key: str, times: int = 1) -> None:
        await until(lambda: self.renderer.say_keys().count(key) >= times)

    async def opened(self) -> None:
        await self.said("opening")
        await until(lambda: self.keyboard.reads >= 1)

    async def restarted(self, times: int = 1) -> None:
        """Wait until the app has been restarted ``times`` times and is up again."""
        await until(
            lambda: (
                self.app.count("restart") >= times and not self.app.restarting and self.app.alive
            )
        )

    async def finish(self) -> int:
        assert self.task is not None
        return await asyncio.wait_for(self.task, WITHIN_S)


def _run(coroutine_function: Callable[[], Awaitable[None]]) -> None:
    asyncio.run(coroutine_function())


# -- restarts while waiting for input -------------------------------------------------


def test_a_change_during_a_restart_gives_exactly_one_follow_up(tmp_path, monkeypatch):
    async def scenario() -> None:
        session = Session(tmp_path, monkeypatch)

        async def save_during_first_spawn(number: int) -> None:
            if number == 1:
                session.watcher.change()  # saved after the new baseline

        session.app.during_restart = save_during_first_spawn
        session.start()
        await session.opened()

        session.watcher.change()
        await session.restarted(times=2)
        await asyncio.sleep(POLL_S * 5)  # nothing more is due

        assert session.app.count("restart") == 2
        assert session.app.most_concurrent_restarts == 1
        assert session.app.number == 3  # boots 2 and 3
        # At the opening a reload is no news: no line for either.
        assert "app.reloaded" not in session.renderer.say_keys()

        session.keyboard.type(None)  # Ctrl-C at the prompt
        assert await session.finish() == 0

    _run(scenario)


def test_an_answer_typed_during_a_restart_waits_for_its_result(tmp_path, monkeypatch):
    async def scenario() -> None:
        session = Session(tmp_path, monkeypatch)
        spawn_gate = asyncio.Event()
        in_restart = asyncio.Event()

        async def hold_the_spawn(_number: int) -> None:
            in_restart.set()
            await spawn_gate.wait()

        session.app.during_restart = hold_the_spawn
        session.start()
        await session.opened()

        session.watcher.change()
        await asyncio.wait_for(in_restart.wait(), WITHIN_S)
        session.keyboard.type("/help")
        await asyncio.sleep(POLL_S * 5)
        assert "help" not in session.renderer.say_keys()

        spawn_gate.set()
        await session.said("help")
        # The answer came once the restart was over.
        assert session.renderer.restarts_when("help") == 1
        assert session.renderer.restarting_when("help") is False
        assert session.app.number == 2

        session.keyboard.type(None)
        assert await session.finish() == 0

    _run(scenario)


def test_ctrl_c_at_the_prompt_during_a_reload_drain_quits_at_once(tmp_path, monkeypatch):
    async def scenario() -> None:
        session = Session(tmp_path, monkeypatch)
        session.drains.backlog = 7
        session.start()
        await session.opened()

        session.watcher.change()
        await session.said("drain.waiting_quit")
        session.keyboard.type(None)  # Ctrl-C is a key at the prompt: it quits
        assert await session.finish() == 0

        assert session.drains.results[0].outcome == "skipped"
        assert session.app.count("restart") == 0
        # The way out stops the app without waiting for its logs again.
        assert session.app.calls[-1] == ("stop", False)
        assert "drain.skipped" not in session.renderer.say_keys()

    _run(scenario)


def test_a_save_before_an_automatic_answer_restarts_first(tmp_path, monkeypatch):
    async def scenario() -> None:
        session = Session(
            tmp_path, monkeypatch, interactive=False, yes=True, until=1, save_state=False
        )

        async def run(stop: asyncio.Event) -> RunResult:
            session.watcher.change()
            await until(lambda: "reload.deferred" in session.renderer.say_keys())
            return FINISHED

        session.run_script = run
        session.start()
        assert await session.finish() == 0

        keys = session.renderer.say_keys()
        assert keys.count("reload.deferred") == 1
        # The close shows the run's tally first; the restart comes before
        # the automatic answer, whose achievement line follows it.
        assert session.renderer.restarts_when("ch1.close") == 0
        assert session.renderer.restarts_when("achievement.ch1") == 1
        assert session.renderer.restarting_when("achievement.ch1") is False
        assert session.app.count("restart") == 1
        # The run said the reload would wait: once it is in, that is said too.
        assert keys.count("app.reloaded") == 1
        assert keys.index("ch1.close") < keys.index("app.reloaded") < keys.index("achievement.ch1")

    _run(scenario)


def test_saves_during_a_run_are_noticed_once_and_wait_for_the_tally(tmp_path, monkeypatch):
    async def scenario() -> None:
        session = Session(tmp_path, monkeypatch)
        seen_during_run: dict[str, Any] = {}

        async def run(stop: asyncio.Event) -> RunResult:
            for _ in range(3):
                session.watcher.change()
                await asyncio.sleep(POLL_S)
            await until(lambda: "reload.deferred" in session.renderer.say_keys())
            await asyncio.sleep(POLL_S * 5)
            seen_during_run["stopped"] = stop.is_set()
            seen_during_run["restarts"] = session.app.count("restart")
            return FINISHED

        session.run_script = run
        session.start()
        await session.opened()
        session.keyboard.type("/start")
        await session.said("ch1.close")
        await session.restarted()
        await asyncio.sleep(POLL_S * 5)  # nothing more is due

        assert seen_during_run == {"stopped": False, "restarts": 0}
        keys = session.renderer.say_keys()
        assert keys.count("reload.deferred") == 1
        assert session.renderer.restarts_when("ch1.close") == 0
        assert session.app.count("restart") == 1
        # The run said the reload would wait: once it is in, that is said too.
        assert keys.count("app.reloaded") == 1
        assert session.renderer.restarts_when("app.reloaded") == 1

        session.keyboard.type(None)
        assert await session.finish() == 0

    _run(scenario)


# -- the app down, or gone ---------------------------------------------------------------


def test_yes_with_the_app_down_exits_2(tmp_path, monkeypatch):
    async def scenario() -> None:
        session = Session(tmp_path, monkeypatch, interactive=False, yes=True, save_state=False)
        session.app.start_failures.append(AppStartError("down", location="app/routes.py:3"))
        session.start()
        assert await session.finish() == 2
        assert session.renderer.say_keys()[-1] == "app.down_at"
        assert isinstance(session.renderer.events[-1], Exit)

    _run(scenario)


def test_a_hold_that_fails_at_run_start_invalidates_the_run(tmp_path, monkeypatch):
    async def scenario() -> None:
        session = Session(tmp_path, monkeypatch)
        session.start()
        await session.opened()

        session.app.hold_error = AppUnavailable("gone")
        session.keyboard.type("/start")
        await session.said("run.again")
        await until(lambda: session.renderer.awaits()[-1:] == ["start"])

        assert session.renderer.run_started_calls == 0
        keys = session.renderer.say_keys()
        assert keys[-2:] == ["run.invalidated", "run.again"]

        session.keyboard.type(None)
        assert await session.finish() == 0

    _run(scenario)


def test_a_second_crash_after_the_automatic_restart_waits_for_a_save(tmp_path, monkeypatch):
    async def scenario() -> None:
        session = Session(tmp_path, monkeypatch)
        session.start()
        await session.opened()

        session.app.crash()
        await session.said("app.restarted")
        assert session.app.count("start") == 2

        session.app.crash()
        await session.said("app.down")
        await asyncio.sleep(POLL_S * 5)
        assert session.app.count("start") == 2  # no second automatic restart

        session.keyboard.type("/start")
        await session.said("app.not_ready")

        session.watcher.change()
        # The save brings the app back after it went down: that is news anywhere.
        await session.said("app.reloaded")
        assert session.app.number == 3
        reloaded = [say for say in session.renderer.says() if say.key == "app.reloaded"]
        assert [say.params for say in reloaded] == [{}]

        session.keyboard.type(None)
        assert await session.finish() == 0

    _run(scenario)


def test_a_crash_mid_run_invalidates_it_and_restarts_the_app(tmp_path, monkeypatch):
    async def scenario() -> None:
        session = Session(tmp_path, monkeypatch)

        async def run(stop: asyncio.Event) -> RunResult:
            session.app.crash()
            await asyncio.wait_for(stop.wait(), WITHIN_S)  # the runner stops the run
            return RunResult(VISITORS, 3, 3, 1, 8, 2, True)

        session.run_script = run
        session.start()
        await session.opened()
        session.keyboard.type("/start")
        await session.said("run.invalidated")
        await session.said("app.restarted")

        session.keyboard.type(None)
        assert await session.finish() == 0

    _run(scenario)


# -- which drain line, and what Ctrl-C then does -----------------------------------------


def test_the_first_ctrl_c_in_a_post_run_drain_skips_it(tmp_path, monkeypatch):
    async def scenario() -> None:
        session = Session(tmp_path, monkeypatch)
        session.drains.backlog = 5
        session.start()
        await session.opened()

        session.keyboard.type("/start")
        await session.said("drain.waiting")
        signal.raise_signal(signal.SIGINT)
        await session.said("drain.skipped")
        await session.said("ch1.close")  # the session goes on

        session.keyboard.type(None)
        assert await session.finish() == 0
        assert "drain.waiting_quit" not in session.renderer.say_keys()

    _run(scenario)


def test_a_reload_drain_without_a_prompt_is_skipped_and_the_restart_goes_on(tmp_path, monkeypatch):
    async def scenario() -> None:
        session = Session(tmp_path, monkeypatch, interactive=False)
        session.drains.backlog = 5
        session.start()
        await session.opened()

        session.watcher.change()
        await session.said("drain.waiting")
        signal.raise_signal(signal.SIGINT)
        await session.said("drain.skipped")
        await session.restarted()
        assert session.app.number == 2

        session.keyboard.type("/quit")
        assert await session.finish() == 0

    _run(scenario)


def test_a_drain_after_a_stopping_ctrl_c_says_the_next_one_quits(tmp_path, monkeypatch):
    async def scenario() -> None:
        session = Session(tmp_path, monkeypatch)
        session.drains.backlog = 5

        async def run(stop: asyncio.Event) -> RunResult:
            signal.raise_signal(signal.SIGINT)
            await asyncio.wait_for(stop.wait(), WITHIN_S)
            return RunResult(VISITORS, 4, 4, 1, 8, 2, True)

        session.run_script = run
        session.start()
        await session.opened()
        session.keyboard.type("/start")
        await session.said("console.interrupt_again")
        await session.said("drain.waiting_quit")

        signal.raise_signal(signal.SIGINT)
        assert await session.finish() == 0
        assert "ch1.stopped" not in session.renderer.say_keys()
        assert "drain.waiting" not in session.renderer.say_keys()

    _run(scenario)


def test_the_exit_drain_says_ctrl_c_quits_and_it_does(tmp_path, monkeypatch):
    async def scenario() -> None:
        session = Session(tmp_path, monkeypatch)
        session.app.exit_backlog = 4
        session.start()
        await session.opened()

        session.keyboard.type("/quit")
        await session.said("drain.waiting_quit")
        signal.raise_signal(signal.SIGINT)
        assert await session.finish() == 0

        assert session.app.exit_drain is not None
        assert session.app.exit_drain.outcome == "skipped"
        keys = session.renderer.say_keys()
        assert keys.index("console.bye") < keys.index("drain.waiting_quit")

    _run(scenario)


# -- saving progress -----------------------------------------------------------------------


def test_quit_saves_before_it_says_so(tmp_path, monkeypatch):
    async def scenario() -> None:
        session = Session(tmp_path, monkeypatch)
        session.start()
        await session.opened()
        session.keyboard.type("/start")
        await session.said("ch1.close")
        session.keyboard.type("/quit")
        assert await session.finish() == 0

        keys = session.renderer.say_keys()
        assert keys[-2:] == ["console.saved", "console.bye"]
        assert session.state_path is not None
        saved = json.loads(session.state_path.read_text())
        assert saved["machine"]["awaiting"] == "yes_no"

    _run(scenario)


def test_a_failing_save_is_reported_once(tmp_path, monkeypatch):
    attempts: list[dict] = []

    def failing_save(path, snapshot) -> bool:
        attempts.append(dict(snapshot))
        return False

    monkeypatch.setattr(runner.statefile, "save", failing_save)

    async def scenario() -> None:
        session = Session(tmp_path, monkeypatch)
        session.start()
        await session.opened()
        session.keyboard.type("/start")
        await session.said("ch1.close")
        session.keyboard.type("yes")
        await session.said("ch2.key.ask")
        session.keyboard.type(None)  # quit now: the last save fails too
        assert await session.finish() == 0

        assert len(attempts) >= 3
        assert session.renderer.say_keys().count("state.save_failed") == 1

    _run(scenario)


def test_an_unattended_session_never_touches_saved_progress(tmp_path, monkeypatch):
    def forbidden(*_args, **_kwargs):
        raise AssertionError("an unattended session touched state.json")

    monkeypatch.setattr(runner.statefile, "save", forbidden)
    monkeypatch.setattr(runner.statefile, "load", forbidden)

    async def scenario() -> None:
        session = Session(
            tmp_path, monkeypatch, interactive=False, yes=True, until=1, save_state=False
        )
        session.start()
        assert await session.finish() == 0
        assert "ch1.close" in session.renderer.say_keys()

    _run(scenario)
    assert not (tmp_path / ".playground").exists()


# -- signals, hang-ups and errors ----------------------------------------------------------


def test_the_signal_fallback_feeds_the_same_dispatcher(tmp_path, monkeypatch):
    previous = signal.getsignal(signal.SIGINT)

    async def scenario() -> None:
        loop = asyncio.get_running_loop()

        def unsupported(*_args, **_kwargs):
            raise NotImplementedError

        monkeypatch.setattr(loop, "add_signal_handler", unsupported)
        session = Session(tmp_path, monkeypatch)

        async def run(stop: asyncio.Event) -> RunResult:
            signal.raise_signal(signal.SIGINT)
            await asyncio.wait_for(stop.wait(), WITHIN_S)
            return RunResult(VISITORS, 4, 4, 1, 8, 2, True)

        session.run_script = run
        session.start()
        await session.opened()
        assert signal.getsignal(signal.SIGINT) is not previous
        session.keyboard.type("/start")
        await session.said("console.interrupt_again")
        await session.said("ch1.stopped")

        session.keyboard.type(None)
        assert await session.finish() == 0

    _run(scenario)
    assert signal.getsignal(signal.SIGINT) is previous


def test_a_hung_up_input_line_quits_cleanly(tmp_path, monkeypatch):
    async def scenario() -> None:
        session = Session(tmp_path, monkeypatch)
        session.start()
        await session.opened()
        session.keyboard.type(OSError(errno.EIO, "input/output error"))
        assert await session.finish() == 0
        assert session.app.calls[-1] == ("stop", False)

    _run(scenario)


def test_a_hung_up_terminal_on_the_way_out_is_ignored(tmp_path, monkeypatch):
    async def scenario() -> None:
        session = Session(tmp_path, monkeypatch)
        session.app.exit_backlog = 4
        session.start()
        await session.opened()

        session.renderer.broken = True
        session.keyboard.type("/quit")
        await until(lambda: session.app.count("stop") >= 1)
        signal.raise_signal(signal.SIGINT)
        assert await session.finish() == 0
        assert session.app.exit_drain is not None

    _run(scenario)


def test_sigterm_quits_without_saying_anything_more(tmp_path, monkeypatch):
    async def scenario() -> None:
        session = Session(tmp_path, monkeypatch)
        session.start()
        await session.opened()
        shown = len(session.renderer.events)
        signal.raise_signal(signal.SIGTERM)
        assert await session.finish() == 0
        assert len(session.renderer.events) == shown
        assert session.app.calls[-1] == ("stop", False)

    _run(scenario)


def test_an_error_inside_the_session_still_stops_the_app(tmp_path, monkeypatch):
    async def scenario() -> None:
        session = Session(tmp_path, monkeypatch)

        async def run(stop: asyncio.Event) -> RunResult:
            raise RuntimeError("boom")

        session.run_script = run
        session.start()
        await session.opened()
        session.keyboard.type("/start")
        with pytest.raises(RuntimeError, match="boom"):
            await session.finish()
        assert session.app.count("stop") == 1
        assert not session.app.alive
        assert session.watcher.stopped

    _run(scenario)
