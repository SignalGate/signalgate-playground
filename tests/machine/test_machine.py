"""Every state the machine can be in, times every input it can receive.

States: ``boot`` (before the app answers), ``opening`` (awaiting ``/start``),
``running`` (chapter 1 is sending its offline visitors), ``close`` (awaiting
the yes/no question), ``end`` (after ``yes``: chapter 2 asks for the API
key; chapters 2-6 have their own tests), ``stopped`` (back at the
opening after a run was interrupted), ``app_down`` (the sample app went down
after the opening) and ``offer_pending`` (a saved session is offered at the
opening). For each, every entry of ``COMMANDS``, both answers, garbage text
and blank input are exercised and the exact resulting events are asserted.

The close question is a yes/no question: an empty line asks it again. The
key question after it is a "continue" wait: an empty line goes on, as
``yes`` does.
"""

from __future__ import annotations

import math
import sys

import pytest

from playground import CLIENT_VERSION
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
    CheckKey,
    Exit,
    RunChapter1,
    RunInvalidated,
    SaveState,
    Say,
    ShowLogo,
    StartApp,
    StateSaved,
    StepDone,
    WriteReport,
)
from playground.machine.machine import Machine
from tests.machine._scenarios import (
    BLANK_INPUTS,
    CHAPTER_1_TITLE,
    EXPECTED_ATTACK_IN_TEN,
    EXPECTED_CONVERSION,
    GARBAGE_INPUTS,
    RUN_FINISHED,
    RUN_STOPPED,
    STATE_BUILDERS,
    STOPPED_DONE,
    TOTAL,
    ZERO_ACTION_RUN,
    app_down_machine,
    assert_say_renders,
    boot_machine,
    close_machine,
    continued_from_offer_end_machine,
    continued_from_offer_pending_machine,
    continued_from_offer_running_machine,
    continued_from_offer_start_chapter_done_machine,
    end_machine,
    offer_end_machine,
    offer_pending_machine,
    offer_running_machine,
    offer_start_chapter_done_machine,
    opening_machine,
    running_machine,
    say,
    stopped_machine,
    unattended_running_machine,
)
from tests.machine.test_online_steps import SESSION, at_code5
from tests.machine.test_session_carry_on import carry_on_machine

# What answering yes at the chapter 1 close leads to: the achievement, then
# chapter 2 asks for the key.
CHAPTER_2_OPENS = [
    say("achievement.ch1"),
    ChapterDone(1),
    say("chapter.heading", chapter=2, title=text("chapter.2")),
    say("ch2.intro", version=CLIENT_VERSION),
    say("ch2.key.ask"),
    AwaitInput("continue"),
]

# The close question, as the machine asks it.
CLOSE = say("ch1.close", conversion=EXPECTED_CONVERSION, attack_in_ten=EXPECTED_ATTACK_IN_TEN)


def _assert(machine: Machine, input_text: str, expected: list) -> None:
    result = machine.handle(input_text)
    assert result == expected
    assert_say_renders(result)


# --- start() -----------------------------------------------------------------


def test_start_shows_logo_and_launches_the_app():
    result = boot_machine().start()
    assert result == [ShowLogo(), say("app.starting"), StartApp()]
    assert_say_renders(result)


# --- boot: before AppReady/AppFailed -----------------------------------------


def test_boot_commands():
    tested = set()
    for command, expected in {
        "/start": [say("reply.unknown")],
        "/continue": [say("continue.none")],
        "/hint": [say("hint.opening")],
        "/status": [say("status.not_started"), say("status.done_none")],
        "/help": [say("help")],
        "/report": [WriteReport()],
        "/quit": [SaveState(), say("console.bye"), Exit(0)],
        "/copy": [say("copy.none")],
    }.items():
        tested.add(command)
        _assert(boot_machine(), command, expected)
    assert tested == set(COMMANDS)


def test_boot_answers_are_unknown_before_the_opening_awaits_them():
    for answer in ANSWERS:
        _assert(boot_machine(), answer, [say("reply.unknown")])


def test_boot_garbage_and_blank_are_unknown():
    for garbage in (*GARBAGE_INPUTS, *BLANK_INPUTS):
        _assert(boot_machine(), garbage, [say("reply.unknown")])


def test_boot_app_ready_opens_the_console():
    machine = boot_machine()
    result = machine.on_event(AppReady())
    assert result == [say("app.ready"), say("opening"), AwaitInput("start")]
    assert_say_renders(result)


