"""One asyncio loop that drives the machine, the renderer, the sample app and its watcher.

``cli.py`` builds a :class:`~playground.machine.machine.Machine`, a renderer
and a way to read one input line, then hands them to :func:`run`. From there
this module owns the whole session:

- it turns machine output events into renderer calls and carries out the
  machine's effects (starting the sample app, running chapter 1 offline,
  saving progress), feeding their results back through ``machine.on_event``;
- while the machine waits for input, it keeps the sample app in step with
  your files: a saved change restarts the app, and an app that stopped on
  its own is started again once. The input line stays open meanwhile, and
  what these restarts report prints above it;
- during a run it holds the current boot open, so a change waits until the
  run and its drain are over, and a boot that stops mid-run makes that run
  not count;
- it owns Ctrl-C, SIGTERM and SIGHUP, and on every way out it stops the
  sample app over HTTP first;
- while the app's SDK sends its queued log events it tells the renderer how
  far that has got, with an estimate of the time left;
- it keeps the active console time since ``/start``, and hands it to the
  machine with each result, for the card at the end.

It never prints anything of its own: every user-visible string still comes
from ``playground.copy.en`` through the renderer.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import secrets
import signal
import time
import traceback
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, Literal, TypeVar

import httpx

from playground import statefile
from playground.machine.events import (
    AppDown,
    AppFailed,
    AppReady,
    AppReloaded,
    AwaitInput,
    ChangeDetected,
    ChapterDone,
    CheckKey,
    Complete,
    CopySnippets,
    DeliverProgress,
    EndSession,
    Event,
    Exit,
    Expect,
    OpenSession,
    ReadStage,
    RunChapter1,
    RunFinished,
    RunGate,
    RunInvalidated,
    RunProgress,
    RunReadiness,
    RunStopped,
    RunTraffic,
    SaveState,
    Say,
    StartApp,
    StateSaved,
    StepDone,
    WaitWorkflow,
    WriteReport,
)
from playground.machine.machine import Machine
from playground.online import Online
from playground.progress import RateWindow, eta_seconds
from playground.supervisor.drain import DrainResult, drain
from playground.supervisor.launcher import (
    AppProcess,
    AppStartError,
    AppUnavailable,
    Hold,
    StopResult,
)
from playground.supervisor.watcher import AppWatcher
from playground.traffic.driver import Progress, RunResult, run_offline
from playground.traffic.local_constants import APP_STOP_GRACE_S, DRAIN_MAX_S
from playground.traffic.plan import build_offline_plan
from playground.tui.base import Renderer

# Chapter 1 always sends this many offline visitors, regardless of --size
# (--size is reserved for a later chapter).
CHAPTER1_VISITORS = 10_000

# Where the console keeps its own files, relative to the repo root.
STATE_RELATIVE = Path(".playground") / "state.json"

_APP_DIR_RELATIVE = Path("app")
_APP_MAIN_RELATIVE = _APP_DIR_RELATIVE / "main.py"
_APP_LOG_RELATIVE = Path(".playground") / "app.log"
# Where an unexpected error in an online step is written down (never shown).
_CONSOLE_LOG_RELATIVE = Path(".playground") / "console.log"

# The effects of chapters 2-6, carried out by ``playground.online``.
_ONLINE_EFFECTS = (
    CheckKey,
    OpenSession,
    EndSession,
    RunGate,
    RunTraffic,
    WaitWorkflow,
    RunReadiness,
    ReadStage,
    Complete,
    WriteReport,
    CopySnippets,
)

ReadLine = Callable[[Expect], Awaitable["str | None"]]

T = TypeVar("T")
# A result the runner stamps with the active console time (see ``_Session._stamped``).
_Stamped = TypeVar("_Stamped", StepDone, RunFinished)

# What kind of drain a waiting line belongs to (see ``_Session._drain_key``).
DrainKind = Literal["run", "reload", "exit"]

_GAVE_UP = ("stalled", "capped")


class _Drain:
    """One wait for the app's queued log events, as shown so far.

    ``first`` is the line it began with (None until the app reported a
    backlog). ``total`` counts every event the wait has seen queued and
    ``done`` the ones sent since; both only ever grow, even when new events
    join the queue while it empties.
    """

    def __init__(self, began: float | None = None) -> None:
        self.first: Say | None = None
        self.total = 0
        self.done = 0
        self._rate = RateWindow()
        # When the wait began (monotonic; None: at the first count). It gives
        # up DRAIN_MAX_S after that, so no estimate says it takes longer.
        self._began = began

    def update(self, now: float, remaining: int) -> DeliverProgress:
        """The app reports ``remaining`` events still queued at ``now``."""
        if self._began is None:
            self._began = now
        self.total = max(self.total, self.done + remaining)
        self.done = max(0, self.total - remaining)
        self._rate.add(now, remaining)
        left = DRAIN_MAX_S - (now - self._began)
        eta = eta_seconds(remaining, self._rate.rate(), at_most=left)
        return DeliverProgress(self.done, self.total, eta)


class _Quit:
    """Sentinel: the user asked to quit now (Ctrl-C, SIGTERM, SIGHUP, a hangup)."""


_QUIT = _Quit()


class _QuitRequested(Exception):
    """Raised right before a spawn once the user has asked to quit, so a
    restart that is under way stops the old app but never starts a new one."""


class _TurnReply(Exception):
    """A result fed back while waiting for input produced a turn reply (for
    example ``Exit(2)`` in an unattended session) instead of a notice. The
    main loop carries out its events in place of an answer."""

    def __init__(self, events: list[Event]) -> None:
        super().__init__("turn reply")
        self.events = events


def find_repo_root(start: Path | None = None) -> Path:
    """Walk up from ``start`` (default: the current directory) looking for
    ``app/main.py``. Falls back to ``start`` itself when none is found, so
    callers always have somewhere to put ``.playground/`` files; the sample
    app's own failure to start is reported through ``StartApp`` instead."""
    here = (start or Path.cwd()).resolve()
    for candidate in (here, *here.parents):
        if (candidate / _APP_MAIN_RELATIVE).is_file():
            return candidate
    return here


