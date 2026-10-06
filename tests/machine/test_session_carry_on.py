"""The carry-on after the playground session closed, in the machine.

When an effect finds its session closed (``StepDone.session_closed``: it
expired, or a newer one replaced it), the step stays where it is and a
"continue" wait offers to carry on. Enter or ``yes`` opens a new session,
with the same size and no new consent, then runs the very effect that found
the session closed once more; for ``/status``, which only reads, the step's
question is asked again. Progress, the chapter and the achievements stay as
they were, and saved progress keeps today's shape. An unattended run says
the session closed and stops with exit 2.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest

from playground.machine.events import (
    ANSWERS,
    COMMANDS,
    EFFECTS,
    AppDown,
    AppReady,
    AppReloaded,
    AwaitInput,
    ChangeDetected,
    CheckKey,
    Complete,
    CopySnippets,
    Event,
    Exit,
    Gate,
    OpenSession,
    Phase,
    ReadStage,
    RunGate,
    RunReadiness,
    RunTraffic,
    SaveState,
    Say,
    StateSaved,
    StepDone,
    WaitWorkflow,
    WriteReport,
)
from playground.machine.machine import Machine
from playground.machine.steps import _KINDS, Step
from tests.machine._scenarios import BLANK_INPUTS, GARBAGE_INPUTS, RUN_FINISHED, say
from tests.machine.test_online_steps import (
    LEARN,
    LIMITS,
    PREDICTION,
    SESSION,
    SIZE,
    _checked,
    at_code3,
    at_code5,
    at_dry_run,
    at_results,
    done,
    keys,
)

# The new session a carry-on opens.
NEW_SESSION = {"session_id": "sid-2", "size": SIZE, "limits": LIMITS}
CLOSED = StepDone(False, session_closed=True)
SHORTFALL = 40

# The question that offers to carry on, and the wait it hands back.
CARRY_ON = [say("online.session_gone"), AwaitInput("continue")]


def _carry_on(machine: Machine, effect: Event) -> list[Event]:
    """``effect`` was just started; it finds the session closed."""
    events = _checked(machine.on_event(CLOSED))
    assert events == CARRY_ON, (effect, events)
    assert events[-2].block == "ask"
    return events


# -- where each effect starts, and the effect ------------------------------------------------


def _gate_3() -> tuple[Machine, Event]:
    machine = at_code3()
    effect = RunGate(3)
    assert machine.handle("yes") == [effect]
    return machine, effect


def _learning_run() -> tuple[Machine, Event]:
    machine, _ = _gate_3()
    effect = RunTraffic(Phase.LEARN, SIZE)
    assert machine.on_event(done())[-1] == effect
    return machine, effect


def _top_up_run() -> tuple[Machine, Event]:
    machine, _ = _learning_run()
    _checked(machine.on_event(done(stats=LEARN, shortfall=SHORTFALL)))
    assert machine.handle("yes") == [RunGate(3)]
    effect = RunTraffic(Phase.LEARN, SHORTFALL)
    assert machine.on_event(done())[-1] == effect
    return machine, effect


def _dry_run() -> tuple[Machine, Event]:
    machine = at_dry_run()
    effect = WaitWorkflow(Gate.G2)
    assert machine.handle("") == [effect]
    return machine, effect


def _start() -> tuple[Machine, Event]:
    machine, _ = _dry_run()
    _checked(machine.on_event(done(prediction=PREDICTION)))
    effect = WaitWorkflow(Gate.G3)
    assert machine.handle("yes") == [effect]
    return machine, effect


def _protection_wait() -> tuple[Machine, Event]:
    # Chapter 5 starts this wait by itself: no question asked for it.
    machine, _ = _start()
    effect = WaitWorkflow(Gate.G4)
    assert machine.on_event(done())[-1] == effect
    return machine, effect


def _gate_5() -> tuple[Machine, Event]:
    machine = at_code5()
    effect = RunGate(5)
    assert machine.handle("yes") == [effect]
    return machine, effect


def _readiness() -> tuple[Machine, Event]:
    machine, _ = _gate_5()
    effect = RunReadiness()
    assert machine.on_event(done())[-1] == effect
    return machine, effect


def _round_1() -> tuple[Machine, Event]:
    machine, _ = _readiness()
    effect = RunTraffic(Phase.PROTECT, SIZE, 1)
    assert machine.on_event(done())[-1] == effect
    return machine, effect


def _round_2() -> tuple[Machine, Event]:
    machine = at_results()
    machine.handle("yes")
    machine.on_event(done(stage=1))
    machine.handle("yes")  # the re-run waits for /continue
    assert machine.handle("/continue") == [ReadStage()]
    machine.on_event(done(stage=2))
    assert machine.handle("yes") == [RunGate(5)]
    effect = RunTraffic(Phase.PROTECT, SIZE, 2)
    assert machine.on_event(done())[-1] == effect
    return machine, effect


def _results() -> tuple[Machine, Event]:
    machine = at_results()
    effect = ReadStage()
    assert machine.handle("yes") == [effect]
    return machine, effect


EFFECT_STARTS: dict[str, Callable[[], tuple[Machine, Event]]] = {
    "gate_3": _gate_3,
    "learning_run": _learning_run,
    "top_up_run": _top_up_run,
    "dry_run": _dry_run,
    "start": _start,
    "protection_wait": _protection_wait,
    "gate_5": _gate_5,
    "readiness": _readiness,
    "round_1": _round_1,
    "round_2": _round_2,
    "results": _results,
}


def carry_on_machine() -> Machine:
    """A console waiting to carry on: chapter 3's code gate found the session closed."""
    machine, effect = _gate_3()
    _carry_on(machine, effect)
    return machine