def test_boot_app_failed_exits():
    machine = boot_machine()
    result = machine.on_event(AppFailed("app.failed"))
    assert result == [say("app.failed"), Exit(3)]
    assert_say_renders(result)


# --- opening: awaiting /start -------------------------------------------------


def test_opening_commands():
    tested = set()
    for command, expected in {
        "/start": [
            say("chapter.heading", chapter=1, title=CHAPTER_1_TITLE),
            say("ch1.intro", total=10_000),
            RunChapter1(),
        ],
        "/continue": [say("continue.none"), AwaitInput("start")],
        "/hint": [say("hint.opening"), AwaitInput("start")],
        "/status": [say("status.not_started"), say("status.done_none"), AwaitInput("start")],
        "/help": [say("help"), AwaitInput("start")],
        "/report": [WriteReport()],
        "/quit": [SaveState(), say("console.bye"), Exit(0)],
        "/copy": [say("copy.none"), AwaitInput("start")],
    }.items():
        tested.add(command)
        _assert(opening_machine(), command, expected)
    assert tested == set(COMMANDS)


def test_opening_answers_are_unknown():
    for answer in ANSWERS:
        _assert(opening_machine(), answer, [say("reply.unknown"), AwaitInput("start")])


def test_opening_garbage_and_blank_are_unknown():
    for garbage in (*GARBAGE_INPUTS, *BLANK_INPUTS):
        _assert(opening_machine(), garbage, [say("reply.unknown"), AwaitInput("start")])


def test_opening_start_uses_the_configured_total():
    machine = Machine(offline_total=1_234)
    machine.on_event(AppReady())
    result = machine.handle("/start")
    assert result == [
        say("chapter.heading", chapter=1, title=CHAPTER_1_TITLE),
        say("ch1.intro", total=1_234),
        RunChapter1(),
    ]
    assert_say_renders(result)


# --- app_down: the sample app went down after the opening --------------------


def test_app_down_commands():
    tested = set()
    for command, expected in {
        "/start": [say("app.not_ready"), AwaitInput("start")],
        "/continue": [say("continue.none"), AwaitInput("start")],
        "/hint": [say("hint.opening"), AwaitInput("start")],
        "/status": [say("status.not_started"), say("status.done_none"), AwaitInput("start")],
        "/help": [say("help"), AwaitInput("start")],
        "/report": [WriteReport()],
        "/quit": [SaveState(), say("console.bye"), Exit(0)],
        "/copy": [say("copy.none"), AwaitInput("start")],
    }.items():
        tested.add(command)
        _assert(app_down_machine(), command, expected)
    assert tested == set(COMMANDS)


def test_app_down_answers_are_unknown():
    for answer in ANSWERS:
        _assert(app_down_machine(), answer, [say("reply.unknown"), AwaitInput("start")])


def test_app_down_garbage_and_blank_are_unknown():
    for garbage in (*GARBAGE_INPUTS, *BLANK_INPUTS):
        _assert(app_down_machine(), garbage, [say("reply.unknown"), AwaitInput("start")])


def test_app_down_does_not_gate_the_close_question():
    """Only /start is gated while the app is down: the close question still
    records chapter 1 as done."""
    machine = close_machine()
    notice = machine.on_event(AppDown())
    assert notice == [say("app.down")]
    result = machine.handle("yes")
    assert result == CHAPTER_2_OPENS


def test_app_down_with_a_known_location():
    machine = opening_machine()
    result = machine.on_event(AppDown("app/routes.py:5"))
    assert result == [say("app.down_at", location="app/routes.py:5")]


def test_app_down_before_the_opening_asks_to_start():
    machine = boot_machine()
    result = machine.on_event(AppDown())
    assert result == [say("app.down"), say("opening"), AwaitInput("start")]


def test_app_reloaded_lifts_the_start_gate():
    """Once the app is back, from a change or a crash respawn, /start runs
    chapter 1 again instead of answering ``app.not_ready``. The save that
    fixed an app that didn't start is worth a line, wherever it lands."""
    for boot, cause, key in ((2, "change", "app.reloaded"), (3, "crash", "app.restarted")):
        machine = app_down_machine()
        notice = machine.on_event(AppReloaded(boot, cause))
        assert notice == [say(key)]
        _assert(
            machine,
            "/start",
            [
                say("chapter.heading", chapter=1, title=CHAPTER_1_TITLE),
                say("ch1.intro", total=TOTAL),
                RunChapter1(),
            ],
        )