async def _race_quit(awaitable: Awaitable[T], quit_now: asyncio.Event) -> T | None:
    """Await ``awaitable``, unless ``quit_now`` fires first.

    Returns the awaitable's result, or ``None`` if the user quit instead: a
    plain ``KeyboardInterrupt`` raised from inside ``awaitable`` itself (an
    interactive prompt intercepting Ctrl-C directly) counts as quitting too,
    and so does an ``OSError`` from a terminal that hung up.
    """
    task = asyncio.ensure_future(awaitable)
    quit_task = asyncio.ensure_future(quit_now.wait())
    try:
        done, _pending = await asyncio.wait({task, quit_task}, return_when=asyncio.FIRST_COMPLETED)
    except KeyboardInterrupt:
        quit_now.set()
        done = set()

    if task in done:
        quit_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await quit_task
        try:
            return task.result()
        except (KeyboardInterrupt, EOFError, OSError):
            # Ctrl-C or Ctrl-D surfacing from the input itself, or a
            # hung-up terminal: all of them mean quit.
            quit_now.set()
            return None

    task.cancel()
    with contextlib.suppress(asyncio.CancelledError, KeyboardInterrupt, EOFError, OSError):
        await task
    quit_task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await quit_task
    return None


def _auto_answer(
    expect: Expect, *, yes: bool, until: int | None, decline: bool = False
) -> str | None:
    """The answer ``--yes`` gives to ``expect`` by itself, or None to read one.

    ``decline`` makes it answer a yes/no question no (the machine's
    ``unattended_no``). A wait where Enter goes on is answered yes, which
    goes on there too.
    """
    if not yes:
        return None
    if expect == "start":
        return "/start"
    if expect == "yes_no":
        return "no" if decline else "yes"
    if expect == "continue":
        return "yes"
    if expect == "any" and until is None:
        return "/quit"
    return None


async def _next_answer(
    expect: Expect,
    *,
    read_line: ReadLine,
    yes: bool,
    until: int | None,
    quit_now: asyncio.Event,
    idle: _Session | None = None,
    decline: bool = False,
) -> str | None:
    """The next answer for ``expect``, or ``None`` if the user quit instead.

    With ``idle``, the session's own work (restarting the sample app after a
    change, or after it stopped) is served while the read stays open, and
    before an automatic ``--yes`` answer is handed over.
    """
    answer = _auto_answer(expect, yes=yes, until=until, decline=decline)
    if answer is not None:
        if idle is not None:
            await idle.serve_due_work()
            if quit_now.is_set():
                return None
        return answer
    if idle is None:
        return await _race_quit(read_line(expect), quit_now)
    return await idle.read_while_serving(read_line(expect))


async def _read_or_none(read: Awaitable[str | None]) -> str | None:
    """``read``'s answer, with Ctrl-C, Ctrl-D and a hung-up terminal read as quit."""
    try:
        return await read
    except (KeyboardInterrupt, EOFError, OSError):
        return None


async def _cancel_and_wait(task: asyncio.Future[Any] | None) -> None:
    """Cancel ``task`` if it's still running, and wait for it to finish.

    Whatever it ends with is retrieved and dropped, so nothing is ever
    reported as an unretrieved task error.
    """
    if task is None:
        return
    if not task.done():
        task.cancel()
    await asyncio.wait({task})
    if not task.cancelled():
        task.exception()