def _without_session(snapshot: dict) -> dict:
    return {**snapshot, "session": None}


# -- Enter, yes, no and anything else ----------------------------------------------------------


@pytest.mark.parametrize("answer", [*BLANK_INPUTS, "yes", "YES", "Yes", "  yes  "])
def test_enter_and_yes_open_a_new_session_alike(answer):
    reference = carry_on_machine()
    assert reference.handle("") == [OpenSession(SIZE)]
    machine = carry_on_machine()
    assert machine.handle(answer) == [OpenSession(SIZE)]
    assert machine.to_dict() == reference.to_dict()
    assert machine.on_event(done(session=NEW_SESSION)) == reference.on_event(
        done(session=NEW_SESSION)
    )
    assert machine.to_dict() == reference.to_dict()


@pytest.mark.parametrize("answer", ["no", "NO", " No "])
def test_no_at_the_carry_on_only_waits(answer):
    machine = carry_on_machine()
    before = machine.to_dict()
    assert machine.handle(answer) == [say("reply.no"), AwaitInput("continue")]
    assert machine.to_dict() == before
    assert machine.handle("") == [OpenSession(SIZE)]


@pytest.mark.parametrize("answer", GARBAGE_INPUTS)
def test_anything_else_at_the_carry_on_is_not_understood(answer):
    machine = carry_on_machine()
    before = machine.to_dict()
    assert machine.handle(answer) == [say("reply.unknown"), AwaitInput("continue")]
    assert machine.to_dict() == before
    assert machine.handle("yes") == [OpenSession(SIZE)]


def test_the_carry_on_opens_the_session_with_the_configured_size():
    machine = Machine(size=1100)
    machine.on_event(AppReady())
    machine.handle("/start")
    machine.on_event(RUN_FINISHED)
    machine.handle("yes")
    assert machine.handle("yes") == [CheckKey()]
    machine.on_event(done())
    assert machine.handle("yes") == [OpenSession(1100)]
    machine.on_event(done(session={**SESSION, "size": 1100}))
    assert machine.handle("yes") == [RunGate(3)]
    _carry_on(machine, RunGate(3))
    assert machine.handle("") == [OpenSession(1100)]


# -- after the new session opens: the very effect again ----------------------------------------


@pytest.mark.parametrize("start", list(EFFECT_STARTS), ids=str)
def test_the_new_session_runs_the_very_effect_that_found_the_old_one_closed(start):
    machine, effect = EFFECT_STARTS[start]()
    before = machine.to_dict()
    _carry_on(machine, effect)
    assert machine.handle("") == [OpenSession(SIZE)]
    assert machine.on_event(done(session=NEW_SESSION)) == [effect]
    assert machine.session == NEW_SESSION
    after = machine.to_dict()
    assert after["session"]["session_id"] == NEW_SESSION["session_id"]
    # The step, the progress, the achievements and the time are as they were.
    assert _without_session(after) == _without_session(before)


def test_the_re_run_carries_on_from_what_it_then_finds():
    machine, effect = _learning_run()
    _carry_on(machine, effect)
    machine.handle("")
    assert machine.on_event(done(session=NEW_SESSION)) == [effect]
    events = _checked(machine.on_event(done(stats=LEARN)))
    assert "ch3.done" in keys(events) and keys(events)[-2:] == ["ch4.ask", AwaitInput("continue")]
    assert keys(events).count("achievement.learned") == 1
    assert machine.to_dict()["learn"]["actions"] == LEARN.actions