def test_app_down_before_the_opening_with_an_offer_pending_offers_it():
    """The app goes down before it ever answered, while a saved session is
    waiting: the console still opens with the resume offer, not the plain
    opening, and the offer survives a gated /start."""
    saved = close_machine().to_dict()
    for location, down in (
        (None, say("app.down")),
        ("app/routes.py:1", say("app.down_at", location="app/routes.py:1")),
    ):
        machine = Machine(saved=saved)
        result = machine.on_event(AppDown(location))
        assert result == [down, say("continue.offer"), AwaitInput("start")]
        assert_say_renders(result)
        assert machine.to_dict() == saved

        _assert(machine, "/start", [say("app.not_ready"), AwaitInput("start")])
        assert machine.to_dict() == saved
        _assert(
            machine,
            "/continue",
            [
                say("continue.back"),
                say(
                    "ch1.close",
                    conversion=EXPECTED_CONVERSION,
                    attack_in_ten=EXPECTED_ATTACK_IN_TEN,
                ),
                AwaitInput("yes_no"),
            ],
        )


def test_app_down_unattended_exits_2():
    machine = Machine(unattended=True)
    result = machine.on_event(AppDown())
    assert result == [say("app.down"), Exit(2)]


# --- offer_pending: a saved session is offered at the opening ----------------


def test_offer_pending_shows_continue_offer():
    machine = Machine(saved=close_machine().to_dict())
    result = machine.on_event(AppReady())
    assert result == [say("app.ready"), say("continue.offer"), AwaitInput("start")]


def test_offer_pending_commands_other_than_start_and_continue():
    tested = {"/start", "/continue"}
    for command, expected in {
        "/hint": [say("hint.opening"), AwaitInput("start")],
        "/status": [say("status.not_started"), say("status.done_none"), AwaitInput("start")],
        "/help": [say("help"), AwaitInput("start")],
        "/report": [WriteReport()],
        "/quit": [SaveState(), say("console.bye"), Exit(0)],
        "/copy": [say("copy.none"), AwaitInput("start")],
    }.items():
        tested.add(command)
        _assert(offer_pending_machine(), command, expected)
    assert tested == set(COMMANDS)


def test_offer_pending_answers_are_unknown():
    saved = close_machine().to_dict()
    for answer in ANSWERS:
        machine = Machine(saved=saved)
        machine.on_event(AppReady())
        result = machine.handle(answer)
        assert result == [say("reply.unknown"), AwaitInput("start")]
        assert machine.to_dict() == saved


def test_offer_pending_garbage_and_blank_are_unknown():
    saved = close_machine().to_dict()
    for garbage in (*GARBAGE_INPUTS, *BLANK_INPUTS):
        machine = Machine(saved=saved)
        machine.on_event(AppReady())
        result = machine.handle(garbage)
        assert result == [say("reply.unknown"), AwaitInput("start")]
        assert machine.to_dict() == saved


def test_offer_pending_to_dict_mirrors_the_saved_snapshot_until_chosen():
    saved = close_machine().to_dict()
    machine = Machine(saved=saved)
    machine.on_event(AppReady())
    assert machine.to_dict() == saved
    machine.handle("/help")
    assert machine.to_dict() == saved


def test_start_discards_a_pending_offer_silently():
    machine = offer_pending_machine()
    result = machine.handle("/start")
    assert result == [
        say("chapter.heading", chapter=1, title=CHAPTER_1_TITLE),
        say("ch1.intro", total=TOTAL),
        RunChapter1(),
    ]
    assert machine.to_dict()["running"] is True


def test_continue_with_no_offer():
    machine = opening_machine()
    result = machine.handle("/continue")
    assert result == [say("continue.none"), AwaitInput("start")]


def test_continue_restores_a_close_pending_snapshot():
    machine = offer_pending_machine()
    result = machine.handle("/continue")
    assert result == [
        say("continue.back"),
        say("ch1.close", conversion=EXPECTED_CONVERSION, attack_in_ten=EXPECTED_ATTACK_IN_TEN),
        AwaitInput("yes_no"),
    ]
    assert machine.to_dict()["awaiting"] == "yes_no"


