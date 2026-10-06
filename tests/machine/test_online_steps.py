"""Chapters 2-6 in the machine: each step's question, its effect and what its outcome leads to.

The runner's part is played here by hand: each effect the machine hands
over is answered with the ``StepDone`` a real run could give.

Most questions are "continue" waits (Enter or ``yes`` goes on); consent, the
top-up, the re-run offer, "re-run anyway" and the keep question are yes/no
questions (see ``tests/machine/test_await_kinds.py``).
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from playground.copy.en import text
from playground.machine.events import (
    EFFECTS,
    AppReady,
    AwaitInput,
    ChapterDone,
    CheckKey,
    Complete,
    EndSession,
    Exit,
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
from playground.machine.machine import Machine
from tests.machine._scenarios import assert_say_renders, end_machine, say

SIZE = 2000
LIMITS = {
    "max_size": 3000,
    "lease_max": 50,
    "use_within_s": 45,
    "max_queue_age_s": 90,
    "clock_warn_s": 7,
    "clock_block_s": 33,
    "readiness_retry_s": 3,
    "readiness_max_attempts": 2,
    "ingest_wait_s": 4,
    "protect_diagnose_after_s": 9,
}
SESSION = {"session_id": "sid-1", "size": SIZE, "limits": LIMITS}
LEARN = RunStats(total=SIZE, actions=SIZE, successes=400)
ROUND = RunStats(
    total=SIZE,
    actions=541,
    successes=400,
    real_blocked=0,
    real_total=541,
    check_ms=12.0,
    inside_ms=0.0,
)
PREDICTION = Prediction(0.2, 0.669, 0.7)
FOUND = (say("verify.line", line="found something"),)


def done(passed: bool = True, **fields) -> StepDone:
    return StepDone(passed, **fields)


def keys(events) -> list:
    return [e.key if isinstance(e, Say) else e for e in events]


def _checked(events):
    assert_say_renders(events)
    assert events, "a reply must never be empty"
    assert isinstance(events[-1], (AwaitInput, Exit, *EFFECTS)), events
    return events


def at_consent() -> Machine:
    machine = end_machine()
    assert machine.handle("yes") == [CheckKey()]
    _checked(machine.on_event(done(says=(say("ch2.key.ok"),))))
    return machine


def at_code3() -> Machine:
    machine = at_consent()
    assert machine.handle("yes") == [OpenSession(SIZE)]
    events = _checked(machine.on_event(done(session=SESSION)))
    assert ChapterDone(2) in events
    return machine


def at_dry_run() -> Machine:
    machine = at_code3()
    assert machine.handle("yes") == [RunGate(3)]
    events = _checked(machine.on_event(done()))
    assert events[-1] == RunTraffic(Phase.LEARN, SIZE)
    events = _checked(machine.on_event(done(stats=LEARN)))
    assert ChapterDone(3) in events and events[-1] == AwaitInput("continue")
    return machine


def at_code5() -> Machine:
    machine = at_dry_run()
    assert machine.handle("yes") == [WaitWorkflow(Gate.G2)]
    _checked(machine.on_event(done(prediction=PREDICTION)))
    assert machine.handle("yes") == [WaitWorkflow(Gate.G3)]
    events = _checked(machine.on_event(done()))
    assert ChapterDone(4) in events and events[-1] == WaitWorkflow(Gate.G4)
    _checked(machine.on_event(done(stage=1)))
    return machine


def at_results() -> Machine:
    machine = at_code5()
    assert machine.handle("yes") == [RunGate(5)]
    assert _checked(machine.on_event(done()))[-1] == RunReadiness()
    assert _checked(machine.on_event(done()))[-1] == RunTraffic(Phase.PROTECT, SIZE, 1)
    events = _checked(machine.on_event(done(stats=ROUND)))
    assert ChapterDone(5) in events and events[-2] == say("ch6.ask")
    return machine


# -- chapter 2 -----------------------------------------------------------------------


def test_a_key_problem_asks_for_the_key_again():
    machine = end_machine()
    machine.handle("yes")
    events = _checked(machine.on_event(done(False, says=(say("ch2.key.missing"),))))
    assert keys(events) == ["ch2.key.missing", "ch2.key.ask", AwaitInput("continue")]


def test_consent_opens_the_session_and_chapter_3():
    machine = at_consent()
    assert machine.handle("no") == [say("reply.no"), AwaitInput("yes_no")]
    assert machine.handle("yes") == [OpenSession(SIZE)]
    events = _checked(machine.on_event(done(says=(say("ch2.session.ok"),), session=SESSION)))
    assert keys(events) == [
        "ch2.session.ok",
        "achievement.connected",
        ChapterDone(2),
        "chapter.heading",
        "ch3.intro",
        "ch3.task",
        "ch3.lesson",
        "ch3.ask",
        AwaitInput("continue"),
    ]
    assert machine.session == SESSION


def test_a_refused_session_asks_to_try_again():
    machine = at_consent()
    machine.handle("yes")
    events = _checked(machine.on_event(done(False, says=(say("ch2.session.busy"),))))
    assert keys(events) == ["ch2.session.busy", "ch2.session.retry", AwaitInput("continue")]
    assert machine.handle("yes") == [OpenSession(SIZE)]


def test_an_outdated_checkout_exits_with_3_even_interactively():
    machine = at_consent()
    machine.handle("yes")
    events = machine.on_event(done(False, says=(say("online.outdated"),), fatal=True, exit_code=3))
    assert events == [say("online.outdated"), Exit(3)]


# -- chapter 3 ----------------------------------------------------------------------------


def test_a_failing_code_gate_asks_to_fix_and_save():
    machine = at_code3()
    machine.handle("yes")
    events = _checked(machine.on_event(done(False, says=FOUND)))
    assert keys(events) == ["verify.line", "ch3.again", AwaitInput("continue")]


def test_hints_go_from_the_concept_to_the_exact_code():
    machine = at_code3()
    shown = [keys(machine.handle("/hint")) for _ in range(4)]
    assert shown == [
        ["hint.ch3.1", AwaitInput("continue")],
        ["hint.ch3.2", AwaitInput("continue")],
        ["hint.ch3.3", "ch3.task", AwaitInput("continue")],
        ["hint.ch3.3", "ch3.task", AwaitInput("continue")],
    ]


def test_the_last_chapter_5_hint_shows_the_exact_code_too():
    machine = at_code5()
    shown = [keys(machine.handle("/hint")) for _ in range(3)]
    assert shown[-1] == ["hint.ch5.3", "ch5.task", AwaitInput("continue")]


def test_the_top_up_has_a_hint_of_its_own():
    machine = at_code3()
    machine.handle("yes")
    machine.on_event(done())
    machine.on_event(done(stats=LEARN, shortfall=40))
    assert keys(machine.handle("/hint")) == ["hint.ch3.topup", AwaitInput("yes_no")]


def test_a_short_window_offers_a_top_up_run():
    machine = at_code3()
    machine.handle("yes")
    machine.on_event(done())
    events = _checked(machine.on_event(done(stats=LEARN, shortfall=40)))
    assert events[-2] == say("ch3.topup", shortfall=40)
    assert machine.handle("yes") == [RunGate(3)]
    assert machine.on_event(done())[-1] == RunTraffic(Phase.LEARN, 40)
    events = _checked(machine.on_event(done(stats=RunStats(40, 40, 8))))
    assert ChapterDone(3) in events
    # The learning run's own numbers stay the "before" column.
    assert machine.to_dict()["learn"]["actions"] == SIZE


def test_a_failed_learning_run_offers_to_run_it_again_after_the_code_gate():
    machine = at_code3()
    machine.handle("yes")
    machine.on_event(done())
    events = _checked(machine.on_event(done(False, says=(say("run.none"),))))
    assert events[-2] == say("ch3.learn.again")
    assert machine.handle("yes") == [RunGate(3)]


# -- chapter 4 ---------------------------------------------------------------------------------


def test_the_prediction_is_frozen_with_the_lesson_and_the_warning():
    machine = at_dry_run()
    machine.handle("yes")
    events = _checked(machine.on_event(done(prediction=PREDICTION)))
    assert keys(events) == [
        "ch4.prediction",
        "achievement.prediction",
        "ch4.lesson",
        "ch4.warning",
        "ch4.start",
        AwaitInput("continue"),
    ]
    assert machine.to_dict()["prediction"] == {
        "conv_now": 0.2,
        "conv_pred": 0.669,
        "blocked_share": 0.7,
    }


def test_a_workflow_that_isnt_ready_asks_again_with_what_the_runner_found():
    machine = at_dry_run()
    machine.handle("yes")
    events = _checked(machine.on_event(done(False, says=(say("wf.method_wrong"),))))
    assert keys(events) == ["wf.method_wrong", "ch4.ask", AwaitInput("continue")]


# -- chapter 5 ------------------------------------------------------------------------------------


def test_protection_not_on_yet_asks_to_check_again():
    machine = at_code5()
    assert machine.handle("/hint")[0].key == "hint.ch5.1"
    machine = at_dry_run()
    machine.handle("yes")
    machine.on_event(done(prediction=PREDICTION))
    machine.handle("yes")
    machine.on_event(done())
    events = _checked(machine.on_event(done(False, says=(say("wf.not_protecting"),))))
    assert keys(events) == ["wf.not_protecting", "ch5.wait.again", AwaitInput("continue")]
    assert machine.handle("yes") == [WaitWorkflow(Gate.G4)]


def test_readiness_that_doesnt_pass_asks_to_run_it_again_through_the_gates():
    machine = at_code5()
    machine.handle("yes")
    machine.on_event(done())
    events = _checked(machine.on_event(done(False, says=(say("ch5.readiness.diagnose"),))))
    assert events[-2] == say("ch5.readiness.again")
    assert machine.handle("yes") == [RunGate(5)]


def test_a_failed_protected_round_asks_to_run_it_again():
    machine = at_code5()
    machine.handle("yes")
    machine.on_event(done())
    machine.on_event(done())
    events = _checked(machine.on_event(done(False, says=(say("ch5.tripwire", count=2),))))
    assert keys(events) == ["ch5.tripwire", "ch5.round.again", AwaitInput("continue")]


# -- chapter 6 ------------------------------------------------------------------------------------


def _frame_text(events) -> str:
    return "\n".join(text(e.key, **e.params) for e in events if isinstance(e, Say))


def test_the_results_at_stage_1_show_the_stage_line_and_offer_a_re_run():
    machine = at_results()
    assert machine.handle("yes") == [ReadStage()]
    events = _checked(machine.on_event(done(stage=1)))
    shown = _frame_text(events)
    assert "20.0%" in shown and "73.9% ▲" in shown and "0 of 541" in shown
    assert "median 12 ms · 0 ms inside SignalGate" in shown
    assert "20.0% → 66.9%" in shown
    assert text("stage.partial", stage=1) in shown
    assert text("results.framing") in shown
    assert events[-2] == say("ch6.rerun.offer")


def test_the_results_mark_each_degraded_run_and_saved_progress_keeps_the_mark():
    machine = at_code3()
    machine.handle("yes")
    machine.on_event(done())
    events = _checked(machine.on_event(done(stats=replace(LEARN, degraded=True))))
    assert ChapterDone(3) in events
    machine.handle("yes")
    machine.on_event(done(prediction=PREDICTION))
    machine.handle("yes")
    machine.on_event(done())
    machine.on_event(done(stage=1))
    machine.handle("yes")
    machine.on_event(done())
    machine.on_event(done())
    machine.on_event(done(stats=replace(ROUND, degraded=True)))
    saved = machine.to_dict()
    assert saved["learn"]["degraded"] is True and saved["rounds"][0]["degraded"] is True

    later = Machine(saved=saved)
    later.on_event(AppReady())
    later.handle("/continue")
    assert later.handle("yes") == [ReadStage()]
    shown = _frame_text(_checked(later.on_event(done(stage=1))))
    assert text("results.degraded", run=text("results.run.learn")) in shown
    assert text("results.degraded", run=text("results.run.round", round=1)) in shown


def test_the_results_of_smooth_runs_carry_no_mark():
    machine = at_results()
    machine.handle("yes")
    shown = _frame_text(machine.on_event(done(stage=1)))
    assert "was cut short" not in shown


def test_a_run_saved_without_the_mark_loads_as_smooth():
    saved = at_results().to_dict()
    for stats in (saved["learn"], *saved["rounds"]):
        del stats["degraded"]
    later = Machine(saved=saved)
    assert later.to_dict()["rounds"][0]["degraded"] is False
    saved["rounds"][0]["degraded"] = "yes"
    with pytest.raises(ValueError, match="degraded"):
        Machine.from_dict(saved)


def test_no_re_run_goes_to_the_keep_question_and_no_pauses():
    machine = at_results()
    machine.handle("yes")
    machine.on_event(done(stage=1))
    assert machine.handle("no")[-2] == say("ch6.keep")
    assert machine.handle("no") == [Complete(False, 0.2, 400 / 541)]
    events = _checked(machine.on_event(done(says=(say("ch6.paused"),))))
    assert keys(events) == [
        "ch6.paused",
        "complete.card.untimed",
        "ch6.diff",
        "ch6.handoff",
        ChapterDone(6),
        "ch6.done",
        AwaitInput("any"),
    ]


def test_an_unattended_run_declines_only_the_re_run_offer():
    machine = at_results()
    assert machine.unattended_no is False
    machine.handle("yes")
    assert machine.unattended_no is False  # reading the stage
    events = _checked(machine.on_event(done(stage=1)))
    assert events[-2] == say("ch6.rerun.offer")
    assert machine.unattended_no is True
    assert machine.handle("no")[-2] == say("ch6.keep")
    assert machine.unattended_no is False


def test_at_full_protection_the_results_go_straight_to_the_keep_question():
    machine = at_results()
    machine.handle("yes")
    events = _checked(machine.on_event(done(stage=3)))
    assert text("stage.full") in _frame_text(events)
    assert events[-2] == say("ch6.keep")
    assert machine.handle("yes") == [Complete(True, 0.2, 400 / 541)]


def test_the_re_run_waits_for_continue_then_runs_round_2_at_stage_3():
    machine = at_results()
    machine.handle("yes")
    machine.on_event(done(stage=1))
    assert machine.handle("yes") == [say("ch6.rerun.later"), AwaitInput("any")]

    # A later console picks it up from saved progress.
    later = Machine(saved=machine.to_dict())
    later.on_event(AppReady())
    assert later.handle("/continue") == [say("continue.back"), ReadStage()]
    events = _checked(later.on_event(done(stage=2)))
    assert keys(events) == ["stage.partial", "ch6.rerun.anyway", AwaitInput("yes_no")]
    assert later.handle("no") == [say("reply.no"), AwaitInput("yes_no")]
    assert later.handle("yes") == [RunGate(5)]
    assert later.on_event(done())[-1] == RunTraffic(Phase.PROTECT, SIZE, 2)
    events = _checked(later.on_event(done(stats=ROUND)))
    assert "Round 1 and round 2" in _frame_text(events)
    assert events[-2] == say("ch6.keep")


def test_a_paused_workflow_at_the_re_run_asks_to_start_it_again():
    machine = at_results()
    machine.handle("yes")
    machine.on_event(done(stage=1))
    machine.handle("yes")
    assert machine.handle("/continue") == [ReadStage()]
    events = _checked(machine.on_event(done(stage=0, paused=True)))
    assert events[-2] == say("wf.paused")
    assert machine.handle("yes") == [WaitWorkflow(Gate.G4)]
    assert machine.on_event(done(stage=0)) == [ReadStage()]
    assert machine.on_event(done(stage=3)) == [RunGate(5)]


# -- commands in the online chapters --------------------------------------------------------------


def test_status_reads_the_workflow_fresh_and_keeps_the_question_open():
    machine = at_code5()
    before = machine.to_dict()
    assert machine.handle("/status") == [ReadStage()]
    stage_line = say("stage.partial", stage=1)
    events = machine.on_event(done(says=(say("status.workflow", state="active"), stage_line)))
    assert keys(events)[:2] == ["status.chapter", "status.done_ch1"]
    assert keys(events)[-3:] == ["status.workflow", "stage.partial", AwaitInput("continue")]
    assert machine.to_dict()["step"] == before["step"]


def test_report_writes_the_bundle_and_keeps_the_question_open():
    machine = at_code3()
    assert machine.handle("/report") == [WriteReport()]
    assert machine.on_event(done(says=(say("report.written"),))) == [
        say("report.written"),
        AwaitInput("continue"),
    ]


def test_every_online_step_resumes_with_its_own_question():
    for build, kind in (
        (at_consent, "yes_no"),
        (at_code3, "continue"),
        (at_dry_run, "continue"),
        (at_code5, "continue"),
        (at_results, "continue"),
    ):
        machine = build()
        saved = machine.to_dict()
        later = Machine(saved=saved)
        later.on_event(AppReady())
        events = _checked(later.handle("/continue"))
        assert events[0] == say("continue.back"), build.__name__
        assert events[-1] == AwaitInput(kind), build.__name__
        assert later.to_dict() == saved, build.__name__


def test_a_code_step_picked_up_again_shows_its_task_before_its_question():
    for build, again, asked in (
        (at_code3, ["ch3.intro", "ch3.task"], "ch3.ask"),
        (at_code5, ["ch5.task"], "ch5.ask"),
    ):
        saved = build().to_dict()
        later = Machine(saved=saved)
        later.on_event(AppReady())
        events = _checked(later.handle("/continue"))
        assert keys(events) == [
            "continue.back",
            "chapter.heading",
            *again,
            asked,
            AwaitInput("continue"),
        ], build.__name__
        assert later.to_dict() == saved, build.__name__


def test_a_step_with_no_code_picked_up_again_shows_no_task():
    for build in (at_consent, at_dry_run, at_results):
        later = Machine(saved=build().to_dict())
        later.on_event(AppReady())
        events = _checked(later.handle("/continue"))
        assert not [key for key in keys(events) if key in ("ch3.task", "ch5.task")]


def test_an_effect_cut_short_resumes_at_its_question():
    machine = at_code3()
    machine.handle("yes")  # the code gate starts, then the console is killed
    later = Machine(saved=machine.to_dict())
    later.on_event(AppReady())
    events = later.handle("/continue")
    assert events[-2:] == [say("ch3.ask"), AwaitInput("continue")]


def test_starting_over_ends_the_session_a_saved_game_had_open():
    saved = at_code3().to_dict()
    machine = Machine(saved=saved)
    machine.on_event(AppReady())
    events = machine.handle("/start")
    assert events[0] == EndSession(SESSION)


def test_an_unattended_session_exits_on_a_failure_instead_of_asking_again():
    machine = Machine(unattended=True)
    machine.on_event(AppReady())
    machine.handle("/start")
    from tests.machine._scenarios import RUN_FINISHED

    machine.on_event(RUN_FINISHED)
    machine.handle("yes")
    machine.handle("yes")
    events = machine.on_event(done(False, says=(say("ch2.key.missing"),), exit_code=3))
    assert events == [say("ch2.key.missing"), Exit(3)]
