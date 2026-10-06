"""The console's state machine: the opening, chapter 1 and, through
``playground.machine.steps``, chapters 2-6.

``Machine`` is pure: it holds only small pieces of state (whether the sample
app is up, whether a run is active, what input it is waiting for, which step
of the tutorial it is on) and turns user text or runner results into the
events in ``playground.machine.events``. It never does I/O, never sleeps and
never draws anything; a renderer turns its events into output, and a runner
carries out its effects.
"""

from __future__ import annotations

import typing
from collections.abc import Mapping
from typing import Any

from playground.copy.en import text
from playground.machine.events import (
    ANSWERS,
    COMMANDS,
    EFFECTS,
    AppDown,
    AppFailed,
    AppReady,
    AppReloaded,
    AwaitInput,
    ChangeDetected,
    ChapterDone,
    Event,
    Exit,
    Expect,
    RunChapter1,
    RunFinished,
    RunInvalidated,
    RunStopped,
    SaveState,
    Say,
    ShowLogo,
    StartApp,
    StateSaved,
    StepDone,
)
from playground.machine.steps import OnlineSteps, Step, question

# Positional views onto the frozen command/answer tuples, so the dispatch
# logic below never spells a command or answer out as a literal of its own.
(
    _CMD_START,
    _CMD_CONTINUE,
    _CMD_HINT,
    _CMD_STATUS,
    _CMD_HELP,
    _CMD_REPORT,
    _CMD_QUIT,
    _CMD_COPY,
) = COMMANDS
_ANSWER_YES, _ANSWER_NO = ANSWERS

# Commands that still work while a run is in progress.
_ALWAYS_AVAILABLE = frozenset({_CMD_HELP, _CMD_STATUS, _CMD_HINT, _CMD_QUIT})

_CHAPTER_1 = 1

# The close question says how many visitors in ten were the attack.
_IN_TEN = 10

# ``Expect`` values a saved snapshot can be waiting on that make it worth
# resuming, alongside ``running`` and ``chapter1_done``.
_OFFER_AWAITING = ("yes_no", "any", "continue")


def _say(key: str, **params: Any) -> Say:
    """Build a ``Say`` event, so call sites read as ``key`` plus its params."""
    return Say(key, params)


def _is_offered(machine: Machine) -> bool:
    """True iff ``machine``'s shape is worth offering the user to resume."""
    return machine._running or machine._chapter1_done or machine._awaiting in _OFFER_AWAITING