def test_continue_restores_an_interrupted_run():
    machine = offer_running_machine()
    result = machine.handle("/continue")
    assert result == [
        say("continue.run_interrupted"),
        say("run.again"),
        AwaitInput("start"),
    ]
    assert machine.to_dict()["running"] is False


def test_continue_restores_the_chapter_2_snapshot():
    machine = offer_end_machine()
    result = machine.handle("/continue")
    assert result == [
        say("continue.back"),
        say("chapter.heading", chapter=2, title=text("chapter.2")),
        say("ch2.key.ask"),
        AwaitInput("continue"),
    ]


def test_continue_restores_start_with_chapter1_done():
    machine = offer_start_chapter_done_machine()
    result = machine.handle("/continue")
    assert result == [say("continue.back"), AwaitInput("start")]


def test_continue_after_chapter_1_with_no_later_step_resumes_at_chapter_2():
    # A save whose chapter 1 is done and that waits for a command, with no
    # later step: chapter 2 is next, never an "end of this version" line.
    saved = {
        **Machine().to_dict(),
        "offline_total": TOTAL,
        "awaiting": "any",
        "chapter1_done": True,
        "last_conversion": EXPECTED_CONVERSION,
        "last_attack_in_ten": EXPECTED_ATTACK_IN_TEN,
    }
    machine = Machine(saved=saved)
    machine.on_event(AppReady())
    result = machine.handle("/continue")
    assert result == [
        say("continue.back"),
        say("chapter.heading", chapter=2, title=text("chapter.2")),
        say("ch2.key.ask"),
        AwaitInput("continue"),
    ]
    assert machine.chapter == 2
    assert Machine.from_dict(machine.to_dict()).to_dict() == machine.to_dict()
    assert machine.handle("yes") == [CheckKey()]


# --- running: chapter 1 traffic is in flight ----------------------------------


def test_running_blocks_everything_except_the_four_always_available_commands():
    tested = set()
    for command in COMMANDS:
        if command in ("/help", "/status", "/hint", "/quit"):
            continue
        tested.add(command)
        _assert(running_machine(), command, [say("reply.busy")])
    for answer in ANSWERS:
        _assert(running_machine(), answer, [say("reply.busy")])
    for garbage in (*GARBAGE_INPUTS, *BLANK_INPUTS):
        _assert(running_machine(), garbage, [say("reply.busy")])
    tested |= {"/help", "/status", "/hint", "/quit"}
    assert tested == set(COMMANDS)


def test_running_help_hint_status_quit_still_work():
    _assert(running_machine(), "/help", [say("help")])
    _assert(running_machine(), "/hint", [say("hint.ch1.running")])
    _assert(
        running_machine(),
        "/status",
        [say("status.chapter", chapter=1, title=CHAPTER_1_TITLE), say("status.done_none")],
    )
    _assert(running_machine(), "/quit", [SaveState(), say("console.bye"), Exit(0)])


def test_run_finished_asks_the_close_question():
    machine = running_machine()
    result = machine.on_event(RUN_FINISHED)
    assert result == [
        say("ch1.close", conversion=EXPECTED_CONVERSION, attack_in_ten=EXPECTED_ATTACK_IN_TEN),
        AwaitInput("yes_no"),
    ]
    assert_say_renders(result)
    # The worked example: 20.0% conversion, 7 in 10 the attack.
    assert round(EXPECTED_CONVERSION * 100, 1) == 20.0
    assert EXPECTED_ATTACK_IN_TEN == 7


def test_run_stopped_returns_to_the_opening():
    machine = running_machine()
    result = machine.on_event(RUN_STOPPED)
    assert result == [
        say("ch1.stopped", done=STOPPED_DONE, total=TOTAL),
        AwaitInput("start"),
    ]
    assert_say_renders(result)


def test_change_detected_once_per_run_then_silent():
    machine = running_machine()
    first = machine.on_event(ChangeDetected())
    assert first == [say("reload.deferred")]
    second = machine.on_event(ChangeDetected())
    assert second == []


def test_change_detected_flag_resets_on_the_next_start():
    machine = running_machine()
    machine.on_event(ChangeDetected())
    machine.on_event(RUN_STOPPED)
    machine.handle("/start")
    result = machine.on_event(ChangeDetected())
    assert result == [say("reload.deferred")]