class _QuietableRenderer:
    """The renderer, made safe for the console's own use.

    A write to a hung-up terminal never escapes (``OSError`` is swallowed and
    the renderer goes quiet), and SIGTERM or SIGHUP can switch all output off.
    """

    def __init__(self, inner: Renderer) -> None:
        self._inner = inner
        self._quiet = False
        # Whether the inner renderer was told a drain began and not yet that it ended.
        self._draining = False

    def quiet(self) -> None:
        self._quiet = True

    def _call(self, method: Callable[..., None], *args: Any) -> None:
        if self._quiet:
            return
        try:
            method(*args)
        except OSError:
            self._quiet = True

    def show(self, event: Event) -> None:
        self._call(self._inner.show, event)

    def run_started(self, total: int, title: str | None = None) -> None:
        if title is None:
            self._call(self._inner.run_started, total)
        else:
            self._call(lambda: self._inner.run_started(total, title=title))

    def progress(self, event: RunProgress) -> None:
        self._call(self._inner.progress, event)

    def wait(self, event: Say) -> None:
        method = getattr(self._inner, "wait", None)
        if method is not None:
            self._call(method, event)
        else:
            self.show(event)

    def wait_ended(self) -> None:
        method = getattr(self._inner, "wait_ended", None)
        if method is not None:
            try:
                method()
            except OSError:
                self._quiet = True

    def run_ended(self) -> None:
        # Always let a live display close, even once quiet, so the terminal
        # is left as it was found.
        try:
            self._inner.run_ended()
        except OSError:
            self._quiet = True

    # The wait for the app's queued log events. A renderer without these
    # methods shows the first line like any other, and nothing more.

    def drain_begin(self, first: Say, *, inline: bool = True) -> None:
        method = getattr(self._inner, "drain_begin", None)
        if method is None:
            self.show(first)
            return
        if self._quiet:
            return
        self._draining = True
        self._call(lambda: method(first, inline=inline))

    def drain_progress(self, event: DeliverProgress) -> None:
        method = getattr(self._inner, "drain_progress", None)
        if method is not None and self._draining:
            self._call(method, event)

    def drain_end(self) -> None:
        # Like ``run_ended``: a line drawn in place is always taken down.
        if not self._draining:
            return
        self._draining = False
        method = getattr(self._inner, "drain_end", None)
        if method is not None:
            try:
                method()
            except OSError:
                self._quiet = True


class _Signals:
    """The console's signal handlers: installed once, removed on the way out.

    Uses ``loop.add_signal_handler`` where the loop supports it. Where it
    doesn't, falls back to ``signal.signal`` with a handler that hands the
    callback to the loop, and puts the previous handler back on removal.
    """

    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop
        self._added: list[int] = []
        self._replaced: list[tuple[int, Any]] = []

    def install(self, signum: int, callback: Callable[[], None]) -> None:
        try:
            self._loop.add_signal_handler(signum, callback)
        except NotImplementedError:
            pass
        except (RuntimeError, ValueError):
            return  # not the main thread: nothing can be installed here
        else:
            self._added.append(signum)
            return
        try:
            previous = signal.getsignal(signum)
            signal.signal(signum, lambda *_: self._loop.call_soon_threadsafe(callback))
        except (OSError, RuntimeError, ValueError):
            return
        self._replaced.append((signum, previous))

    def remove(self) -> None:
        for signum in self._added:
            with contextlib.suppress(NotImplementedError, RuntimeError, ValueError):
                self._loop.remove_signal_handler(signum)
        for signum, previous in self._replaced:
            with contextlib.suppress(OSError, RuntimeError, TypeError, ValueError):
                signal.signal(signum, previous)
        self._added.clear()
        self._replaced.clear()


