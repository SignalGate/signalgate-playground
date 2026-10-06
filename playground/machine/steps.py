"""Chapters 2-6: the steps after chapter 1, as a mixin of ``Machine``.

Each step is one question the console asks (``Step``), of one of two
kinds (``_KINDS``): a "continue" wait, where Enter or ``yes`` goes on, or a
yes/no question, where only ``yes`` or ``no`` counts. Going on starts the
effect that checks or does what the question is about; its outcome comes
back as one ``StepDone``, and the step it arrived for decides what follows:
the next question, the next effect, or the same question again with what
went wrong. On a PASS the console moves on by itself.

A step that completes something the user worked for also earns an
achievement line, once per session (``_achievement``), and the console
keeps the active time it measured since ``/start`` for the completion card.

When an effect finds the playground session closed (it expired, or a newer
one replaced it), the step stays where it is and a "continue" wait offers to
carry on: Enter or ``yes`` opens a new session, with no new consent, and
runs that same effect again. Progress, the chapter and the achievements
stay as they were. An unattended run stops there instead.

Like the rest of the machine, nothing here does I/O: the runner carries out
the effects and puts their findings into words (``StepDone.says``); the
steps add the chapter's own narration and questions.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import asdict, fields
from enum import Enum, auto
from typing import Any

from playground import CLIENT_VERSION
from playground.api import decode
from playground.copy.en import TASKS, text
from playground.machine.events import (
    AwaitInput,
    ChapterDone,
    CheckKey,
    Complete,
    CopySnippets,
    EndSession,
    Event,
    Exit,
    Expect,
    Gate,
    OpenSession,
    Phase,
    Prediction,
    ReadStage,
    RunGate,
    RunReadiness,
    RunStats,
    RunTraffic,
    Say,
    StepDone,
    WaitWorkflow,
    WriteReport,
)

_STAGES_TOTAL = 3
_ROUNDS_MAX = 2


class Step(Enum):
    """The questions of chapters 2-6, in order."""

    KEY = auto()
    CONSENT = auto()
    SESSION = auto()
    CODE3 = auto()
    LEARN = auto()
    TOPUP = auto()
    DRY_RUN = auto()
    START = auto()
    PROTECT_WAIT = auto()
    CODE5 = auto()
    READINESS = auto()
    ROUND = auto()
    RESULTS = auto()
    RERUN_OFFER = auto()
    RERUN_LATER = auto()
    RERUN_ANYWAY = auto()
    RERUN_PAUSED = auto()
    KEEP = auto()
    DONE = auto()


CHAPTER_OF: dict[Step, int] = {
    Step.KEY: 2,
    Step.CONSENT: 2,
    Step.SESSION: 2,
    Step.CODE3: 3,
    Step.LEARN: 3,
    Step.TOPUP: 3,
    Step.DRY_RUN: 4,
    Step.START: 4,
    Step.PROTECT_WAIT: 5,
    Step.CODE5: 5,
    Step.READINESS: 5,
    Step.ROUND: 5,
    Step.RESULTS: 6,
    Step.RERUN_OFFER: 6,
    Step.RERUN_LATER: 6,
    Step.RERUN_ANYWAY: 6,
    Step.RERUN_PAUSED: 6,
    Step.KEEP: 6,
    Step.DONE: 6,
}

_CHAPTER_TITLES = {
    2: "chapter.2",
    3: "chapter.3",
    4: "chapter.4",
    5: "chapter.5",
    6: "chapter.6",
}

# What kind of answer each step's question waits for: "continue" (Enter or
# yes goes on), "yes_no" (only yes or no counts; an empty line asks again) or
# "any" (only commands mean something).
_KINDS: dict[Step, Expect] = {
    Step.KEY: "continue",
    Step.CONSENT: "yes_no",
    Step.SESSION: "continue",
    Step.CODE3: "continue",
    Step.LEARN: "continue",
    Step.TOPUP: "yes_no",
    Step.DRY_RUN: "continue",
    Step.START: "continue",
    Step.PROTECT_WAIT: "continue",
    Step.CODE5: "continue",
    Step.READINESS: "continue",
    Step.ROUND: "continue",
    Step.RESULTS: "continue",
    Step.RERUN_OFFER: "yes_no",
    Step.RERUN_LATER: "any",
    Step.RERUN_ANYWAY: "yes_no",
    Step.RERUN_PAUSED: "continue",
    Step.KEEP: "yes_no",
    Step.DONE: "any",
}

_PROMPTS: dict[Step, str] = {
    Step.KEY: "ch2.key.ask",
    Step.CONSENT: "ch2.consent",
    Step.SESSION: "ch2.session.retry",
    Step.CODE3: "ch3.ask",
    Step.LEARN: "ch3.learn.again",
    Step.DRY_RUN: "ch4.ask",
    Step.START: "ch4.start",
    Step.PROTECT_WAIT: "ch5.wait.again",
    Step.CODE5: "ch5.ask",
    Step.READINESS: "ch5.readiness.again",
    Step.ROUND: "ch5.round.again",
    Step.RESULTS: "ch6.ask",
    Step.RERUN_OFFER: "ch6.rerun.offer",
    Step.RERUN_LATER: "ch6.rerun.later",
    Step.RERUN_ANYWAY: "ch6.rerun.anyway",
    Step.RERUN_PAUSED: "wf.paused",
    Step.KEEP: "ch6.keep",
    Step.DONE: "ch6.done",
}
_PROMPTS_AGAIN = {Step.CODE3: "ch3.again", Step.CODE5: "ch5.again"}

_HINTS: dict[Step, tuple[str, ...]] = {
    Step.KEY: ("hint.ch2.key",),
    Step.CONSENT: ("hint.ch2.consent",),
    Step.SESSION: ("hint.ch2.session",),
    Step.CODE3: ("hint.ch3.1", "hint.ch3.2", "hint.ch3.3"),
    Step.LEARN: ("hint.ch3.learn",),
    Step.TOPUP: ("hint.ch3.topup",),
    Step.DRY_RUN: ("hint.ch4.dry_run",),
    Step.START: ("hint.ch4.start",),
    Step.PROTECT_WAIT: ("hint.ch5.wait",),
    Step.CODE5: ("hint.ch5.1", "hint.ch5.2", "hint.ch5.3"),
    Step.READINESS: ("hint.ch5.readiness",),
    Step.ROUND: ("hint.ch5.round",),
    Step.RESULTS: ("hint.ch6.results",),
    Step.RERUN_OFFER: ("hint.ch6.rerun",),
    Step.RERUN_LATER: ("hint.ch6.rerun",),
    Step.RERUN_ANYWAY: ("hint.ch6.rerun",),
    Step.RERUN_PAUSED: ("hint.ch5.wait",),
    Step.KEEP: ("hint.ch6.keep",),
    Step.DONE: ("hint.done",),
}

# The hint levels that show the exact code: the chapter's "Your turn" block follows.
_HINT_TASKS = {"hint.ch3.3": "ch3.task", "hint.ch5.3": "ch5.task"}

# The chapters whose steps have code to copy, and the task that holds it.
_TASK_OF_CHAPTER = {3: "ch3.task", 5: "ch5.task"}

# What a code step shows again when a saved session is picked up there, so
# the "Your turn" block is on screen before its question.
_RESUME_SAYS: dict[Step, tuple[str, ...]] = {
    Step.CODE3: ("ch3.intro", "ch3.task"),
    Step.CODE5: ("ch5.task",),
}

# The chapters in which the user edits app/routes.py: there a save that the
# sample app picked up is worth a line.
_EDIT_CHAPTERS = frozenset({3, 5})

# What a session can achieve, each shown once when it is earned.
_ACHIEVEMENTS = (
    "achievement.ch1",
    "achievement.connected",
    "achievement.ch3",
    "achievement.learned",
    "achievement.prediction",
    "achievement.ch5",
    "achievement.results",
)

# The questions an unattended run answers no: declining the re-run offer
# goes on to the keep question, so the session still completes.
_UNATTENDED_NO = frozenset({Step.RERUN_OFFER})

# The steps whose yes starts the chapter 5 checks again before a traffic run.
_ROUND_STEPS = frozenset({Step.CODE5, Step.READINESS, Step.ROUND})
_RERUN_STEPS = frozenset({Step.RERUN_LATER, Step.RERUN_ANYWAY, Step.RERUN_PAUSED})
# The steps after the results were shown.
_AFTER_RESULTS = frozenset({Step.RERUN_OFFER, *_RERUN_STEPS, Step.KEEP, Step.DONE})


def _say(key: str, **params: Any) -> Say:
    return Say(key, params)


def question(key: str, **params: Any) -> Say:
    """The ``Say`` for a question right before the input line."""
    return Say(key, params, block="ask")


def heading(chapter: int) -> Say:
    """The heading that opens ``chapter``."""
    return _say("chapter.heading", chapter=chapter, title=text(_CHAPTER_TITLES[chapter]))


def stage_say(stage: int | None) -> Say | None:
    """The frozen line for a protection stage, or None when it isn't known."""
    if stage is None:
        return None
    if stage <= 0:
        return _say("stage.0")
    if stage >= _STAGES_TOTAL:
        return _say("stage.full")
    return _say("stage.partial", stage=stage)