def test_run_invalidated_returns_to_start_not_running():
    machine = running_machine()
    result = machine.on_event(RunInvalidated())
    assert result == [say("run.invalidated"), say("run.again"), AwaitInput("start")]
    assert machine.to_dict()["running"] is False
    assert machine.to_dict()["awaiting"] == "start"


# --- stopped: back at the opening after a run was interrupted ----------------


def test_stopped_behaves_like_the_opening():
    tested = set()
    for command, expected in {
        "/start": [
            say("chapter.heading", chapter=1, title=CHAPTER_1_TITLE),
            say("ch1.intro", total=10_000),
            RunChapter1(),
        ],
        "/continue": [say("continue.none"), AwaitInput("start")],
        "/hint": [say("hint.opening"), AwaitInput("start")],
        "/status": [say("status.not_started"), say("status.done_none"), AwaitInput("start")],
        "/help": [say("help"), AwaitInput("start")],
        "/report": [WriteReport()],
        "/quit": [SaveState(), say("console.bye"), Exit(0)],
        "/copy": [say("copy.none"), AwaitInput("start")],
    }.items():
        tested.add(command)
        _assert(stopped_machine(), command, expected)
    assert tested == set(COMMANDS)
    for answer in ANSWERS:
        _assert(stopped_machine(), answer, [say("reply.unknown"), AwaitInput("start")])
    for garbage in (*GARBAGE_INPUTS, *BLANK_INPUTS):
        _assert(stopped_machine(), garbage, [say("reply.unknown"), AwaitInput("start")])


# --- close: awaiting yes/no ----------------------------------------------------


def test_close_commands():
    tested = set()
    for command, expected in {
        "/start": [say("reply.unknown"), AwaitInput("yes_no")],
        "/continue": [say("continue.none"), AwaitInput("yes_no")],
        "/hint": [say("hint.ch1.close"), AwaitInput("yes_no")],
        "/status": [
            say("status.chapter", chapter=1, title=CHAPTER_1_TITLE),
            say("status.done_none"),
            AwaitInput("yes_no"),
        ],
        "/help": [say("help"), AwaitInput("yes_no")],
        "/report": [WriteReport()],
        "/quit": [SaveState(), say("console.bye"), Exit(0)],
        "/copy": [say("copy.none"), AwaitInput("yes_no")],
    }.items():
        tested.add(command)
        _assert(close_machine(), command, expected)
    assert tested == set(COMMANDS)


def test_close_yes_completes_chapter_1_and_opens_chapter_2():
    machine = close_machine()
    result = machine.handle("yes")
    assert result == CHAPTER_2_OPENS
    assert_say_renders(result)


def test_close_no_keeps_the_same_question_open():
    machine = close_machine()
    before = machine.to_dict()
    result = machine.handle("no")
    assert result == [say("reply.no"), AwaitInput("yes_no")]
    assert_say_renders(result)
    assert machine.to_dict() == before


def test_close_answers_are_case_insensitive():
    for answer in ("YES", "Yes", "yEs"):
        machine = close_machine()
        result = machine.handle(answer)
        assert result == CHAPTER_2_OPENS
    for answer in ("NO", "No", "nO"):
        machine = close_machine()
        result = machine.handle(answer)
        assert result == [say("reply.no"), AwaitInput("yes_no")]


def test_close_garbage_is_unknown():
    for garbage in GARBAGE_INPUTS:
        _assert(close_machine(), garbage, [say("reply.unknown"), AwaitInput("yes_no")])


def test_close_blank_asks_the_question_again_with_its_numbers():
    """A yes/no question has no default: Enter alone never answers it."""
    for blank in BLANK_INPUTS:
        machine = close_machine()
        before = machine.to_dict()
        result = machine.handle(blank)
        assert result == [CLOSE, AwaitInput("yes_no")]
        assert result[0].block == "ask"
        assert_say_renders(result)
        assert machine.to_dict() == before
    machine = close_machine()
    machine.handle("")
    assert machine.handle("yes") == CHAPTER_2_OPENS


# --- end: after yes, chapter 2 asks for the API key ------------------------------