class _Session:
    """Everything one console session keeps between events."""

    def __init__(
        self,
        machine: Machine,
        renderer: Renderer,
        read_line: ReadLine,
        *,
        root: Path,
        yes: bool,
        until: int | None,
        state_path: Path | None,
        interactive: bool,
        app: AppProcess | None,
        watcher: AppWatcher | None,
        api_transport: httpx.AsyncBaseTransport | None = None,
        telemetry: bool = True,
        discard_typeahead: Callable[[], None] | None = None,
    ) -> None:
        self._machine = machine
        self._renderer = _QuietableRenderer(renderer)
        self._read_line = read_line
        self._discard_typeahead = discard_typeahead
        self._root = root
        self._yes = yes
        self._until = until
        self._state_path = state_path
        self._interactive = interactive
        self._app = app if app is not None else AppProcess(root, root / _APP_LOG_RELATIVE)
        self._watcher = watcher

        self._quit_now = asyncio.Event()
        self._stop_run = asyncio.Event()

        # Whether a run is under way right now.
        self._run_active = False
        # Whether Ctrl-C has already been pressed in this episode: a run and
        # the drain after it, or one reload drain.
        self._pressed = False
        # The drain in progress, if any: setting this event ends it.
        self._drain_cancel: asyncio.Event | None = None
        # Whether a Ctrl-C now would skip the drain (and the session goes on)
        # rather than quit.
        self._drain_skippable = False
        # Whether the interactive prompt is up, so Ctrl-C there is a key.
        self._prompt_up = False

        # The runner's own view of the app: a healthy boot it's watching.
        self._app_up = False
        # The watcher generation whose one automatic respawn is used up.
        self._respawned_generation: int | None = None

        self._watch_stop: asyncio.Event | None = None
        self._watch_task: asyncio.Future[None] | None = None

        self._last_saved: dict[str, Any] | None = None
        self._save_failed_shown = False

        # The active console time since /start (monotonic seconds): measured
        # from ``_clock``, plus ``_clock_offset`` carried over from a saved
        # session. ``_started_at`` is when this console started.
        self._started_at = time.monotonic()
        self._clock: float | None = None
        self._clock_offset = 0.0

        self._online = Online(self, root=root, api_transport=api_transport, telemetry=telemetry)

    # -- output -------------------------------------------------------------

    def _show(self, event: Event) -> None:
        self._renderer.show(event)

    def _effect_ended(self) -> None:
        """A check, a run or another effect is over, and nothing of its
        outcome is on screen yet: what was typed while it went on is dropped,
        so a key pressed meanwhile never answers the question that follows.
        Anything typed once the outcome shows is kept."""
        if self._discard_typeahead is not None:
            self._discard_typeahead()

    def _say(self, key: str, **params: Any) -> None:
        self._show(Say(key, params))

    # -- what the online steps use (see ``playground.online``) ----------------

    @property
    def machine(self) -> Machine:
        return self._machine

    @property
    def app(self) -> AppProcess:
        return self._app

    @property
    def stop_run(self) -> asyncio.Event:
        return self._stop_run

    def show(self, event: Event) -> None:
        self._show(event)

    def progress(self, event: RunProgress) -> None:
        self._renderer.progress(event)

    def panel_begin(self, total: int, title: str) -> None:
        self._renderer.run_started(total, title)

    def panel_end(self) -> None:
        self._renderer.run_ended()

    def wait_show(self, event: Say) -> None:
        self._renderer.wait(event)

    def wait_end(self) -> None:
        self._renderer.wait_ended()

    async def as_run(self, awaitable: Awaitable[T], held: Hold) -> T:
        """Await a traffic run: Ctrl-C stops it, a boot that goes away stops
        it, and the first saved change says the reload waits."""
        self._stop_run.clear()
        self._pressed = False
        self._run_active = True
        task = asyncio.ensure_future(awaitable)
        invalidated = asyncio.ensure_future(held.invalidated.wait())
        changed = (
            asyncio.ensure_future(self._watcher.wait_dirty()) if self._watcher is not None else None
        )
        try:
            while not task.done():
                waiters: set[asyncio.Future[Any]] = {task}
                waiters.update(w for w in (invalidated, changed) if w is not None and not w.done())
                await asyncio.wait(waiters, return_when=asyncio.FIRST_COMPLETED)
                if invalidated.done():
                    self._stop_run.set()
                if changed is not None and changed.done():
                    changed = None
                    for event in self._feed(ChangeDetected()):
                        self._show(event)
            return task.result()
        finally:
            self._run_active = False
            for waiter in (invalidated, changed):
                await _cancel_and_wait(waiter)
            await _cancel_and_wait(task)

    async def drain_after_run(self) -> None:
        await self._drain_now("run")

    async def restart_with_key(self) -> None:
        """Restart the sample app so it reads the key (a notice either way)."""
        drained = asyncio.Event()
        drained.set()
        try:
            boot = await self._app.restart(cancel=drained, before_spawn=self._before_spawn)
        except AppStartError as error:
            self._app_up = False
            self._show_notices(self._feed(AppDown(error.location, error.failure)))
            return
        self._app_up = True
        self._show_notices(self._feed(AppReloaded(boot.number, "request")))

    def _note_error(self) -> None:
        """Write an unexpected error down in ``.playground/console.log``."""
        with contextlib.suppress(OSError):
            path = self._root / _CONSOLE_LOG_RELATIVE
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as handle:
                handle.write(traceback.format_exc())

    async def _online_effect(self, event: Event) -> list[Event] | _Quit:
        try:
            outcome = await self._until_quit(self._online.handle(event))
        except _QuitRequested:
            return _QUIT
        except _TurnReply as reply:
            return reply.events
        except Exception:  # noqa: BLE001 - never a traceback: the step can be tried again
            self._note_error()
            outcome = StepDone(False, (Say("online.unexpected", {}),))
        if isinstance(outcome, _Quit):
            return _QUIT
        if outcome is None:
            return []
        return self._feed(self._stamped(outcome))

    # -- the active console time ------------------------------------------------

    def _start_clock(self) -> None:
        """``/start``: the active console time counts from now."""
        self._clock = time.monotonic()
        self._clock_offset = 0.0

    def _elapsed(self) -> float | None:
        """The active console time since ``/start``, or None without a clock.

        A resumed session carries on from the time its saved progress holds,
        counting from when this console started.
        """
        if self._clock is None:
            carried = self._machine.elapsed_s
            if carried is None:
                return None
            self._clock = self._started_at
            self._clock_offset = carried
        return self._clock_offset + (time.monotonic() - self._clock)

    def _stamped(self, outcome: _Stamped) -> _Stamped:
        """``outcome`` with the active console time it ended at, when there is a clock."""
        elapsed = self._elapsed()
        if elapsed is None:
            return outcome
        return dataclasses.replace(outcome, elapsed_s=elapsed)

    # -- signals ------------------------------------------------------------

    def on_sigint(self) -> None:
        """Ctrl-C, whichever way it arrived: stop the run, skip a drain, or quit."""
        if self._run_active and not self._stop_run.is_set():
            # The first Ctrl-C during a run stops it and lets in-flight
            # visitors finish; the drain after it can't be skipped any more.
            self._stop_run.set()
            self._pressed = True
            self._say("console.interrupt_again")
            return
        drain_cancel = self._drain_cancel
        if (
            drain_cancel is not None
            and not drain_cancel.is_set()
            and self._drain_skippable
            and not self._pressed
        ):
            self._pressed = True
            drain_cancel.set()
            return
        self._quit()

    def on_terminate(self) -> None:
        """SIGTERM or SIGHUP: quit now, with nothing more on screen."""
        self._renderer.quiet()
        self._quit()

    def _quit(self) -> None:
        self._quit_now.set()
        if self._drain_cancel is not None:
            self._drain_cancel.set()

    # -- progress on disk ----------------------------------------------------

    def _write_state(self) -> bool:
        assert self._state_path is not None
        snapshot = self._machine.to_dict()
        saved = statefile.save(self._state_path, snapshot)
        if saved:
            self._last_saved = snapshot
        return saved

    def _save_if_changed(self) -> None:
        """Save progress whenever it changed, telling the user once if that fails."""
        if self._state_path is None:
            return
        if self._machine.to_dict() == self._last_saved:
            return
        if not self._write_state() and not self._save_failed_shown:
            self._save_failed_shown = True
            self._say("state.save_failed")

    def _save_state_effect(self) -> list[Event]:
        """``SaveState``: save now and let the machine say how it went."""
        if self._state_path is None:
            return []
        saved = self._write_state()
        if not saved:
            # The machine says so itself; the background notice never repeats it.
            self._save_failed_shown = True
        return self._machine.on_event(StateSaved(saved))

    def _handle(self, answer: str) -> list[Event]:
        events = self._machine.handle(answer)
        self._save_if_changed()
        return events

    def _feed(self, result: Event) -> list[Event]:
        events = self._machine.on_event(result)
        self._save_if_changed()
        return events

    def _show_notices(self, events: list[Event]) -> None:
        """Render a notice; a turn reply goes back to the main loop instead."""
        if all(isinstance(event, Say) for event in events):
            for event in events:
                self._show(event)
            return
        raise _TurnReply(events)

    # -- the watcher ---------------------------------------------------------

    def _start_watcher(self) -> None:
        if self._watch_task is not None:
            return
        if self._watcher is None:
            self._watcher = AppWatcher(str(self._root / _APP_DIR_RELATIVE))
        self._watch_stop = asyncio.Event()
        self._watch_task = asyncio.ensure_future(self._watcher.run(self._watch_stop))

    async def _stop_watcher(self) -> None:
        task = self._watch_task
        if task is None:
            return
        assert self._watch_stop is not None
        self._watch_stop.set()
        await asyncio.wait({task}, timeout=APP_STOP_GRACE_S)
        await _cancel_and_wait(task)
        self._watch_task = None

    async def _before_spawn(self) -> None:
        """Right before every spawn: take the watcher's new baseline, and
        never start an app the user has already asked to leave behind."""
        if self._watcher is not None:
            await self._watcher.baseline()
        if self._quit_now.is_set():
            raise _QuitRequested

    # -- draining ------------------------------------------------------------

    def _drain_key(self, kind: DrainKind) -> str:
        """Which waiting line matches what the next Ctrl-C will do."""
        if kind == "run":
            return "drain.waiting_quit" if self._pressed else "drain.waiting"
        if kind == "reload":
            return "drain.waiting_quit" if self._prompt_up else "drain.waiting"
        return "drain.waiting_quit"

    def _begin_drain(self, kind: DrainKind) -> tuple[asyncio.Event, Callable[[int], None], _Drain]:
        cancel = asyncio.Event()
        if self._quit_now.is_set():
            cancel.set()
        if kind == "reload":
            self._pressed = False
        # With the interactive prompt up, Ctrl-C is a key that quits; a
        # signal that arrives anyway does the same, so the line never lies.
        self._drain_skippable = kind == "run" or (kind == "reload" and not self._prompt_up)
        self._drain_cancel = cancel
        shown = _Drain(time.monotonic())

        def on_wait(remaining: int) -> None:
            if shown.first is None:
                shown.first = Say(self._drain_key(kind), {"remaining": remaining})
                shown.total = remaining
                # Nothing is redrawn in place while the input line is open.
                self._renderer.drain_begin(shown.first, inline=not self._prompt_up)
            self._renderer.drain_progress(shown.update(time.monotonic(), remaining))

        return cancel, on_wait, shown

    def _end_drain(self, result: DrainResult | None, shown: _Drain) -> None:
        self._drain_cancel = None
        self._drain_skippable = False
        if shown.first is None:
            return
        if result is not None and result.outcome == "drained":
            # The app reports no count once its queue is empty: the last one
            # shown is made whole before the line ends.
            self._renderer.drain_progress(DeliverProgress(shown.total, shown.total, None))
            self._renderer.drain_end()
            self._say("drain.done")
            return
        self._renderer.drain_end()
        if result is None:
            return
        if result.outcome == "skipped" and not self._quit_now.is_set():
            self._say("drain.skipped", remaining=result.queued)
        elif result.outcome in _GAVE_UP:
            self._say("drain.gave_up", remaining=result.queued)

    async def _drain_now(self, kind: DrainKind) -> DrainResult | None:
        """Wait for the app's queued ``log()`` events, showing what it waits for."""
        control = self._app.control
        if control is None or not self._app.alive:
            return None
        cancel, on_wait, shown = self._begin_drain(kind)
        result: DrainResult | None = None
        try:
            result = await drain(control, cancel=cancel, on_wait=on_wait)
        finally:
            self._end_drain(result, shown)
        return result

    # -- waiting for input ------------------------------------------------------

    def _due_work(self) -> str | None:
        """What the app needs now: a respawn after a crash, or a reload after a change."""
        if self._app_up and not self._app.alive:
            return "crash"
        if self._watcher is not None and self._watcher.dirty:
            return "change"
        return None

    async def serve_due_work(self) -> None:
        """Serve every due restart before an automatic answer is handed over."""
        while not self._quit_now.is_set():
            work = self._due_work()
            if work is None:
                return
            await self._serve(work, None)

    async def read_while_serving(self, read: Awaitable[str | None]) -> str | None:
        """Read one answer, keeping the app in step with your files meanwhile.

        The read is started once and stays open while restarts come and go;
        their notices print above it. An answer that arrives during a restart
        is handed over only once that restart's result has been shown. A read
        that ends with nothing (Ctrl-C at the prompt, a hung-up terminal)
        quits at once, cutting short any drain under way.
        """
        read_task = asyncio.ensure_future(_read_or_none(read))
        self._prompt_up = self._interactive
        try:
            while True:
                if read_task.done():
                    answer = read_task.result()
                    if answer is None:
                        self._quit()
                    return answer
                if self._quit_now.is_set():
                    return None
                work = self._due_work()
                if work is not None:
                    await self._serve(work, read_task)
                    continue
                await self._wait_for_input_or_work(read_task)
        finally:
            self._prompt_up = False
            await _cancel_and_wait(read_task)

    async def _wait_for_input_or_work(self, read_task: asyncio.Future[str | None]) -> None:
        helpers: list[asyncio.Future[Any]] = [asyncio.ensure_future(self._quit_now.wait())]
        if self._app_up:
            helpers.append(asyncio.ensure_future(self._app.wait_exit()))
        if self._watcher is not None:
            helpers.append(asyncio.ensure_future(self._watcher.wait_dirty()))
        try:
            await asyncio.wait({read_task, *helpers}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for helper in helpers:
                await _cancel_and_wait(helper)

    async def _serve(self, work: str, read_task: asyncio.Future[str | None] | None) -> None:
        job = self._respawn() if work == "crash" else self._reload()
        result = await self._while_serving(asyncio.ensure_future(job), read_task)
        if result is _QUIT:
            return
        assert isinstance(result, (AppReloaded, AppDown))
        self._app_up = isinstance(result, AppReloaded)
        self._show_notices(self._feed(result))

    async def _while_serving(
        self, job: asyncio.Future[Event], read_task: asyncio.Future[str | None] | None
    ) -> Event | _Quit:
        quit_waiter = asyncio.ensure_future(self._quit_now.wait())
        try:
            while True:
                waiters: set[asyncio.Future[Any]] = {job, quit_waiter}
                if read_task is not None and not read_task.done():
                    waiters.add(read_task)
                await asyncio.wait(waiters, return_when=asyncio.FIRST_COMPLETED)
                if read_task is not None and read_task.done() and read_task.result() is None:
                    self._quit()
                if self._quit_now.is_set():
                    await _cancel_and_wait(job)
                    return _QUIT
                if job.done():
                    return job.result()
                # An answer arrived mid-restart: it waits for the result.
        finally:
            await _cancel_and_wait(quit_waiter)

    async def _respawn(self) -> Event:
        """The app stopped on its own: start it once more, without a drain."""
        generation = self._watcher.generation if self._watcher is not None else 0
        if self._respawned_generation == generation:
            # Its one automatic restart since the last change is used up.
            return AppDown(None)
        self._respawned_generation = generation
        try:
            await self._app.start(before_spawn=self._before_spawn)
        except AppStartError as error:
            return AppDown(error.location, error.failure)
        assert self._app.boot is not None
        return AppReloaded(self._app.boot.number, "crash")

    async def _reload(self) -> Event:
        """A watched file changed: drain, then restart the app on the new code."""
        assert self._watcher is not None
        await self._watcher.wait_settled()
        await self._drain_now("reload")
        if self._quit_now.is_set():
            raise _QuitRequested
        # The drain above already ran (or was skipped): the restart's own
        # drain is told to finish at once.
        drained = asyncio.Event()
        drained.set()
        try:
            boot = await self._app.restart(cancel=drained, before_spawn=self._before_spawn)
        except AppStartError as error:
            return AppDown(error.location, error.failure)
        return AppReloaded(boot.number, "change")

    # -- effects ------------------------------------------------------------

    async def _until_quit(self, awaitable: Awaitable[T]) -> T | _Quit:
        task = asyncio.ensure_future(awaitable)
        quit_waiter = asyncio.ensure_future(self._quit_now.wait())
        try:
            await asyncio.wait({task, quit_waiter}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            await _cancel_and_wait(quit_waiter)
        if self._quit_now.is_set():
            await _cancel_and_wait(task)
            return _QUIT
        return task.result()

    async def _start_app(self) -> list[Event] | _Quit:
        if not (self._root / _APP_MAIN_RELATIVE).is_file():
            return self._feed(AppFailed("app.failed"))
        # The watcher is running before the first spawn, so nothing saved
        # while the app starts is missed.
        self._start_watcher()
        try:
            started = await self._until_quit(self._app.start(before_spawn=self._before_spawn))
        except AppStartError as error:
            self._app_up = False
            return self._feed(AppDown(error.location, error.failure))
        except _QuitRequested:
            return _QUIT
        if started is _QUIT:
            return _QUIT
        self._app_up = True
        return self._feed(AppReady())

    async def _run_chapter1(self) -> list[Event] | _Quit:
        async with contextlib.AsyncExitStack() as stack:
            try:
                held = await stack.enter_async_context(self._app.hold())
            except AppUnavailable:
                # The app stopped between the question and /start: this run
                # never starts, and waiting for input restarts the app.
                return self._feed(RunInvalidated())
            return await self._run_held(held)

    async def _run_held(self, held: Hold) -> list[Event] | _Quit:
        boot = self._app.boot
        assert boot is not None
        self._stop_run.clear()
        self._pressed = False
        self._run_active = True
        plan = build_offline_plan(CHAPTER1_VISITORS, seed=secrets.randbits(64))
        self._renderer.run_started(len(plan))
        try:
            result = await self._drive_run(plan, boot.base_url, boot.token, held)
        finally:
            self._run_active = False
            self._renderer.run_ended()
        if result is _QUIT:
            return _QUIT

        await self._drain_now("run")
        if self._quit_now.is_set():
            return _QUIT

        if not await held.intact():
            outcome: Event = RunInvalidated()
        elif result.stopped:
            outcome = RunStopped(result.done, result.total, result.actions, result.successes)
        else:
            outcome = self._stamped(
                RunFinished(
                    result.total, result.actions, result.successes, result.attack, result.real
                )
            )
        # Before the tally, which ends with the close question, is shown.
        self._effect_ended()
        events = self._feed(outcome)
        # The tally is shown while this boot is still held; a restart the
        # run deferred only starts once the machine waits for input again.
        while events and isinstance(events[0], Say):
            self._show(events.pop(0))
        return events

    async def _drive_run(
        self, plan: list[Any], base_url: str, token: str, held: Hold
    ) -> RunResult | _Quit:
        def on_progress(progress: Progress) -> None:
            self._renderer.progress(
                RunProgress(progress.done, progress.total, progress.actions, progress.successes)
            )

        run_task = asyncio.ensure_future(
            run_offline(
                plan,
                base_url,
                token,
                on_progress=on_progress,
                stop=self._stop_run,
                run_id=secrets.token_hex(8),
            )
        )
        quit_waiter = asyncio.ensure_future(self._quit_now.wait())
        invalidated = asyncio.ensure_future(held.invalidated.wait())
        changed = (
            asyncio.ensure_future(self._watcher.wait_dirty()) if self._watcher is not None else None
        )
        try:
            while not run_task.done():
                waiters: set[asyncio.Future[Any]] = {run_task, quit_waiter}
                waiters.update(w for w in (invalidated, changed) if w is not None and not w.done())
                await asyncio.wait(waiters, return_when=asyncio.FIRST_COMPLETED)
                if quit_waiter.done():
                    await _cancel_and_wait(run_task)
                    return _QUIT
                if invalidated.done():
                    # The boot this run holds is gone: no new visitors start.
                    self._stop_run.set()
                if changed is not None and changed.done():
                    # Once per run: the restart waits until the run is over.
                    changed = None
                    for event in self._feed(ChangeDetected()):
                        self._show(event)
            return run_task.result()
        finally:
            for waiter in (quit_waiter, invalidated, changed):
                await _cancel_and_wait(waiter)
            await _cancel_and_wait(run_task)

    # -- the session --------------------------------------------------------

    async def _loop(self) -> int:
        pending: list[Event] = list(self._machine.start())
        while pending and not self._quit_now.is_set():
            event = pending.pop(0)

            if isinstance(event, StartApp):
                reply = await self._start_app()
                if isinstance(reply, _Quit):
                    return 0
                self._effect_ended()
                pending = reply + pending
                continue

            if isinstance(event, RunChapter1):
                self._start_clock()
                reply = await self._run_chapter1()
                if isinstance(reply, _Quit):
                    return 0
                pending = reply + pending
                continue

            if isinstance(event, SaveState):
                pending = self._save_state_effect() + pending
                continue

            if isinstance(event, _ONLINE_EFFECTS):
                reply = await self._online_effect(event)
                if isinstance(reply, _Quit):
                    return 0
                self._effect_ended()
                pending = reply + pending
                continue

            if isinstance(event, ChapterDone):
                if self._until is not None and event.chapter >= self._until:
                    return 0
                continue

            if isinstance(event, Exit):
                self._show(event)
                return event.code

            self._show(event)
            if isinstance(event, AwaitInput):
                try:
                    answer = await _next_answer(
                        event.expect,
                        read_line=self._read_line,
                        yes=self._yes,
                        until=self._until,
                        quit_now=self._quit_now,
                        idle=self,
                        decline=self._machine.unattended_no,
                    )
                except _TurnReply as reply:
                    pending = reply.events + pending
                    continue
                if answer is None:
                    return 0
                pending = self._handle(answer) + pending
        return 0

    async def _stop_app(self) -> None:
        """Stop the app over HTTP first, draining unless the user quit now."""
        quit_now = self._quit_now.is_set()
        cancel, on_wait, shown = self._begin_drain("exit")
        result: StopResult | None = None
        try:
            result = await self._app.stop(drain=not quit_now, cancel=cancel, on_drain=on_wait)
        finally:
            self._end_drain(result.drain if result is not None else None, shown)

    def _final_save(self) -> None:
        """Save progress on the way out, unless the file already holds it."""
        if self._state_path is None:
            return
        try:
            if self._machine.to_dict() == self._last_saved:
                return
            saved = self._write_state()
        except Exception:  # noqa: BLE001 - the way out must go on whatever happened
            saved = False
        if not saved and not self._save_failed_shown:
            self._save_failed_shown = True
            self._say("state.save_failed")

    async def run(self) -> int:
        loop = asyncio.get_running_loop()
        signals = _Signals(loop)
        signals.install(signal.SIGINT, self.on_sigint)
        signals.install(signal.SIGTERM, self.on_terminate)
        sighup = getattr(signal, "SIGHUP", None)
        if sighup is not None:
            signals.install(sighup, self.on_terminate)

        self._started_at = time.monotonic()
        # What's on disk already needs no save until something changes.
        self._last_saved = self._machine.to_dict() if self._state_path is not None else None
        exit_code = 0
        try:
            try:
                exit_code = await self._loop()
            except KeyboardInterrupt:
                # Belt and braces for a Ctrl-C that arrived as an exception
                # (no handler could be installed on this platform or thread).
                self._quit()
                exit_code = 0
        finally:
            try:
                await self._stop_watcher()
                await self._stop_app()
                self._final_save()
            finally:
                # Last, so a Ctrl-C during the drain on the way out still
                # cuts it short.
                signals.remove()
        return exit_code


async def run(
    machine: Machine,
    renderer: Renderer,
    read_line: ReadLine,
    *,
    root: Path,
    yes: bool = False,
    until: int | None = None,
    state_path: Path | None = None,
    interactive: bool = False,
    app: AppProcess | None = None,
    watcher: AppWatcher | None = None,
    api_transport: httpx.AsyncBaseTransport | None = None,
    telemetry: bool = True,
    discard_typeahead: Callable[[], None] | None = None,
) -> int:
    """Drive ``machine`` to completion, rendering through ``renderer``.

    ``state_path`` is where progress is saved (None: never saved, as in an
    unattended ``--yes`` session). ``interactive`` says whether input comes
    from the interactive prompt, where Ctrl-C is a key rather than a signal.
    ``app`` and ``watcher`` default to the real sample app under ``root`` and
    a watcher over its ``app/`` directory. ``api_transport`` is for tests
    only: the playground API client's transport (None: the network).
    ``telemetry`` False keeps finding IDs and results from SignalGate.
    ``discard_typeahead`` (interactive input only) drops the keys typed while
    an effect or a run went on; it is called each time one ends, before its
    outcome is shown.

    Always stops the sample app on the way out, however the session ends.
    """
    session = _Session(
        machine,
        renderer,
        read_line,
        root=root,
        yes=yes,
        until=until,
        state_path=state_path,
        interactive=interactive,
        app=app,
        watcher=watcher,
        api_transport=api_transport,
        telemetry=telemetry,
        discard_typeahead=discard_typeahead,
    )
    return await session.run()


__all__ = ["CHAPTER1_VISITORS", "STATE_RELATIVE", "ReadLine", "find_repo_root", "run"]