def _conversion(stats: RunStats) -> float:
    return stats.successes / stats.actions if stats.actions else 0.0


def _snippet_number(index: str) -> int | None:
    """``index`` as the number ``/copy N`` names, or None when it isn't one.

    Only plain ASCII digits count. A number too long for ``int()`` to read is
    no snippet's number either.
    """
    if not (index.isascii() and index.isdigit()):
        return None
    try:
        return int(index)
    except ValueError:
        return None


# -- saved progress -----------------------------------------------------------------------
# Each ``from_dict`` below rebuilds one saved value and raises ``ValueError``
# for any shape ``OnlineSteps.to_dict`` couldn't have written.


def _count(value: Any) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError
    return value


def _number(value: Any) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise ValueError  # noqa: TRY004 - every bad shape is one "set it aside" signal
    number = float(value)
    if not math.isfinite(number) or number < 0:
        raise ValueError
    return number


def _earned(value: Any) -> set[str]:
    """The achievements a snapshot saved: a sorted list of known keys, each once."""
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError
    if value != sorted(set(value)) or not set(value) <= set(_ACHIEVEMENTS):
        raise ValueError
    return set(value)


def _keys_of(data: Any, cls: Any) -> Mapping[str, Any]:
    if not isinstance(data, Mapping) or set(data) != {f.name for f in fields(cls)}:
        raise ValueError
    return data