def test_end_commands():
    tested = set()
    for command, expected in {
        "/start": [say("reply.unknown"), AwaitInput("continue")],
        "/continue": [say("continue.none"), AwaitInput("continue")],
        "/hint": [say("hint.ch2.key"), AwaitInput("continue")],
        "/status": [
            say("status.chapter", chapter=2, title=text("chapter.2")),
            say("status.done_ch1", conversion=EXPECTED_CONVERSION),
            AwaitInput("continue"),
        ],
        "/help": [say("help"), AwaitInput("continue")],
        "/report": [WriteReport()],
        "/quit": [SaveState(), say("console.bye"), Exit(0)],
        "/copy": [say("copy.none"), AwaitInput("continue")],
    }.items():
        tested.add(command)
        _assert(end_machine(), command, expected)
    assert tested == set(COMMANDS)


def test_end_answers():
    _assert(end_machine(), "yes", [CheckKey()])
    _assert(end_machine(), "no", [say("reply.no"), AwaitInput("continue")])


def test_end_blank_goes_on_like_yes():
    for blank in BLANK_INPUTS:
        _assert(end_machine(), blank, [CheckKey()])


def test_end_garbage_is_unknown():
    for garbage in GARBAGE_INPUTS:
        _assert(end_machine(), garbage, [say("reply.unknown"), AwaitInput("continue")])


# --- unattended sessions -------------------------------------------------------


def test_unattended_quit_has_no_save_state():
    machine = Machine(unattended=True)
    result = machine.handle("/quit")
    assert result == [say("console.bye"), Exit(0)]


# --- StateSaved: the machine's answer to its own SaveState effect -------------


def test_state_saved_ok_says_saved_when_resumable():
    machine = close_machine()
    result = machine.on_event(StateSaved(True))
    assert result == [say("console.saved")]


def test_state_saved_ok_says_nothing_when_not_resumable():
    machine = opening_machine()
    result = machine.on_event(StateSaved(True))
    assert result == []


def test_state_saved_ok_says_saved_while_an_offer_is_pending():
    """Quitting at the offer saves the offered snapshot back, which is
    resumable by definition, so the save is reported."""
    for build in (
        offer_pending_machine,
        offer_running_machine,
        offer_end_machine,
        offer_start_chapter_done_machine,
    ):
        machine = build()
        assert machine.handle("/quit") == [SaveState(), say("console.bye"), Exit(0)]
        assert machine.on_event(StateSaved(True)) == [say("console.saved")], build.__name__


def test_state_saved_failure_says_save_failed():
    machine = boot_machine()
    result = machine.on_event(StateSaved(False))
    assert result == [say("state.save_failed")]


# --- AppReloaded: the sample app is back up (a notice) ------------------------


def test_a_save_outside_the_edit_steps_reloads_without_a_line():
    """Outside the steps where the user edits the code, a reload a save
    caused says nothing: the app was up before and is up again."""
    for build in (opening_machine, running_machine, close_machine, end_machine, stopped_machine):
        machine = build()
        assert machine.on_event(AppReloaded(2, "change")) == [], build.__name__


def test_a_save_that_fixes_an_app_that_never_started_says_so():
    machine = boot_machine()
    machine.on_event(AppDown("app/routes.py:3"))
    assert machine.on_event(AppReloaded(2, "change")) == [say("app.reloaded")]


def test_app_reloaded_on_a_crash_respawn():
    machine = opening_machine()
    result = machine.on_event(AppReloaded(3, "crash"))
    assert result == [say("app.restarted")]
    assert result[0].params == {}


# --- input is trimmed ----------------------------------------------------------


def test_command_input_is_trimmed():
    _assert(boot_machine(), "  /help  ", [say("help")])


def test_command_matching_is_case_sensitive():
    _assert(boot_machine(), "/HELP", [say("reply.unknown")])


# --- serialisation --------------------------------------------------------------


def test_state_round_trips_through_to_dict_and_from_dict():
    machine = close_machine()
    restored = Machine.from_dict(machine.to_dict())
    assert restored.to_dict() == machine.to_dict()
    assert restored.handle("no") == machine.handle("no")


def test_fresh_state_round_trips():
    machine = boot_machine()
    restored = Machine.from_dict(machine.to_dict())
    assert restored.to_dict() == machine.to_dict()


def test_from_dict_rejects_a_bad_shape():
    base = close_machine().to_dict()
    for key, bad_value in (
        ("offline_total", "10000"),
        ("running", "yes"),
        ("awaiting", "later"),
        ("chapter1_done", 1),
        ("last_conversion", "20%"),
        ("last_attack_in_ten", "seven"),
    ):
        broken = dict(base)
        broken[key] = bad_value
        with pytest.raises(ValueError):
            Machine.from_dict(broken)
    for key in base:
        broken = dict(base)
        del broken[key]
        with pytest.raises(ValueError):
            Machine.from_dict(broken)


