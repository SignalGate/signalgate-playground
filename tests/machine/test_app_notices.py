"""What the console says when the sample app comes back up or doesn't start.

A save the app picked up gets one line where the user edits the code
(chapters 3 and 5), anywhere once it fixes an app that didn't start, and
anywhere a run said the reload would wait until it was over; elsewhere a
reload says nothing. No line names a boot number. A start that
failed names the error, shows the line Python pointed at, gives a hint for
the common mistakes, and says how to go on.
"""

from __future__ import annotations

import pytest

from playground.machine.events import (
    AppDown,
    AppReady,
    AppReloaded,
    AwaitInput,
    BootFailure,
    ChangeDetected,
    Exit,
    RunGate,
    Say,
)
from playground.machine.machine import Machine
from playground.machine.steps import CHAPTER_OF, Step
from tests.machine._scenarios import (
    RUN_FINISHED,
    assert_say_renders,
    opening_machine,
    running_machine,
    say,
)
from tests.machine.test_online_steps import LEARN, at_code3, at_results, done

EDIT_STEPS = [step for step in Step if CHAPTER_OF[step] in (3, 5)]
OTHER_STEPS = [step for step in Step if CHAPTER_OF[step] not in (3, 5)]

INDENTED = BootFailure(
    error="IndentationError",
    message="unexpected indent",
    location="app/routes.py:57",
    source="        ev = sg_event(request, body.user, body.signalgate_log, 'playground.action')",
    hint="app.down.hint.indent",
)


def machine_at(step: Step) -> Machine:
    machine = Machine(saved={**at_results().to_dict(), "step": step.name})
    machine.on_event(AppReady())
    machine.handle("/continue")
    return machine


# -- reloads ----------------------------------------------------------------------------------


@pytest.mark.parametrize("step", EDIT_STEPS, ids=lambda step: step.name)
def test_a_save_at_an_edit_step_says_it_is_running(step):
    machine = machine_at(step)
    before = machine.to_dict()
    notice = machine.on_event(AppReloaded(4, "change"))
    assert notice == [say("app.reloaded")]
    assert notice[0].params == {}
    assert machine.to_dict() == before


@pytest.mark.parametrize("step", OTHER_STEPS, ids=lambda step: step.name)
def test_a_save_elsewhere_says_nothing(step):
    machine = machine_at(step)
    before = machine.to_dict()
    assert machine.on_event(AppReloaded(4, "change")) == []
    assert machine.to_dict() == before


def test_a_save_that_fixes_a_failed_start_says_so_at_any_step():
    for step in (Step.DRY_RUN, Step.KEY, Step.RESULTS):
        machine = machine_at(step)
        machine.on_event(AppDown("app/routes.py:3"))
        assert machine.on_event(AppReloaded(5, "change")) == [say("app.reloaded")], step
        # Up again: the next save outside the edit steps is quiet once more.
        assert machine.on_event(AppReloaded(6, "change")) == [], step
    machine = opening_machine()
    machine.on_event(AppDown(failure=INDENTED))
    assert machine.on_event(AppReloaded(2, "change")) == [say("app.reloaded")]


def test_a_save_a_run_held_back_is_confirmed_once_it_lands():
    machine = at_code3()
    assert machine.handle("") == [RunGate(3)]
    machine.on_event(done())  # the code gate passed: the learning run starts
    assert machine.on_event(ChangeDetected()) == [say("reload.deferred")]
    machine.on_event(done(stats=LEARN))  # the run passed: chapter 4, not an edit step
    assert machine.to_dict()["step"] == "DRY_RUN"
    before = machine.to_dict()
    assert machine.on_event(AppReloaded(2, "change")) == [say("app.reloaded")]
    assert machine.to_dict() == before
    # Said once: the next save here is quiet again.
    assert machine.on_event(AppReloaded(3, "change")) == []