class SavedStats:
    @staticmethod
    def from_dict(data: Any) -> RunStats:
        if isinstance(data, Mapping) and "degraded" not in data:
            data = {**data, "degraded": False}  # saved before runs carried the mark
        data = _keys_of(data, RunStats)
        if not isinstance(data["degraded"], bool):
            raise ValueError("bad machine snapshot: degraded")  # noqa: TRY004 - as above
        return RunStats(
            total=_count(data["total"]),
            actions=_count(data["actions"]),
            successes=_count(data["successes"]),
            real_blocked=_count(data["real_blocked"]),
            real_total=_count(data["real_total"]),
            check_ms=None if data["check_ms"] is None else _number(data["check_ms"]),
            inside_ms=None if data["inside_ms"] is None else _number(data["inside_ms"]),
            degraded=data["degraded"],
        )


class SavedPrediction:
    @staticmethod
    def from_dict(data: Any) -> Prediction:
        data = _keys_of(data, Prediction)
        return Prediction(
            conv_now=_number(data["conv_now"]),
            conv_pred=_number(data["conv_pred"]),
            blocked_share=_number(data["blocked_share"]),
        )


class SavedSession:
    @staticmethod
    def from_dict(data: Any) -> dict[str, Any]:
        """A session as the runner hands it over: its id, run size and limits."""
        if not isinstance(data, Mapping) or set(data) != {"session_id", "size", "limits"}:
            raise ValueError("bad machine snapshot: session")
        if not isinstance(data["session_id"], str) or not data["session_id"]:
            raise ValueError("bad machine snapshot: session")
        return {
            "session_id": data["session_id"],
            "size": _count(data["size"]),
            "limits": decode.limits(data["limits"]),
        }