def test_from_dict_rejects_a_close_question_missing_a_number():
    """``awaiting == "yes_no"`` needs both numbers to render."""
    base = close_machine().to_dict()
    assert base["awaiting"] == "yes_no"
    for key in ("last_conversion", "last_attack_in_ten"):
        broken = dict(base)
        broken[key] = None
        with pytest.raises(ValueError):
            Machine.from_dict(broken)


def test_from_dict_rejects_a_done_chapter_with_no_conversion():
    """``chapter1_done`` needs a conversion to render ``status.done_ch1``."""
    base = end_machine().to_dict()
    assert base["chapter1_done"] is True
    broken = dict(base)
    broken["last_conversion"] = None
    with pytest.raises(ValueError):
        Machine.from_dict(broken)


def test_from_dict_rejects_numbers_no_finished_run_could_report():
    """A conversion is a fraction of actions and the attack is counted out of
    ten; anything else, including an integer too large to be a float at all,
    is a bad shape (a ``ValueError``, never an ``OverflowError``)."""
    base = close_machine().to_dict()
    too_large_for_a_float = 2**sys.float_info.max_exp
    for key, bad_value in (
        ("last_conversion", too_large_for_a_float),
        ("last_conversion", -too_large_for_a_float),
        ("last_conversion", -EXPECTED_CONVERSION),
        ("last_conversion", 1 + EXPECTED_CONVERSION),
        ("last_conversion", math.inf),
        ("last_conversion", -math.inf),
        ("last_conversion", math.nan),
        ("last_conversion", True),
        ("last_attack_in_ten", -EXPECTED_ATTACK_IN_TEN),
        ("last_attack_in_ten", 2 * EXPECTED_ATTACK_IN_TEN),
        ("last_attack_in_ten", too_large_for_a_float),
        ("last_attack_in_ten", float(EXPECTED_ATTACK_IN_TEN)),
        ("last_attack_in_ten", True),
        ("offline_total", True),
    ):
        broken = dict(base)
        broken[key] = bad_value
        with pytest.raises(ValueError):
            Machine.from_dict(broken)
        with pytest.raises(ValueError):
            Machine(saved=broken)


def test_from_dict_accepts_the_ends_of_each_range():
    base = close_machine().to_dict()
    for conversion in (0, 1, 0.0, 1.0):
        for attack_in_ten in (0, 10):
            snapshot = dict(base, last_conversion=conversion, last_attack_in_ten=attack_in_ten)
            assert Machine.from_dict(snapshot).to_dict() == snapshot
            assert Machine(saved=snapshot).to_dict() == snapshot


def test_from_dict_rejects_an_unknown_key():
    """Only the keys ``to_dict`` writes: otherwise a machine offered this
    snapshot would serialise back to something other than what was saved."""
    broken = dict(close_machine().to_dict())
    broken["extra"] = "x"
    with pytest.raises(ValueError):
        Machine.from_dict(broken)
    with pytest.raises(ValueError):
        Machine(saved=broken)


def test_from_dict_rejects_anything_but_a_mapping():
    for not_a_mapping in (None, [], "snapshot", 1, list(close_machine().to_dict())):
        with pytest.raises(ValueError):
            Machine.from_dict(not_a_mapping)


# --- the input line always comes back -----------------------------------------


def test_every_reply_hands_the_input_line_back_or_moves_on():
    """A reply in a waiting state must end by asking for input again, exiting,
    or starting a run -- otherwise the console would have nothing left to do."""
    builders = (
        opening_machine,
        stopped_machine,
        close_machine,
        end_machine,
        app_down_machine,
        offer_pending_machine,
        offer_running_machine,
        offer_end_machine,
        offer_start_chapter_done_machine,
        continued_from_offer_pending_machine,
        continued_from_offer_running_machine,
        continued_from_offer_end_machine,
        continued_from_offer_start_chapter_done_machine,
        carry_on_machine,
    )
    inputs = (*COMMANDS, *ANSWERS, *GARBAGE_INPUTS, *BLANK_INPUTS, "/copy 2")
    for build in builders:
        for input_text in inputs:
            result = build().handle(input_text)
            assert result, (build.__name__, input_text)
            assert isinstance(result[-1], (AwaitInput, Exit, *EFFECTS)), (
                build.__name__,
                input_text,
                result,
            )