def test_a_save_during_chapter_1_is_confirmed_at_the_close():
    machine = running_machine()
    assert machine.on_event(ChangeDetected()) == [say("reload.deferred")]
    assert machine.on_event(ChangeDetected()) == []
    machine.on_event(RUN_FINISHED)
    assert machine.on_event(AppReloaded(2, "change")) == [say("app.reloaded")]
    assert machine.on_event(AppReloaded(3, "change")) == []


def test_a_restart_with_the_key_does_not_use_up_a_held_back_save():
    machine = running_machine()
    machine.on_event(ChangeDetected())
    machine.on_event(RUN_FINISHED)
    assert machine.on_event(AppReloaded(2, "request")) == [say("app.restarted_key")]
    assert machine.on_event(AppReloaded(3, "change")) == [say("app.reloaded")]


def test_restarts_say_what_happened_without_a_number():
    for cause, key in (("request", "app.restarted_key"), ("crash", "app.restarted")):
        for machine in (opening_machine(), machine_at(Step.DRY_RUN), machine_at(Step.CODE3)):
            notice = machine.on_event(AppReloaded(7, cause))
            assert notice == [say(key)]
            assert notice[0].params == {}
            assert_say_renders(notice)


# -- a start that failed ---------------------------------------------------------------------------


def test_an_indentation_error_names_the_error_the_line_and_the_fix():
    machine = machine_at(Step.CODE3)
    before = machine.to_dict()
    notice = machine.on_event(AppDown(INDENTED.location, INDENTED))
    assert notice == [
        say("app.down.error", error="IndentationError", location="app/routes.py:57"),
        say("app.down.code", code=INDENTED.source),
        say("app.down.hint.indent"),
        say("app.down.fix"),
    ]
    assert [event.block for event in notice] == ["result", "code", "note", "note"]
    assert_say_renders(notice)
    assert machine.to_dict() == before


def test_a_syntax_error_shows_its_caret_line_and_no_hint():
    failure = BootFailure(
        error="SyntaxError",
        message="invalid syntax",
        location="app/routes.py:9",
        source="def broken(:",
        caret="           ^",
    )
    notice = machine_at(Step.CODE5).on_event(AppDown(failure.location, failure))
    assert notice == [
        say("app.down.error", error="SyntaxError", location="app/routes.py:9"),
        say("app.down.code", code="def broken(:"),
        say("app.down.code", code="           ^"),
        say("app.down.fix"),
    ]
    assert_say_renders(notice)


def test_a_failure_with_no_line_to_show_still_names_the_error():
    failure = BootFailure(error="ImportError", message="x", location="app/routes.py:1")
    notice = opening_machine().on_event(AppDown(failure.location, failure))
    assert [event.key for event in notice] == ["app.down.error", "app.down.fix"]


def test_a_code_line_with_braces_is_shown_as_it_is():
    failure = BootFailure(
        error="SyntaxError", message="x", location="app/routes.py:2", source="d = {'a': 2**8"
    )
    notice = opening_machine().on_event(AppDown(failure.location, failure))
    assert_say_renders(notice)


def test_without_the_details_the_short_line_is_kept():
    assert opening_machine().on_event(AppDown("app/routes.py:5")) == [
        say("app.down_at", location="app/routes.py:5")
    ]
    assert opening_machine().on_event(AppDown()) == [say("app.down")]


def test_a_failure_before_the_opening_still_opens_the_console():
    events = Machine().on_event(AppDown(INDENTED.location, INDENTED))
    keys = [event.key if isinstance(event, Say) else event for event in events]
    assert keys == [
        "app.down.error",
        "app.down.code",
        "app.down.hint.indent",
        "app.down.fix",
        "opening",
        AwaitInput("start"),
    ]


def test_an_unattended_session_exits_after_saying_why():
    events = Machine(unattended=True).on_event(AppDown(INDENTED.location, INDENTED))
    assert events[-1] == Exit(2)
    assert [event.key for event in events[:-1]] == [
        "app.down.error",
        "app.down.code",
        "app.down.hint.indent",
        "app.down.fix",
    ]
