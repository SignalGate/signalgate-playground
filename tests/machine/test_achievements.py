"""Achievement lines, the time the console measured, and the completion card.

Each achievement is shown once per session, right after the ✓ lines of the
step that earned it and before its chapter is marked done; a re-run never
shows one again. The set is saved with the progress; a snapshot saved
before it was kept gets it back from its progress. The time from ``/start``
to the results comes from the runner's stamps and ends up on the card.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from playground.machine.events import (
    AppReady,
    AwaitInput,
    ChapterDone,
    CheckKey,
    Complete,
    Event,
    Gate,
    OpenSession,
    Phase,
    ReadStage,
    RunFinished,
    RunGate,
    RunReadiness,
    RunStats,
    RunTraffic,
    Say,
    StepDone,
    WaitWorkflow,
)
from playground.machine.machine import Machine
from playground.machine.steps import _ACHIEVEMENTS
from tests.machine._scenarios import RUN_FINISHED, assert_say_renders, close_machine, say
from tests.machine.test_online_steps import (
    LEARN,
    PREDICTION,
    ROUND,
    SESSION,
    SIZE,
    at_code3,
    at_results,
    done,
)

OK = say("verify.line", line="✓ checked")


def keys(events: list[Event]) -> list[object]:
    return [event.key if isinstance(event, Say) else event for event in events]


def achievements_in(events: list[Event]) -> list[str]:
    return [e.key for e in events if isinstance(e, Say) and e.key.startswith("achievement.")]


class Run:
    """A console driven through the whole tutorial, every event kept."""

    def __init__(self, machine: Machine | None = None) -> None:
        self.machine = machine or Machine()
        self.events: list[Event] = []

    def handle(self, text: str) -> list[Event]:
        events = self.machine.handle(text)
        assert_say_renders(events)
        self.events += events
        return events

    def feed(self, event: Event) -> list[Event]:
        events = self.machine.on_event(event)
        assert_say_renders(events)
        self.events += events
        return events


def full_run(*, stamps: bool = True) -> tuple[Run, dict[str, list[Event]]]:
    """A first run from the opening to the end: no re-run, keep the workflow.

    Returns the run and, per step that earns an achievement, what it said.
    """

    def at(seconds: float) -> dict[str, float]:
        return {"elapsed_s": seconds} if stamps else {}

    run = Run()
    by_step: dict[str, list[Event]] = {}
    run.feed(AppReady())
    run.handle("/start")
    run.feed(replace(RUN_FINISHED, **at(60.0)))
    by_step["close"] = run.handle("yes")
    assert run.handle("") == [CheckKey()]
    run.feed(done(says=(say("ch2.key.ok"),), **at(120.0)))
    assert run.handle("yes") == [OpenSession(SIZE)]
    by_step["session"] = run.feed(done(says=(say("ch2.session.ok"),), session=SESSION))
    assert run.handle("") == [RunGate(3)]
    by_step["gate3"] = run.feed(done(says=(OK,), **at(300.0)))
    by_step["learn"] = run.feed(done(says=(say("run.none"),), stats=LEARN, **at(420.0)))
    assert run.handle("") == [WaitWorkflow(Gate.G2)]
    by_step["dry_run"] = run.feed(done(prediction=PREDICTION))
    assert run.handle("") == [WaitWorkflow(Gate.G3)]
    by_step["start"] = run.feed(done())
    run.feed(done(stage=1))
    assert run.handle("") == [RunGate(5)]
    by_step["gate5"] = run.feed(done(says=(OK,), **at(900.0)))
    assert by_step["gate5"][-1] == RunReadiness()
    run.feed(done())
    by_step["round"] = run.feed(done(stats=ROUND, **at(1200.0)))
    assert run.handle("") == [ReadStage()]
    by_step["results"] = run.feed(done(stage=1, **at(1500.0)))
    run.handle("no")  # no re-run
    assert run.handle("yes") == [Complete(True, 0.2, 400 / 541)]
    by_step["complete"] = run.feed(done(says=(say("ch6.kept"),), **at(1800.0)))
    return run, by_step


def _between(events: list[Event], key: str, after: list[object], before: object | None) -> None:
    """``key`` comes after every item of ``after`` and before ``before`` in ``events``."""
    shown = keys(events)
    at = shown.index(key)
    for item in after:
        assert shown.index(item) < at, (key, item, shown)
    if before is not None:
        assert at < shown.index(before), (key, before, shown)


# -- once each, in its place ---------------------------------------------------------------------


def test_a_full_run_shows_each_achievement_exactly_once():
    run, _ = full_run()
    shown = achievements_in(run.events)
    assert sorted(shown) == sorted(_ACHIEVEMENTS)
    assert len(shown) == len(set(shown)) == 7
    assert run.machine.to_dict()["achievements"] == sorted(_ACHIEVEMENTS)


def test_each_achievement_follows_its_steps_lines_and_comes_before_its_chapter_is_done():
    _, by_step = full_run()
    _between(by_step["close"], "achievement.ch1", [], ChapterDone(1))
    _between(by_step["session"], "achievement.connected", ["ch2.session.ok"], ChapterDone(2))
    _between(by_step["gate3"], "achievement.ch3", ["verify.line"], "ch3.learn.intro")
    _between(by_step["learn"], "achievement.learned", ["run.none", "ch3.done"], ChapterDone(3))
    _between(by_step["dry_run"], "achievement.prediction", ["ch4.prediction"], "ch4.lesson")
    _between(by_step["gate5"], "achievement.ch5", ["verify.line"], "ch5.readiness.intro")
    _between(by_step["results"], "achievement.results", ["results.frame"], "ch6.rerun.offer")
    assert achievements_in(by_step["start"]) == []
    assert achievements_in(by_step["round"]) == []


def test_the_chapter_entries_say_what_to_do_in_order():
    _, by_step = full_run()
    session = keys(by_step["session"])
    at = session.index("chapter.heading")
    assert session[at:] == [
        "chapter.heading",
        "ch3.intro",
        "ch3.task",
        "ch3.lesson",
        "ch3.ask",
        AwaitInput("continue"),
    ]
    start = keys(by_step["start"])
    at = start.index("chapter.heading")
    assert start[at:] == [
        "chapter.heading",
        "ch5.meanwhile",
        "ch5.task",
        "ch5.lesson",
        WaitWorkflow(Gate.G4),
    ]


def test_one_heading_per_chapter_and_none_in_the_other_lines():
    run, _ = full_run()
    headings = [
        e.params["chapter"] for e in run.events if isinstance(e, Say) and e.key == "chapter.heading"
    ]
    assert headings == [1, 2, 3, 4, 5, 6]


def test_a_retried_learning_run_does_not_repeat_the_code_gate_achievement():
    run = Run(at_code3())
    run.handle("")
    assert achievements_in(run.feed(done(says=(OK,)))) == ["achievement.ch3"]
    run.feed(done(False, says=(say("run.none"),)))
    assert run.handle("") == [RunGate(3)]
    again = run.feed(done(says=(OK,)))
    assert achievements_in(again) == []
    assert again[-1] == RunTraffic(Phase.LEARN, SIZE)
    assert achievements_in(run.feed(done(stats=LEARN))) == ["achievement.learned"]


def test_a_top_up_repeats_nothing_and_learning_is_earned_once_it_completes():
    run = Run(at_code3())
    run.handle("")
    run.feed(done())
    topup = run.feed(done(stats=LEARN, shortfall=40))
    assert achievements_in(topup) == []
    assert run.handle("yes") == [RunGate(3)]
    assert achievements_in(run.feed(done())) == []
    assert achievements_in(run.feed(done(stats=RunStats(40, 40, 8)))) == ["achievement.learned"]


def test_round_2_repeats_neither_the_gate_nor_the_results():
    run = Run(at_results())
    run.handle("")
    assert achievements_in(run.feed(done(stage=1))) == ["achievement.results"]
    run.handle("yes")  # re-run later
    assert run.handle("/continue") == [ReadStage()]
    run.feed(done(stage=2))
    assert run.handle("yes") == [RunGate(5)]
    gate = run.feed(done(says=(OK,)))
    assert achievements_in(gate) == []
    assert gate[-1] == RunTraffic(Phase.PROTECT, SIZE, 2)
    keep = run.feed(done(stats=ROUND))
    assert "results.frame.two" in keys(keep)
    assert achievements_in(keep) == []
    assert keys(keep)[-2] == "ch6.keep"


def test_a_readiness_retry_does_not_repeat_the_chapter_5_achievement():
    saved = at_results().to_dict()
    earned = [key for key in saved["achievements"] if key != "achievement.ch5"]
    run = _resume({**saved, "step": "CODE5", "achievements": earned})
    run.handle("")
    assert achievements_in(run.feed(done())) == ["achievement.ch5"]
    run.feed(done(False, says=(say("ch5.readiness.diagnose"),)))
    run.handle("")
    assert achievements_in(run.feed(done())) == []


# -- saved progress ---------------------------------------------------------------------------------


def _without_the_new_keys(machine: Machine) -> dict:
    return {k: v for k, v in machine.to_dict().items() if k not in ("achievements", "elapsed_s")}


def _resume(snapshot: dict) -> Run:
    run = Run(Machine(saved=snapshot))
    run.feed(AppReady())
    run.handle("/continue")
    return run


def test_an_older_snapshot_at_the_top_up_has_earned_the_gate_but_not_the_learning():
    machine = at_code3()
    machine.handle("")
    machine.on_event(done())
    machine.on_event(done(stats=LEARN, shortfall=40))
    older = _without_the_new_keys(machine)
    assert older["step"] == "TOPUP"

    run = _resume(older)
    assert run.machine.to_dict()["achievements"] == [
        "achievement.ch1",
        "achievement.ch3",
        "achievement.connected",
    ]
    assert run.machine.elapsed_s is None
    run.handle("yes")
    run.feed(done())
    run.feed(done(stats=RunStats(40, 40, 8)))
    assert achievements_in(run.events) == ["achievement.learned"]


def _script(run: Run) -> list:
    """A whole run as steps that each end at a question: (step it asks, what to do)."""
    return [
        ("KEY", lambda: run.handle("yes")),
        ("CONSENT", lambda: (run.handle(""), run.feed(done()))),
        ("CODE3", lambda: (run.handle("yes"), run.feed(done(session=SESSION)))),
        ("LEARN", lambda: (run.handle(""), run.feed(done()), run.feed(done(False)))),
        (
            "TOPUP",
            lambda: (run.handle(""), run.feed(done()), run.feed(done(stats=LEARN, shortfall=40))),
        ),
        ("DRY_RUN", lambda: (run.handle("yes"), run.feed(done()), run.feed(done(stats=LEARN)))),
        ("START", lambda: (run.handle(""), run.feed(done(prediction=PREDICTION)))),
        ("CODE5", lambda: (run.handle(""), run.feed(done()), run.feed(done(stage=1)))),
        ("READINESS", lambda: (run.handle(""), run.feed(done()), run.feed(done(False)))),
        (
            "RESULTS",
            lambda: (
                run.handle(""),
                run.feed(done()),
                run.feed(done()),
                run.feed(done(stats=ROUND)),
            ),
        ),
        ("RERUN_OFFER", lambda: (run.handle(""), run.feed(done(stage=1)))),
        ("KEEP", lambda: run.handle("no")),
        ("DONE", lambda: (run.handle("yes"), run.feed(done()))),
    ]


@pytest.mark.parametrize("point", range(13))
def test_an_older_snapshot_reads_what_it_earned_from_its_progress(point):
    """Along a whole run, at every question, a snapshot without the saved set
    earns back exactly the achievements the live session had shown by then."""
    run = Run()
    run.feed(AppReady())
    run.handle("/start")
    run.feed(RUN_FINISHED)
    script = _script(run)
    for _step, act in script[: point + 1]:
        act()
    live = run.machine.to_dict()
    assert (live["step"], live["running"]) == (script[point][0], False)
    older = _without_the_new_keys(run.machine)
    assert Machine.from_dict(older).to_dict()["achievements"] == live["achievements"]


def test_an_older_snapshot_goes_on_to_the_end_without_repeating_anything():
    machine = at_results()
    older = _without_the_new_keys(machine)
    run = _resume(older)
    run.handle("")
    run.feed(done(stage=3))
    run.handle("yes")
    run.feed(done(says=(say("ch6.kept"),)))
    assert achievements_in(run.events) == ["achievement.results"]
    assert "complete.card.untimed" in keys(run.events)


# -- the time and the card -----------------------------------------------------------------------------


def test_the_card_shows_the_minutes_from_start_to_the_results():
    run, by_step = full_run()
    card = [e for e in by_step["complete"] if isinstance(e, Say) and e.key.startswith("complete.")]
    # Stamped 1500 s when the results were shown; later stamps don't count.
    assert card == [say("complete.card", minutes=25)]
    assert card[0].block == "card"
    assert keys(by_step["complete"]) == [
        "ch6.kept",
        "complete.card",
        "ch6.diff",
        "ch6.handoff",
        ChapterDone(6),
        "ch6.done",
        AwaitInput("any"),
    ]
    assert run.machine.elapsed_s == 1500.0
    assert run.machine.to_dict()["elapsed_s"] == 1500.0


def test_without_stamps_the_card_has_no_time():
    _, by_step = full_run(stamps=False)
    assert "complete.card.untimed" in keys(by_step["complete"])
    assert "complete.card" not in keys(by_step["complete"])


def test_a_short_session_still_shows_a_minute():
    run = Run(at_results())
    run.handle("")
    run.feed(done(stage=3, elapsed_s=10.0))
    run.handle("no")
    events = run.feed(done(says=(say("ch6.paused"),)))
    assert say("complete.card", minutes=1) in events


def test_the_chapter_1_run_stamps_the_time_by_the_close_question():
    machine = Machine()
    machine.on_event(AppReady())
    machine.handle("/start")
    machine.on_event(replace(RUN_FINISHED, elapsed_s=60.0))
    assert machine.to_dict()["awaiting"] == "yes_no"
    assert machine.to_dict()["elapsed_s"] == 60.0
    assert machine.elapsed_s == 60.0


def test_a_stamp_the_runner_could_not_take_keeps_the_last_one():
    machine = close_machine()
    machine.on_event(RunFinished(10, 10, 2, 8, 2, elapsed_s=None))
    assert machine.elapsed_s is None
    machine = at_code3()
    machine.handle("")
    machine.on_event(StepDone(True, elapsed_s=42.0))
    machine.on_event(StepDone(True, elapsed_s=None))
    assert machine.to_dict()["elapsed_s"] == 42.0


def test_the_time_is_saved_and_picked_up_again():
    machine = at_code3()
    snapshot = {**machine.to_dict(), "elapsed_s": 100.0}
    run = _resume(snapshot)
    assert run.machine.elapsed_s == 100.0
    run.handle("")
    run.feed(done(elapsed_s=130.0))
    assert run.machine.elapsed_s == 130.0