def test_every_turn_reply_hands_the_input_line_back_or_moves_on():
    """A turn reply is also every ``on_event`` result that ends a step of
    the session -- ``AppReady`` (offered or not), ``AppFailed``,
    ``RunFinished`` (including zero actions), ``RunStopped``,
    ``RunInvalidated``, ``AppDown`` before the opening, and any ``AppDown``
    when unattended. Each must still end with ``AwaitInput``, ``Exit`` or an
    effect."""

    def assert_turn_reply(result: list) -> None:
        assert result
        last = result[-1]
        is_effect = isinstance(last, (StartApp, RunChapter1, ChapterDone, SaveState))
        assert isinstance(last, (AwaitInput, Exit)) or is_effect, result

    assert_turn_reply(boot_machine().on_event(AppReady()))
    assert_turn_reply(Machine(saved=close_machine().to_dict()).on_event(AppReady()))

    assert_turn_reply(boot_machine().on_event(AppFailed("app.failed")))

    assert_turn_reply(running_machine().on_event(RUN_FINISHED))
    assert_turn_reply(running_machine().on_event(ZERO_ACTION_RUN))
    assert_turn_reply(unattended_running_machine().on_event(ZERO_ACTION_RUN))

    assert_turn_reply(running_machine().on_event(RUN_STOPPED))

    assert_turn_reply(running_machine().on_event(RunInvalidated()))

    # AppDown before the opening, with and without a saved session offered.
    assert_turn_reply(boot_machine().on_event(AppDown()))
    assert_turn_reply(boot_machine().on_event(AppDown("app/routes.py:1")))
    saved = close_machine().to_dict()
    assert_turn_reply(Machine(saved=saved).on_event(AppDown()))
    assert_turn_reply(Machine(saved=saved).on_event(AppDown("app/routes.py:1")))

    # AppDown when unattended: Exit(2) regardless of whether the opening has
    # already happened.
    assert_turn_reply(Machine(unattended=True).on_event(AppDown()))
    opened_unattended = Machine(unattended=True)
    opened_unattended.on_event(AppReady())
    assert_turn_reply(opened_unattended.on_event(AppDown()))

    # A step that found its session closed: the carry-on wait, after an
    # effect or after /status, or the exit of an unattended run; and a new
    # session the carry-on couldn't open.
    closed = StepDone(False, session_closed=True)
    running = at_code5()
    running.handle("yes")
    assert_turn_reply(running.on_event(closed))
    reading = at_code5()
    reading.handle("/status")
    assert_turn_reply(reading.on_event(closed))
    unattended = Machine(unattended=True)
    unattended.on_event(AppReady())
    unattended.handle("/start")
    unattended.on_event(RUN_FINISHED)
    unattended.handle("yes")
    unattended.handle("yes")
    unattended.on_event(StepDone(True))
    unattended.handle("yes")
    unattended.on_event(StepDone(True, session=SESSION))
    unattended.handle("yes")
    assert_turn_reply(unattended.on_event(closed))
    reopening = carry_on_machine()
    reopening.handle("")
    assert_turn_reply(reopening.on_event(StepDone(False, (Say("ch2.session.busy"),), exit_code=4)))


# --- notices never change to_dict() and hold only Say events -----------------


def test_notices_hold_only_say_events_and_never_change_state():
    notices = [
        AppReloaded(1, "change"),
        AppReloaded(2, "crash"),
        ChangeDetected(),
        StateSaved(True),
        StateSaved(False),
    ]
    for name, build in STATE_BUILDERS.items():
        for notice in notices:
            machine = build()
            before = machine.to_dict()
            result = machine.on_event(notice)
            assert all(isinstance(event, Say) for event in result), (name, notice, result)
            assert machine.to_dict() == before, (name, notice)
        if name == "boot":
            # AppDown before the opening is a turn reply, not a notice: it
            # opens the console for the first time, so to_dict() changes.
            continue
        machine = build()
        before = machine.to_dict()
        result = machine.on_event(AppDown())
        assert all(isinstance(event, Say) for event in result), (name, "AppDown", result)
        assert machine.to_dict() == before, (name, "AppDown")