def test_no_achievement_or_chapter_comes_with_the_new_session():
    machine, effect = _gate_5()
    achievements = machine.to_dict()["achievements"]
    _carry_on(machine, effect)
    machine.handle("")
    events = machine.on_event(done(says=(say("ch2.session.ok"),), session=NEW_SESSION))
    assert events == [say("ch2.session.ok"), effect]
    assert machine.to_dict()["achievements"] == achievements


def test_a_refused_new_session_keeps_the_carry_on_and_the_effect():
    machine, effect = _round_1()
    _carry_on(machine, effect)
    machine.handle("")
    refused = say("ch2.session.active", started_at="at 14:05 on 2026-10-02", key_prefix="k1")
    events = _checked(machine.on_event(done(False, says=(refused,), exit_code=3)))
    assert events == [refused, say("ch2.session.retry"), AwaitInput("continue")]
    assert events[-2].block == "ask"
    assert machine.handle("no") == [say("reply.no"), AwaitInput("continue")]
    assert machine.handle("yes") == [OpenSession(SIZE)]
    unreachable = say("online.unreachable")
    events = _checked(machine.on_event(done(False, says=(unreachable,), exit_code=4)))
    assert events == [unreachable, say("ch2.session.retry"), AwaitInput("continue")]
    assert machine.handle("") == [OpenSession(SIZE)]
    assert machine.on_event(done(session=NEW_SESSION)) == [effect]


def test_an_outdated_checkout_at_the_carry_on_exits_with_3():
    machine = carry_on_machine()
    machine.handle("")
    events = machine.on_event(done(False, says=(say("online.outdated"),), fatal=True, exit_code=3))
    assert events == [say("online.outdated"), Exit(3)]


def test_the_new_session_closing_too_offers_to_carry_on_again():
    machine, effect = _dry_run()
    _carry_on(machine, effect)
    machine.handle("")
    assert machine.on_event(done(session=NEW_SESSION)) == [effect]
    _carry_on(machine, effect)
    machine.handle("")
    third = {**NEW_SESSION, "session_id": "sid-3"}
    assert machine.on_event(done(session=third)) == [effect]
    assert machine.session == third


# -- /status, which only reads -------------------------------------------------------------


def test_status_that_finds_the_session_closed_shows_its_lines_then_the_carry_on():
    machine = at_code5()
    assert machine.handle("/status") == [ReadStage()]
    events = _checked(machine.on_event(CLOSED))
    assert keys(events) == [
        "status.chapter",
        "status.done_ch1",
        "status.done_ch2",
        "status.done_learn",
        "status.prediction",
        "online.session_gone",
        AwaitInput("continue"),
    ]
    assert events[-2].block == "ask"
    assert machine.handle("") == [OpenSession(SIZE)]
    # After the new session opens, the step's own question is asked again.
    assert machine.on_event(done(session=NEW_SESSION)) == [say("ch5.ask"), AwaitInput("continue")]
    assert machine.session == NEW_SESSION
    assert machine.handle("yes") == [RunGate(5)]


@pytest.mark.parametrize(
    ("answered", "asked", "kind"),
    [((), "ch6.rerun.offer", "yes_no"), (("no",), "ch6.keep", "yes_no")],
)
def test_no_at_a_carry_on_never_answers_the_step_underneath(answered, asked, kind):
    machine = at_results()
    machine.handle("yes")
    machine.on_event(done(stage=1))
    for answer in answered:
        machine.handle(answer)
    assert machine.handle("/status")[-1] == ReadStage()
    assert _checked(machine.on_event(CLOSED))[-2:] == CARRY_ON
    before = machine.to_dict()
    events = machine.handle("no")
    assert events == [say("reply.no"), AwaitInput("continue")]
    assert not any(isinstance(event, Complete) for event in events)
    assert machine.to_dict() == before
    assert machine.handle("yes") == [OpenSession(SIZE)]
    assert machine.on_event(done(session=NEW_SESSION)) == [say(asked), AwaitInput(kind)]


def test_status_during_the_carry_on_reads_nothing():
    machine, effect = _dry_run()
    _carry_on(machine, effect)
    events = machine.handle("/status")
    assert not any(isinstance(event, EFFECTS) for event in events)
    assert events[-1] == AwaitInput("continue")
    assert "status.done_learn" in keys(events)


def test_status_after_the_session_completed_reads_nothing():
    machine = at_results()
    machine.handle("yes")
    machine.on_event(done(stage=1))
    machine.handle("no")
    machine.handle("no")
    _checked(machine.on_event(done(says=(say("ch6.paused"),))))
    assert machine.to_dict()["step"] == Step.DONE.name
    events = machine.handle("/status")
    assert not any(isinstance(event, EFFECTS) for event in events)
    assert events[-1] == AwaitInput("any")
    assert "status.round" in keys(events)