class OnlineSteps:
    """Chapters 2-6 of ``Machine``: its steps, their questions and their outcomes."""

    _running: bool
    _awaiting: Expect | None
    _unattended: bool
    _change_noticed: bool
    _chapter1_done: bool
    _last_conversion: float | None

    def _init_online(self, size: int) -> None:
        self._size = size
        self._step: Step | None = None
        self._session: dict[str, Any] | None = None
        self._learn: RunStats | None = None
        self._prediction: Prediction | None = None
        self._rounds: list[RunStats] = []
        self._stage: int | None = None
        self._topup = 0
        self._achievements: set[str] = set()
        self._elapsed_s: float | None = None
        # Runner-side only: never saved.
        self._hint = 0
        self._pending: type | None = None
        self._side = False
        self._status_lines: list[Event] = []
        # The carry-on after the session closed: the effect ``_run`` started
        # last, whether Enter now opens a new session, and what to run once
        # it is open (None: ask the step's question again).
        self._last_effect: Event | None = None
        self._reopen = False
        self._rerun: Event | None = None

    @property
    def session(self) -> Mapping[str, Any] | None:
        """The playground session the runner's API calls belong to, if one is open."""
        return self._session

    @property
    def chapter(self) -> int:
        return CHAPTER_OF[self._step] if self._step is not None else 1

    @property
    def elapsed_s(self) -> float | None:
        """The active console time since ``/start`` the machine has kept, in
        seconds, or None when it has none.

        It follows the runner's stamps on ``StepDone`` and ``RunFinished``
        until the results are shown, then stays put for the completion card.
        """
        return self._elapsed_s

    @property
    def unattended_no(self) -> bool:
        """True when ``--yes`` answers the open question no rather than yes."""
        return self._step in _UNATTENDED_NO and not self._running

    # -- achievements and time ---------------------------------------------------------------

    def _achievement(self, key: str) -> list[Event]:
        """The line for achievement ``key``, the first time it is earned; after that, nothing."""
        if key in self._achievements:
            return []
        self._achievements.add(key)
        return [_say(key)]

    def _stamp(self, elapsed_s: float | None) -> None:
        """Keep the runner's measure of the time since ``/start``, until the results are shown."""
        if elapsed_s is not None and "achievement.results" not in self._achievements:
            self._elapsed_s = elapsed_s

    def _card(self) -> Say:
        """The completion card, with the minutes from ``/start`` to the results when known."""
        if self._elapsed_s is None:
            return _say("complete.card.untimed")
        return _say("complete.card", minutes=max(1, round(self._elapsed_s / 60)))

    def _earned_so_far(self) -> set[str]:
        """The achievements a snapshot saved without them has earned, read from its progress."""
        step = self._step
        chapter = CHAPTER_OF[step] if step is not None else 1
        earned: set[str] = set()
        if self._chapter1_done:
            earned.add("achievement.ch1")
        if self._session is not None:
            earned.add("achievement.connected")
        if step in (Step.LEARN, Step.TOPUP) or chapter >= 4:
            earned.add("achievement.ch3")
        if chapter >= 4:
            earned.add("achievement.learned")
        if self._prediction is not None:
            earned.add("achievement.prediction")
        if step in (Step.READINESS, Step.ROUND) or chapter >= 6:
            earned.add("achievement.ch5")
        if step in _AFTER_RESULTS:
            earned.add("achievement.results")
        return earned

    # -- asking and running ---------------------------------------------------------------

    def _ask(self, step: Step, *before: Event, again: bool = False) -> list[Event]:
        """Ask ``step``'s question (after ``before``) and hand the input line back."""
        if step is not self._step:
            self._hint = 0
        self._step = step
        self._running = False
        self._pending = None
        self._reopen = False
        self._rerun = None
        self._awaiting = _KINDS[step]
        prompt = _PROMPTS_AGAIN.get(step) if again else None
        if step is Step.TOPUP:
            asked = question("ch3.topup", shortfall=self._topup)
        else:
            asked = question(prompt or _PROMPTS[step])
        return [*before, asked, AwaitInput(self._awaiting)]

    def _is_edit_step(self) -> bool:
        """True at a step where the user edits app/routes.py."""
        return self._step is not None and CHAPTER_OF[self._step] in _EDIT_CHAPTERS

    def _run(self, effect: Event, *before: Event) -> list[Event]:
        """Start ``effect`` (after ``before``); its ``StepDone`` decides what's next."""
        self._running = True
        self._awaiting = None
        self._pending = type(effect)
        self._last_effect = effect
        if isinstance(effect, RunTraffic):
            self._change_noticed = False
        return [*before, effect]

    def _run_aside(self, effect: Event) -> list[Event]:
        """Start ``effect`` for a command; the same question stays open afterwards."""
        self._pending = type(effect)
        self._side = True
        return [effect]

    # -- entering chapters -------------------------------------------------------------------

    def _enter_chapter2(self) -> list[Event]:
        return self._ask(
            Step.KEY, heading(2), _say("ch2.intro", version=CLIENT_VERSION), again=False
        )

    def _enter_chapter3(self, *before: Event) -> list[Event]:
        return self._ask(
            Step.CODE3,
            *before,
            heading(3),
            _say("ch3.intro"),
            _say("ch3.task"),
            _say("ch3.lesson"),
        )

    def _enter_chapter4(self, *before: Event) -> list[Event]:
        return self._ask(
            Step.DRY_RUN,
            *before,
            heading(4),
            _say("ch4.intro"),
            _say("ch4.click_path"),
            _say("ch4.why_block"),
            _say("ch4.where"),
        )

    def _enter_chapter5(self, *before: Event) -> list[Event]:
        self._step = Step.PROTECT_WAIT
        self._hint = 0
        return self._run(
            WaitWorkflow(Gate.G4),
            *before,
            heading(5),
            _say("ch5.meanwhile"),
            _say("ch5.task"),
            _say("ch5.lesson"),
        )

    def _next_round(self) -> int:
        return min(len(self._rounds) + 1, _ROUNDS_MAX)

    # -- user input ------------------------------------------------------------------------

    def _online_answer(self, yes: bool) -> list[Event]:
        """Going on (``yes``, or Enter at a continue wait) or ``no`` at the current step."""
        step = self._step
        assert step is not None
        if self._reopen:
            # The carry-on wait comes first: its no only waits, whatever the step.
            if yes:
                return self._run(OpenSession(self._size))
            return [_say("reply.no"), AwaitInput("continue")]
        if not yes:
            if step is Step.RERUN_OFFER:
                return self._ask(Step.KEEP)
            if step is Step.KEEP:
                return self._run(Complete(False, *self._conversions()))
            assert self._awaiting is not None
            return [_say("reply.no"), AwaitInput(self._awaiting)]
        if step is Step.KEY:
            return self._run(CheckKey())
        if step in (Step.CONSENT, Step.SESSION):
            return self._run(OpenSession(self._size))
        if step in (Step.CODE3, Step.LEARN, Step.TOPUP):
            return self._run(RunGate(3))
        if step is Step.DRY_RUN:
            return self._run(WaitWorkflow(Gate.G2))
        if step is Step.START:
            return self._run(WaitWorkflow(Gate.G3))
        if step in (Step.PROTECT_WAIT, Step.RERUN_PAUSED):
            return self._run(WaitWorkflow(Gate.G4))
        if step in _ROUND_STEPS or step is Step.RERUN_ANYWAY:
            return self._run(RunGate(5))
        if step is Step.RESULTS:
            return self._run(ReadStage())
        if step is Step.RERUN_OFFER:
            return self._ask(Step.RERUN_LATER)
        if step is Step.KEEP:
            return self._run(Complete(True, *self._conversions()))
        return [_say("reply.unknown")]

    def _conversions(self) -> tuple[float | None, float | None]:
        before = _conversion(self._learn) if self._learn is not None else None
        after = _conversion(self._rounds[-1]) if self._rounds else None
        return before, after

    def _online_continue(self) -> list[Event] | None:
        """``/continue`` within the session: the re-run, when one was offered."""
        if self._reopen:
            return None
        if self._step is Step.RERUN_LATER:
            return self._run(ReadStage())
        return None

    def _resume_step(self, step: Step) -> list[Event]:
        """Pick a saved step up again: its chapter's heading, at a code step the
        "Your turn" block again, then its question."""
        if step is Step.RERUN_LATER:
            self._step = step
            return self._run(ReadStage(), _say("continue.back"))
        again = [_say(key) for key in _RESUME_SAYS.get(step, ())]
        return self._ask(step, _say("continue.back"), heading(CHAPTER_OF[step]), *again)

    def _online_hint(self) -> list[Event]:
        assert self._step is not None
        if self._running:
            return [_say("hint.running")]
        if self._reopen:
            return [_say("hint.ch2.session")]
        levels = _HINTS[self._step]
        key = levels[min(self._hint, len(levels) - 1)]
        self._hint = min(self._hint + 1, len(levels) - 1)
        task = _HINT_TASKS.get(key)
        return [_say(key), _say(task)] if task is not None else [_say(key)]

    def _online_copy(self, index: str | None = None) -> list[Event]:
        """``/copy``, or ``/copy N`` with ``index`` "N": the current chapter's code, if it has any."""
        task = _TASK_OF_CHAPTER.get(self.chapter)
        if task is None:
            return [_say("copy.none")]
        count = len(TASKS[task][1])
        if index is None:
            return self._run_aside(CopySnippets(task))
        number = _snippet_number(index)
        if number is not None and 1 <= number <= count:
            return self._run_aside(CopySnippets(task, number))
        return [_say("copy.bad_index", count=count)]

    def _online_status(self) -> list[Event]:
        lines: list[Event] = [
            _say("status.chapter", chapter=self.chapter, title=text(_CHAPTER_TITLES[self.chapter]))
        ]
        if self._chapter1_done and self._last_conversion is not None:
            lines.append(_say("status.done_ch1", conversion=self._last_conversion))
        if self._session is not None:
            lines.append(_say("status.done_ch2"))
        if self._learn is not None:
            lines.append(
                _say(
                    "status.done_learn",
                    actions=self._learn.actions,
                    successes=self._learn.successes,
                    conversion=_conversion(self._learn),
                )
            )
        if self._prediction is not None:
            lines.append(
                _say(
                    "status.prediction",
                    conv_now=self._prediction.conv_now,
                    conv_pred=self._prediction.conv_pred,
                )
            )
        for number, stats in enumerate(self._rounds, start=1):
            lines.append(
                _say(
                    "status.round",
                    round=number,
                    conversion=_conversion(stats),
                    real_blocked=stats.real_blocked,
                    real_total=stats.real_total,
                )
            )
        if (
            self._session is not None
            and self.chapter >= 4
            and not self._running
            and self._step is not Step.DONE
            and not self._reopen
        ):
            # The workflow's state and stage are read fresh, while the
            # session is still open: not once it is completed or closed.
            self._status_lines = lines
            return self._run_aside(ReadStage())
        return lines

    def _online_report(self) -> list[Event]:
        return self._run_aside(WriteReport())

    def _end_offered_session(self, offer: OnlineSteps | None) -> list[Event]:
        """An ``EndSession`` for the session a discarded saved session had open."""
        if offer is None or offer._session is None:
            return []
        return [EndSession(dict(offer._session))]

    # -- outcomes ------------------------------------------------------------------------------

    def _on_step_done(self, event: StepDone) -> list[Event]:
        self._stamp(event.elapsed_s)
        pending, self._pending = self._pending, None
        says = list(event.says)
        if event.session_closed:
            return self._on_session_closed(event, says)
        if self._side:
            self._side = False
            lines, self._status_lines = self._status_lines, []
            if pending is ReadStage:
                if event.stage is not None:
                    self._stage = event.stage
                lines = [*lines, *says]
            else:
                lines = says
            return [*lines, AwaitInput(self._awaiting)] if self._awaiting else lines
        if event.fatal or (not event.passed and self._unattended):
            self._running = False
            return [*says, Exit(event.exit_code)]
        if pending is OpenSession and self._reopen:
            return self._on_reopened(event, says)
        step = self._step
        if pending is CheckKey:
            if event.passed:
                return self._ask(Step.CONSENT, *says)
            return self._ask(Step.KEY, *says)
        if pending is OpenSession:
            if event.passed and event.session is not None:
                self._session = SavedSession.from_dict(event.session)
                return self._enter_chapter3(
                    *says, *self._achievement("achievement.connected"), ChapterDone(2)
                )
            return self._ask(Step.SESSION, *says)
        if pending is RunGate:
            return self._on_gate(step, event, says)
        if pending is RunTraffic:
            return self._on_traffic(step, event, says)
        if pending is WaitWorkflow:
            return self._on_workflow(step, event, says)
        if pending is RunReadiness:
            if event.passed:
                round_no = self._next_round()
                self._step = Step.ROUND
                return self._run(
                    RunTraffic(Phase.PROTECT, self._size, round_no),
                    *says,
                    _say("ch5.round.intro", size=self._size),
                )
            return self._ask(Step.READINESS, *says)
        if pending is ReadStage:
            return self._on_stage(step, event, says)
        if pending is Complete:
            return self._ask(
                Step.DONE,
                *says,
                self._card(),
                _say("ch6.diff"),
                _say("ch6.handoff"),
                ChapterDone(6),
            )
        raise ValueError(event)

    def _on_session_closed(self, event: StepDone, says: list[Event]) -> list[Event]:
        """The session this step used was closed: offer to open a new one and carry on.

        Enter (or yes) opens it, then runs the effect that found the session
        closed once more; for a command's effect (``/status``) it asks the
        step's question again instead. An unattended run stops here.
        """
        if self._side:
            self._side = False
            lines, self._status_lines = self._status_lines, []
            rerun = None
        else:
            lines = []
            rerun = self._last_effect
        self._running = False
        if self._unattended:
            return [*lines, *says, _say("online.session_gone"), Exit(event.exit_code)]
        if not self._reopen:
            self._rerun = rerun
        self._reopen = True
        self._pending = None
        self._awaiting = "continue"
        return [*lines, *says, question("online.session_gone"), AwaitInput("continue")]

    def _on_reopened(self, event: StepDone, says: list[Event]) -> list[Event]:
        """The new session of a carry-on: on with the step, or the same wait again."""
        if event.passed and event.session is not None:
            self._session = SavedSession.from_dict(event.session)
            self._reopen = False
            rerun, self._rerun = self._rerun, None
            if rerun is None:
                assert self._step is not None
                return self._ask(self._step, *says)
            return self._run(rerun, *says)
        self._running = False
        self._awaiting = "continue"
        return [*says, question("ch2.session.retry"), AwaitInput("continue")]

    def _on_gate(self, step: Step | None, event: StepDone, says: list[Event]) -> list[Event]:
        if step in (Step.CODE3, Step.LEARN, Step.TOPUP):
            if not event.passed:
                return self._ask(Step.CODE3, *says, again=True)
            size = self._topup if step is Step.TOPUP and self._topup > 0 else self._size
            return self._run(
                RunTraffic(Phase.LEARN, size),
                *says,
                *self._achievement("achievement.ch3"),
                _say("ch3.learn.intro", size=size),
            )
        if not event.passed:
            return self._ask(Step.CODE5, *says, again=True)
        if step in _RERUN_STEPS:
            return self._run(
                RunTraffic(Phase.PROTECT, self._size, _ROUNDS_MAX),
                *says,
                _say("ch6.rerun.go", size=self._size),
            )
        return self._run(
            RunReadiness(),
            *says,
            *self._achievement("achievement.ch5"),
            _say("ch5.readiness.intro"),
        )

    def _on_traffic(self, step: Step | None, event: StepDone, says: list[Event]) -> list[Event]:
        if step in (Step.CODE3, Step.LEARN, Step.TOPUP):
            if not event.passed or event.stats is None:
                return self._ask(Step.LEARN, *says)
            if step is not Step.TOPUP or self._learn is None:
                self._learn = event.stats
            if event.shortfall > 0:
                self._topup = event.shortfall
                return self._ask(Step.TOPUP, *says)
            self._topup = 0
            return self._enter_chapter4(
                *says,
                _say("ch3.done"),
                *self._achievement("achievement.learned"),
                ChapterDone(3),
            )
        if not event.passed or event.stats is None:
            return self._ask(Step.ROUND, *says)
        round_no = self._next_round()
        if round_no > len(self._rounds):
            self._rounds.append(event.stats)
        else:
            self._rounds[round_no - 1] = event.stats
        if len(self._rounds) >= _ROUNDS_MAX:
            return self._ask(Step.KEEP, *says, *self._frame())
        return self._ask(Step.RESULTS, *says, _say("ch5.done"), ChapterDone(5), heading(6))

    def _on_workflow(self, step: Step | None, event: StepDone, says: list[Event]) -> list[Event]:
        if event.stage is not None:
            self._stage = event.stage
        if step is Step.DRY_RUN:
            if not event.passed or event.prediction is None:
                return self._ask(Step.DRY_RUN, *says)
            self._prediction = event.prediction
            return self._ask(
                Step.START,
                *says,
                _say(
                    "ch4.prediction",
                    conv_now=event.prediction.conv_now,
                    conv_pred=event.prediction.conv_pred,
                    blocked_share=event.prediction.blocked_share,
                ),
                *self._achievement("achievement.prediction"),
                _say("ch4.lesson"),
                _say("ch4.warning"),
            )
        if step is Step.START:
            if not event.passed:
                return self._ask(Step.START, *says)
            return self._enter_chapter5(*says, ChapterDone(4))
        if step is Step.RERUN_PAUSED:
            if not event.passed:
                return self._ask(Step.RERUN_PAUSED, *says)
            return self._run(ReadStage(), *says)
        if not event.passed:
            return self._ask(Step.PROTECT_WAIT, *says)
        return self._ask(Step.CODE5, *says, _say("ch5.live"))

    def _on_stage(self, step: Step | None, event: StepDone, says: list[Event]) -> list[Event]:
        if event.stage is not None:
            self._stage = event.stage
        if step is Step.RESULTS:
            frame = self._frame()
            if event.stage is None:
                frame = [*says, *frame]
            frame = [*frame, *self._achievement("achievement.results")]
            if (self._stage or 0) >= _STAGES_TOTAL:
                return self._ask(Step.KEEP, *frame)
            return self._ask(Step.RERUN_OFFER, *frame)
        if event.paused:
            return self._ask(Step.RERUN_PAUSED, *says)
        if event.stage is not None and event.stage >= _STAGES_TOTAL:
            self._step = Step.RERUN_ANYWAY
            return self._run(RunGate(5))
        line = stage_say(event.stage)
        before = [line] if line is not None else says
        return self._ask(Step.RERUN_ANYWAY, *before)

    # -- the results frame ----------------------------------------------------------------------

    def _frame(self) -> list[Event]:
        """The results: before and after, the prediction, the stage line, the framing."""
        learn = self._learn or RunStats(0, 0, 0)
        if not self._rounds:
            return []
        none = text("results.cell.none")
        last = self._rounds[-1]
        if last.check_ms is not None and last.inside_ms is not None:
            check_cell = text(
                "results.cell.check", check_ms=last.check_ms, inside_ms=last.inside_ms
            )
        else:
            check_cell = none
        stage = self._stage if self._stage is not None else 0
        before_conversion = _conversion(learn)
        lines: list[Event] = []
        if len(self._rounds) == 1:
            after_conversion = _conversion(last)
            trend = "results.trend.up" if after_conversion > before_conversion else None
            lines.append(
                _say(
                    "results.frame",
                    round=1,
                    stage=stage,
                    before_actions=learn.actions,
                    after_actions=last.actions,
                    before_successes=learn.successes,
                    after_successes=last.successes,
                    before_conversion=before_conversion,
                    after_conversion=after_conversion,
                    trend=text(trend or "results.trend.none"),
                    blocked_cell=text(
                        "results.cell.of", blocked=last.real_blocked, total=last.real_total
                    ),
                    check_cell=check_cell,
                )
            )
        else:
            first = self._rounds[0]
            lines.append(
                _say(
                    "results.frame.two",
                    stage=stage,
                    before_actions=learn.actions,
                    r1_actions=first.actions,
                    r2_actions=last.actions,
                    before_successes=learn.successes,
                    r1_successes=first.successes,
                    r2_successes=last.successes,
                    before_conversion=before_conversion,
                    r1_conversion=_conversion(first),
                    r2_conversion=_conversion(last),
                    r1_blocked=text(
                        "results.cell.of", blocked=first.real_blocked, total=first.real_total
                    ),
                    r2_blocked=text(
                        "results.cell.of", blocked=last.real_blocked, total=last.real_total
                    ),
                    check_cell=check_cell,
                )
            )
        runs = [(text("results.run.learn"), learn)]
        runs += [(text("results.run.round", round=n), s) for n, s in enumerate(self._rounds, 1)]
        for run, stats in runs:
            if stats.degraded:
                lines.append(_say("results.indent", line=text("results.degraded", run=run)))
        if self._prediction is not None:
            lines.append(
                _say(
                    "results.prediction",
                    conv_now=self._prediction.conv_now,
                    conv_pred=self._prediction.conv_pred,
                )
            )
        line = stage_say(stage)
        if line is not None:
            lines.append(_say("results.indent", line=text(line.key, **dict(line.params))))
        lines.append(_say("results.indent", line=text("results.framing")))
        return lines

    # -- saved progress -------------------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        """The chapters 2-6 part of ``Machine.to_dict()``."""
        return {
            "size": self._size,
            "step": self._step.name if self._step is not None else None,
            "session": dict(self._session) if self._session is not None else None,
            "learn": asdict(self._learn) if self._learn is not None else None,
            "prediction": asdict(self._prediction) if self._prediction is not None else None,
            "rounds": [asdict(stats) for stats in self._rounds],
            "stage": self._stage,
            "topup": self._topup,
            "achievements": sorted(self._achievements),
            "elapsed_s": self._elapsed_s,
        }

    def from_dict(self, data: Mapping[str, Any]) -> None:
        """Load the chapters 2-6 part of a ``Machine.to_dict()`` snapshot into ``self``.

        Raises ``ValueError`` for a shape ``to_dict`` couldn't have written.
        A snapshot saved before achievements and time were kept has neither:
        its achievements are read from its progress, and its time is unknown.
        """
        size = data["size"]
        if not isinstance(size, int) or isinstance(size, bool) or size <= 0:
            raise ValueError("bad machine snapshot: size")
        step = data["step"]
        if step is not None and (not isinstance(step, str) or step not in Step.__members__):
            raise ValueError("bad machine snapshot: step")
        rounds = data["rounds"]
        if not isinstance(rounds, list) or len(rounds) > _ROUNDS_MAX:
            raise ValueError("bad machine snapshot: rounds")
        stage = data["stage"]
        if stage is not None and (
            not isinstance(stage, int) or isinstance(stage, bool) or not 0 <= stage <= 3
        ):
            raise ValueError("bad machine snapshot: stage")
        session = data["session"]
        learn = data["learn"]
        prediction = data["prediction"]
        self._size = size
        self._step = Step[step] if step is not None else None
        self._session = SavedSession.from_dict(session) if session is not None else None
        self._learn = SavedStats.from_dict(learn) if learn is not None else None
        self._prediction = SavedPrediction.from_dict(prediction) if prediction is not None else None
        self._rounds = [SavedStats.from_dict(item) for item in rounds]
        self._stage = stage
        self._topup = _count(data["topup"])
        if self._step is not None and CHAPTER_OF[self._step] >= 3 and self._session is None:
            raise ValueError("bad machine snapshot: a later chapter needs its session")
        if "achievements" in data:
            self._achievements = _earned(data["achievements"])
            elapsed_s = data["elapsed_s"]
            self._elapsed_s = None if elapsed_s is None else _number(elapsed_s)
        else:
            self._achievements = self._earned_so_far()
            self._elapsed_s = None


__all__ = ["CHAPTER_OF", "OnlineSteps", "Step", "heading", "question", "stage_say"]