class Machine(OnlineSteps):
    """The console's whole flow, chapter by chapter, with no I/O of its own."""

    def __init__(
        self,
        offline_total: int = 10_000,
        *,
        saved: Mapping[str, Any] | None = None,
        unattended: bool = False,
        size: int = 2000,
    ) -> None:
        self._offline_total = offline_total
        self._running = False
        self._awaiting: Expect | None = None
        self._chapter1_done = False
        self._last_conversion: float | None = None
        self._last_attack_in_ten: int | None = None
        self._unattended = unattended
        self._init_online(size)

        # Runner-only flags: never serialised through to_dict()/from_dict().
        self._app_up = False
        self._opened = False
        self._change_noticed = False

        # A saved snapshot offered for /continue, until the user chooses.
        self._offer: Machine | None = Machine.from_dict(saved) if saved is not None else None

    # -- entry point -----------------------------------------------------

    def start(self) -> list[Event]:
        """Show the logo and kick off the sample app."""
        return [ShowLogo(), _say("app.starting"), StartApp()]

    # -- user text ---------------------------------------------------------

    def handle(self, input_text: str) -> list[Event]:
        """Turn one line of user input into the events it produces.

        Unless the reply starts a run or another effect, or ends the session,
        it finishes by handing the input line back, so the same question
        stays open.
        """
        events = self._handle(input_text)
        if self._running or self._awaiting is None:
            return events
        if events and isinstance(events[-1], (AwaitInput, Exit, *EFFECTS)):
            return events
        return [*events, AwaitInput(self._awaiting)]

    def _handle(self, input_text: str) -> list[Event]:
        stripped = input_text.strip()

        if self._running and stripped not in _ALWAYS_AVAILABLE:
            return [_say("reply.busy")]

        # /copy takes an argument, so it is matched on its first word, before
        # the whole-line commands (a bare /copy is one of them too).
        head, *index = stripped.split(maxsplit=1) or [stripped]
        if head == _CMD_COPY:
            return self._online_copy(*index)

        if stripped in COMMANDS:
            return self._handle_command(stripped)

        answer = stripped.lower()
        if self._awaiting == "continue":
            # Enter goes on, and so does yes, as it always did.
            if not answer or answer == _ANSWER_YES:
                return self._answer(yes=True)
            if answer == _ANSWER_NO:
                return self._answer(yes=False)
        elif self._awaiting == "yes_no":
            # A yes/no question has no default: an empty line asks it again.
            if not answer:
                return self._ask_again()
            if answer in ANSWERS:
                return self._answer(yes=answer == _ANSWER_YES)

        return [_say("reply.unknown")]

    def _answer(self, *, yes: bool) -> list[Event]:
        if self._step is not None:
            return self._online_answer(yes)
        return self._handle_answer(yes)

    def _ask_again(self) -> list[Event]:
        """The open yes/no question once more, exactly as it was asked."""
        if self._step is not None:
            return self._ask(self._step)
        return [self._close_question(), AwaitInput("yes_no")]

    def _close_question(self) -> Say:
        return question(
            "ch1.close",
            conversion=self._last_conversion,
            attack_in_ten=self._last_attack_in_ten,
        )

    def _handle_command(self, command: str) -> list[Event]:
        if command == _CMD_HELP:
            return [_say("help")]
        if command == _CMD_HINT:
            return self._hint_says()
        if command == _CMD_STATUS:
            return self._status_events()
        if command == _CMD_QUIT:
            return self._handle_quit()
        if command == _CMD_CONTINUE:
            return self._handle_continue()
        if command == _CMD_REPORT:
            return self._online_report()
        if command == _CMD_START:
            if self._awaiting == "start":
                if not self._app_up:
                    return [_say("app.not_ready"), AwaitInput("start")]
                return self._start_chapter1()
            return [_say("reply.unknown")]
        # COMMANDS is exhausted above; /copy never gets here (see _handle).
        raise AssertionError(command)

    def _handle_answer(self, yes: bool) -> list[Event]:
        if not yes:
            assert self._awaiting is not None
            return [_say("reply.no"), AwaitInput(self._awaiting)]
        self._chapter1_done = True
        return [
            *self._achievement("achievement.ch1"),
            ChapterDone(_CHAPTER_1),
            *self._enter_chapter2(),
        ]

    def _handle_quit(self) -> list[Event]:
        if self._unattended:
            return [_say("console.bye"), Exit(0)]
        return [SaveState(), _say("console.bye"), Exit(0)]

    def _handle_continue(self) -> list[Event]:
        offer = self._offer
        if offer is None or not _is_offered(offer):
            within = self._online_continue()
            return within if within is not None else [_say("continue.none")]
        self._offer = None
        self._chapter1_done = offer._chapter1_done
        self._last_conversion = offer._last_conversion
        self._last_attack_in_ten = offer._last_attack_in_ten
        OnlineSteps.from_dict(self, OnlineSteps.to_dict(offer))
        if offer._step is not None:
            return self._resume_step(offer._step)
        if offer._running:
            self._running = False
            self._awaiting = "start"
            return [_say("continue.run_interrupted"), _say("run.again"), AwaitInput("start")]
        if offer._awaiting == "yes_no":
            self._running = False
            self._awaiting = "yes_no"
            return [_say("continue.back"), self._close_question(), AwaitInput("yes_no")]
        if offer._awaiting == "any" and offer._chapter1_done:
            # Chapter 1 done and nothing after it: chapter 2 is next, as for
            # a version 1 save.
            return self._resume_step(Step.KEY)
        if offer._awaiting == "start" and offer._chapter1_done:
            self._running = False
            self._awaiting = "start"
            return [_say("continue.back"), AwaitInput("start")]
        return [_say("continue.none")]

    def _start_chapter1(self) -> list[Event]:
        ended = self._end_offered_session(self._offer)
        self._offer = None
        self._awaiting = None
        self._running = True
        self._change_noticed = False
        return [
            *ended,
            _say("chapter.heading", chapter=_CHAPTER_1, title=text("chapter.1")),
            _say("ch1.intro", total=self._offline_total),
            RunChapter1(),
        ]

    def _hint_says(self) -> list[Event]:
        if self._step is not None:
            return self._online_hint()
        if self._running:
            return [_say("hint.ch1.running")]
        if self._awaiting == "yes_no":
            return [_say("hint.ch1.close")]
        return [_say("hint.opening")]

    def _status_events(self) -> list[Event]:
        if self._step is not None:
            return self._online_status()
        if self._running or self._awaiting == "yes_no":
            return [
                _say("status.chapter", chapter=_CHAPTER_1, title=text("chapter.1")),
                _say("status.done_none"),
            ]
        if self._chapter1_done:
            return [_say("status.done_ch1", conversion=self._last_conversion)]
        return [_say("status.not_started"), _say("status.done_none")]

    # -- runner/effect results --------------------------------------------

    def on_event(self, event: Event) -> list[Event]:
        """Handle a result the runner fed back from an effect."""
        if isinstance(event, AppReady):
            return self._on_app_ready()
        if isinstance(event, AppFailed):
            return [_say(event.reason), Exit(3)]
        if isinstance(event, RunFinished):
            return self._on_run_finished(event)
        if isinstance(event, RunStopped):
            return self._on_run_stopped(event)
        if isinstance(event, AppReloaded):
            return self._on_app_reloaded(event)
        if isinstance(event, AppDown):
            return self._on_app_down(event)
        if isinstance(event, ChangeDetected):
            return self._on_change_detected()
        if isinstance(event, RunInvalidated):
            return self._on_run_invalidated()
        if isinstance(event, StateSaved):
            return self._on_state_saved(event)
        if isinstance(event, StepDone):
            return self._on_step_done(event)
        raise ValueError(event)

    def _on_app_ready(self) -> list[Event]:
        self._app_up = True
        self._opened = True
        self._awaiting = "start"
        if self._offer_pending():
            return [_say("app.ready"), _say("continue.offer"), AwaitInput("start")]
        return [_say("app.ready"), _say("opening"), AwaitInput("start")]

    def _on_run_finished(self, event: RunFinished) -> list[Event]:
        self._running = False
        self._stamp(event.elapsed_s)
        if event.actions == 0:
            # Not one action went through: the sample app stopped answering.
            if self._unattended:
                return [_say("app.crashed"), Exit(2)]
            self._awaiting = "start"
            return [_say("app.crashed"), _say("run.again"), AwaitInput("start")]
        self._awaiting = "yes_no"
        conversion = event.successes / event.actions
        attack_in_ten = round(_IN_TEN * event.attack / event.total)
        self._last_conversion = conversion
        self._last_attack_in_ten = attack_in_ten
        return [self._close_question(), AwaitInput("yes_no")]

    def _on_run_stopped(self, event: RunStopped) -> list[Event]:
        self._running = False
        self._awaiting = "start"
        return [
            _say("ch1.stopped", done=event.done, total=event.total),
            AwaitInput("start"),
        ]

    def _on_app_reloaded(self, event: AppReloaded) -> list[Event]:
        was_up, self._app_up = self._app_up, True
        if event.cause == "change":
            # A save the app picked up is worth a line where the user edits
            # the code, anywhere once it fixes an app that didn't start, and
            # anywhere a run said the reload would wait for it.
            deferred, self._change_noticed = self._change_noticed, False
            if not was_up or deferred or self._is_edit_step():
                return [_say("app.reloaded")]
            return []
        if event.cause == "request":
            return [_say("app.restarted_key")]
        return [_say("app.restarted")]

    def _on_app_down(self, event: AppDown) -> list[Event]:
        self._app_up = False
        down = self._down_says(event)
        if self._unattended:
            self._opened = True
            return [*down, Exit(2)]
        if not self._opened:
            self._opened = True
            self._awaiting = "start"
            if self._offer_pending():
                return [*down, _say("continue.offer"), AwaitInput("start")]
            return [*down, _say("opening"), AwaitInput("start")]
        return down

    def _down_says(self, event: AppDown) -> list[Event]:
        """Why the app didn't start: the error, its line, a hint, and how to fix it."""
        failure = event.failure
        if failure is None:
            if event.location is not None:
                return [_say("app.down_at", location=event.location)]
            return [_say("app.down")]
        says: list[Event] = [_say("app.down.error", error=failure.error, location=failure.location)]
        says += [
            _say("app.down.code", code=line) for line in (failure.source, failure.caret) if line
        ]
        if failure.hint is not None:
            says.append(_say(failure.hint))
        says.append(_say("app.down.fix"))
        return says

    def _on_change_detected(self) -> list[Event]:
        if self._running and not self._change_noticed:
            self._change_noticed = True
            return [_say("reload.deferred")]
        return []

    def _on_run_invalidated(self) -> list[Event]:
        self._running = False
        self._awaiting = "start"
        return [_say("run.invalidated"), _say("run.again"), AwaitInput("start")]

    def _on_state_saved(self, event: StateSaved) -> list[Event]:
        if not event.ok:
            return [_say("state.save_failed")]
        if self._is_resumable():
            return [_say("console.saved")]
        return []

    # -- offer / resume helpers --------------------------------------------

    def _offer_pending(self) -> bool:
        return self._offer is not None and _is_offered(self._offer)

    def _is_resumable(self) -> bool:
        # While an offer is pending, the saved snapshot is what to_dict()
        # hands the save, and a pending offer is resumable by definition.
        return self._offer_pending() or _is_offered(self)

    # -- state -------------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        """Serialise the machine's state to a plain, JSON-safe dict.

        While a saved snapshot is offered, this returns that snapshot
        unchanged, so the question on screen and the state on disk agree
        until the user picks /continue or /start.
        """
        if self._offer_pending():
            return self._offer.to_dict()  # type: ignore[union-attr]
        return {
            "offline_total": self._offline_total,
            "running": self._running,
            "awaiting": self._awaiting,
            "chapter1_done": self._chapter1_done,
            "last_conversion": self._last_conversion,
            "last_attack_in_ten": self._last_attack_in_ten,
            **OnlineSteps.to_dict(self),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Machine:
        """Rebuild a ``Machine`` from a dict produced by ``to_dict``.

        Raises ``ValueError`` when ``data`` isn't a shape ``to_dict`` could
        have produced: a missing, extra or mistyped key, an ``awaiting`` value
        that isn't one of ``Expect``'s, a number outside the range a finished
        run can report, or a combination the console couldn't show.
        """
        # Every bad shape, mistyped fields included, raises ValueError alike:
        # to the caller it is one "set the saved file aside" signal.
        try:
            offline_total = data["offline_total"]
            running = data["running"]
            awaiting = data["awaiting"]
            chapter1_done = data["chapter1_done"]
            last_conversion = data["last_conversion"]
            last_attack_in_ten = data["last_attack_in_ten"]
            keys = set(data)
        except (KeyError, TypeError, AttributeError) as exc:
            raise ValueError("bad machine snapshot: missing key") from exc
        # Only the keys to_dict() writes, so a machine rebuilt from ``data``
        # serialises back to exactly ``data``; a snapshot saved before the
        # achievements and the time were kept has neither of those two.
        expected = set(cls().to_dict())
        if keys != expected and keys != expected - {"achievements", "elapsed_s"}:
            raise ValueError("bad machine snapshot: unexpected key")

        if not isinstance(offline_total, int) or isinstance(offline_total, bool):
            raise ValueError("bad machine snapshot: offline_total")  # noqa: TRY004
        if not isinstance(running, bool):
            raise ValueError("bad machine snapshot: running")  # noqa: TRY004
        if awaiting is not None and awaiting not in typing.get_args(Expect):
            raise ValueError("bad machine snapshot: awaiting")
        if not isinstance(chapter1_done, bool):
            raise ValueError("bad machine snapshot: chapter1_done")  # noqa: TRY004
        if last_conversion is not None:
            # A conversion is successes over actions: a number in [0, 1].
            if not isinstance(last_conversion, (int, float)) or isinstance(last_conversion, bool):
                raise ValueError("bad machine snapshot: last_conversion")
            try:
                last_conversion = float(last_conversion)
            except OverflowError as exc:  # an integer too large for a float
                raise ValueError("bad machine snapshot: last_conversion") from exc
            # NaN fails both comparisons, so it is rejected with the infinities.
            if not 0.0 <= last_conversion <= 1.0:
                raise ValueError("bad machine snapshot: last_conversion")
        if last_attack_in_ten is not None and (
            not isinstance(last_attack_in_ten, int)
            or isinstance(last_attack_in_ten, bool)
            or not 0 <= last_attack_in_ten <= _IN_TEN
        ):
            raise ValueError("bad machine snapshot: last_attack_in_ten")

        # The fields aren't only individually well-typed: a close question
        # needs both numbers to render, and a completed chapter 1 needs its
        # conversion. Either combination missing a number would otherwise
        # load cleanly and then crash the renderer the next time it's shown.
        if awaiting == "yes_no" and (last_conversion is None or last_attack_in_ten is None):
            raise ValueError("bad machine snapshot: yes_no needs both numbers")
        if chapter1_done and last_conversion is None:
            raise ValueError("bad machine snapshot: chapter1_done needs a conversion")

        machine = cls(offline_total=offline_total)
        machine._running = running
        machine._awaiting = awaiting
        machine._chapter1_done = chapter1_done
        machine._last_conversion = last_conversion
        machine._last_attack_in_ten = last_attack_in_ten
        OnlineSteps.from_dict(machine, data)
        if machine._step is not None and not chapter1_done:
            raise ValueError("bad machine snapshot: a later chapter needs chapter 1 done")
        return machine


__all__ = ["Machine"]