def test_hint_and_continue_during_the_carry_on():
    machine = carry_on_machine()
    assert machine.handle("/hint") == [say("hint.ch2.session"), AwaitInput("continue")]
    assert machine.handle("/continue") == [say("continue.none"), AwaitInput("continue")]
    assert machine.handle("") == [OpenSession(SIZE)]
    assert machine.handle("/hint") == [say("hint.running")]


def test_report_and_copy_during_the_carry_on_keep_it_open():
    machine = carry_on_machine()
    assert machine.handle("/report") == [WriteReport()]
    assert machine.on_event(done(says=(say("report.written"),))) == [
        say("report.written"),
        AwaitInput("continue"),
    ]
    assert machine.handle("/copy") == [CopySnippets("ch3.task")]
    assert machine.on_event(done(says=(say("copy.done", count=3),)))[-1] == AwaitInput("continue")
    assert machine.handle("") == [OpenSession(SIZE)]
    assert machine.on_event(done(session=NEW_SESSION)) == [RunGate(3)]


def test_notices_during_the_carry_on_change_nothing():
    machine = carry_on_machine()
    before = machine.to_dict()
    for notice in (AppReloaded(2, "change"), ChangeDetected(), StateSaved(True), AppDown()):
        result = machine.on_event(notice)
        assert all(isinstance(event, Say) for event in result), notice
        assert machine.to_dict() == before, notice
    assert machine.handle("") == [OpenSession(SIZE)]


# -- an unattended run -----------------------------------------------------------------------


def _unattended_at_code3() -> Machine:
    machine = Machine(unattended=True, size=SIZE)
    machine.on_event(AppReady())
    machine.handle("/start")
    machine.on_event(RUN_FINISHED)
    machine.handle("yes")
    machine.handle("yes")
    machine.on_event(done())
    assert machine.handle("yes") == [OpenSession(SIZE)]
    machine.on_event(done(session=SESSION))
    return machine


@pytest.mark.parametrize("learning_run", [False, True], ids=["code_gate", "learning_run"])
def test_an_unattended_run_says_the_session_closed_and_exits_2(learning_run):
    machine = _unattended_at_code3()
    assert machine.handle("yes") == [RunGate(3)]
    if learning_run:
        assert machine.on_event(done())[-1] == RunTraffic(Phase.LEARN, SIZE)
    events = machine.on_event(CLOSED)
    assert events == [say("online.session_gone"), Exit(2)]
    assert events[0].block == "result"
    assert not any(isinstance(event, OpenSession) for event in events)


# -- saved progress keeps today's shape ------------------------------------------------------


def test_saved_progress_during_the_carry_on_has_todays_keys_and_round_trips():
    machine, _ = _learning_run()
    machine.on_event(done(stats=LEARN, shortfall=SHORTFALL))  # the top-up question
    assert machine.handle("yes") == [RunGate(3)]
    _carry_on(machine, RunGate(3))
    snapshot = machine.to_dict()
    assert set(snapshot) == set(Machine().to_dict())
    assert snapshot["step"] == Step.TOPUP.name and snapshot["awaiting"] == "continue"
    assert snapshot["session"] == SESSION
    assert Machine.from_dict(snapshot).to_dict() == snapshot

    # Picked up later, the step's own question is asked, with its own kind.
    later = Machine(saved=snapshot)
    later.on_event(AppReady())
    events = _checked(later.handle("/continue"))
    assert _KINDS[Step.TOPUP] == "yes_no"
    assert events[-2:] == [say("ch3.topup", shortfall=SHORTFALL), AwaitInput("yes_no")]


def test_quit_at_the_carry_on_saves_the_progress_with_the_old_session():
    machine = carry_on_machine()
    assert machine.handle("/quit") == [SaveState(), say("console.bye"), Exit(0)]
    assert machine.on_event(StateSaved(True)) == [say("console.saved")]
    snapshot = machine.to_dict()
    assert snapshot["session"] == SESSION and snapshot["step"] == Step.CODE3.name
    later = Machine(saved=snapshot)
    later.on_event(AppReady())
    events = _checked(later.handle("/continue"))
    assert events[-2:] == [say("ch3.ask"), AwaitInput("continue")]


def test_every_reply_at_the_carry_on_hands_the_input_line_back_or_moves_on():
    inputs = (*COMMANDS, *ANSWERS, *GARBAGE_INPUTS, *BLANK_INPUTS, "/copy 2")
    for input_text in inputs:
        result = carry_on_machine().handle(input_text)
        assert result, input_text
        assert isinstance(result[-1], (AwaitInput, Exit, *EFFECTS)), (input_text, result)
