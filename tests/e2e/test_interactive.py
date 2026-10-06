"""End-to-end tests for the interactive console, driven through a real terminal.

The console runs in a pseudo-terminal, so it takes the interactive path
(Rich output, prompt_toolkit input) exactly as it does for a person. These
tests type commands and keys the way a user would, save files in the sample
app while the console waits or runs, stop or kill processes, and check what
the console says and that it always ends cleanly: exit code 0, no traceback,
and no sample app left running (the ``tracked_copy`` fixture checks that
last part for every test).

The start-up hook (``tests/e2e/seam.py``) gives each sample app a boot file,
so a test knows the app's process id and can read its health. Outside the
steps where the user edits the code a reload prints nothing, so these tests
watch the boot files to know the app came back.
"""

from __future__ import annotations

import os
import signal
import sys
import time

import filelock
import pytest

from playground.copy.en import text
from playground.traffic.local_constants import RELOAD_DEBOUNCE_S
from tests.e2e.conftest import (
    BYE,
    CLOSE_QUESTION,
    CTRL_C,
    CTRL_D,
    DUMMY_KEY,
    EXIT_WITHIN_S,
    HELP_LAST_COMMAND,
    OPENING,
    PROMPT,
    READY_WITHIN_S,
    REPLY_WITHIN_S,
    RUN_WITHIN_S,
    SAVED,
    append_comment,
    command_of,
    ended_cleanly,
    flatten,
    owned_app_pids,
    wait_for_run_panel,
    wait_pids_gone,
    wait_until_gone,
)
from tests.e2e.seam import read_health, wait_for_health

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(sys.platform == "win32", reason="needs a POSIX pseudo-terminal"),
]

WELCOME_BACK = "Welcome back. Type /continue"
RELOADED = text("app.reloaded")
KEY_QUESTION = text("ch2.key.ask")
NO_KEY_YET = text("ch2.key.missing")
ENTER = b"\r"

# The console's own kill-9 recovery: the orphaned app notices its lifeline
# is gone and exits by itself.
ORPHAN_GONE_WITHIN_S = 10.0


# -- the prompt ----------------------------------------------------------------------


def test_informational_commands_keep_the_session_open(open_terminal, tracked_copy, seam):
    terminal = open_terminal(seam.env(tracked_copy))
    terminal.expect(OPENING, READY_WITHIN_S)

    terminal.type("/help")
    terminal.expect(HELP_LAST_COMMAND, REPLY_WITHIN_S)
    terminal.type("/status")
    terminal.expect("You haven't started yet.", REPLY_WITHIN_S)
    terminal.type("/hint")
    terminal.expect("Type /start to begin.", REPLY_WITHIN_S)
    terminal.type("what now")
    terminal.expect("I understand yes, no and /commands.", REPLY_WITHIN_S)

    terminal.type("/quit")
    terminal.expect(BYE, REPLY_WITHIN_S)
    assert terminal.wait_exit(EXIT_WITHIN_S) == 0
    # Nothing worth resuming yet, so nothing is announced as saved.
    assert flatten(SAVED) not in terminal.flat
    ended_cleanly(terminal, tracked_copy)


def test_ctrl_c_at_an_idle_prompt_exits_cleanly(open_terminal, tracked_copy, seam):
    terminal = open_terminal(seam.env(tracked_copy))
    terminal.expect(OPENING, READY_WITHIN_S)

    terminal.press(CTRL_C)  # typed at the prompt: quit now

    assert terminal.wait_exit(EXIT_WITHIN_S) == 0
    ended_cleanly(terminal, tracked_copy)


def test_ctrl_d_at_an_empty_prompt_quits_like_quit(open_terminal, tracked_copy, seam):
    terminal = open_terminal(seam.env(tracked_copy))
    terminal.expect(OPENING, READY_WITHIN_S)
    # Wait for the prompt itself: a Ctrl-D sent before the prompt takes the
    # terminal is swallowed by the line discipline as end of input.
    terminal.expect(PROMPT, REPLY_WITHIN_S)

    terminal.press(CTRL_D)

    terminal.expect(BYE, REPLY_WITHIN_S)
    assert terminal.wait_exit(EXIT_WITHIN_S) == 0
    assert (tracked_copy / ".playground" / "state.json").exists()
    ended_cleanly(terminal, tracked_copy)


def test_ctrl_c_stops_a_run_after_a_prompt_and_a_second_exits(open_terminal, tracked_copy, seam):
    terminal = open_terminal(seam.env(tracked_copy))
    terminal.expect(OPENING, READY_WITHIN_S)
    terminal.type("/start")
    wait_for_run_panel(terminal)

    terminal.press(CTRL_C)  # the run is going: this Ctrl-C stops it
    terminal.expect("press Ctrl-C again", REPLY_WITHIN_S)
    terminal.expect("Run stopped after", REPLY_WITHIN_S)

    terminal.press(CTRL_C)  # back at the prompt: this one exits
    assert terminal.wait_exit(EXIT_WITHIN_S) == 0
    ended_cleanly(terminal, tracked_copy)


# -- Enter and yes ---------------------------------------------------------------------


def _at_the_key_question(terminal) -> None:
    """From the opening: chapter 1, Enter at the close (asked again), yes, the key question."""
    terminal.expect(OPENING, READY_WITHIN_S)
    terminal.type("/start")
    terminal.expect(CLOSE_QUESTION, RUN_WITHIN_S)
    terminal.expect(PROMPT, REPLY_WITHIN_S)

    terminal.press(ENTER)  # a yes/no question has no default: it is asked again
    terminal.expect(CLOSE_QUESTION, REPLY_WITHIN_S)
    assert terminal.count(CLOSE_QUESTION) == 2
    assert flatten(KEY_QUESTION) not in terminal.flat
    terminal.expect(PROMPT, REPLY_WITHIN_S)

    terminal.type("yes")
    terminal.expect(KEY_QUESTION, REPLY_WITHIN_S)
    terminal.expect(PROMPT, REPLY_WITHIN_S)


def test_enter_checks_the_key_but_only_asks_the_close_again(open_terminal, tracked_copy, seam):
    env = seam.env(tracked_copy)
    env.pop("SIGNALGATE_API_KEY", None)  # the key comes from app/.env only
    terminal = open_terminal(env)
    _at_the_key_question(terminal)

    terminal.press(ENTER)  # "Press Enter when it's saved": the key check runs
    terminal.expect(NO_KEY_YET, REPLY_WITHIN_S)
    terminal.expect(KEY_QUESTION, REPLY_WITHIN_S)
    assert (terminal.count(NO_KEY_YET), terminal.count(KEY_QUESTION)) == (1, 2)
    terminal.expect(PROMPT, REPLY_WITHIN_S)

    terminal.type("yes")  # yes still goes on too
    terminal.expect(NO_KEY_YET, REPLY_WITHIN_S)
    terminal.expect(KEY_QUESTION, REPLY_WITHIN_S)
    assert (terminal.count(NO_KEY_YET), terminal.count(KEY_QUESTION)) == (2, 3)
    assert terminal.count(CLOSE_QUESTION) == 2
    terminal.expect(PROMPT, REPLY_WITHIN_S)

    terminal.press(CTRL_D)
    terminal.expect(BYE, REPLY_WITHIN_S)
    assert terminal.wait_exit(EXIT_WITHIN_S) == 0
    assert (tracked_copy / ".playground" / "state.json").exists()
    ended_cleanly(terminal, tracked_copy)


def test_ctrl_c_at_the_key_question_exits_cleanly(open_terminal, tracked_copy, seam):
    terminal = open_terminal(seam.env(tracked_copy))
    _at_the_key_question(terminal)

    terminal.press(CTRL_C)

    assert terminal.wait_exit(EXIT_WITHIN_S) == 0
    ended_cleanly(terminal, tracked_copy)


# -- saving files --------------------------------------------------------------------


def test_a_save_at_the_prompt_reloads_and_keeps_the_typed_text(open_terminal, tracked_copy, seam):
    terminal = open_terminal(seam.env(tracked_copy))
    terminal.expect(OPENING, READY_WITHIN_S)
    (boot1,) = seam.wait_for_boots(1, READY_WITHIN_S)

    terminal.press(b"/he")  # typed, not sent
    append_comment(tracked_copy / "app" / "routes.py")
    seam.wait_for_boots(2, READY_WITHIN_S)

    terminal.type("lp")
    terminal.expect(HELP_LAST_COMMAND, REPLY_WITHIN_S)
    # At the opening a reload is no news: nothing about it on screen.
    assert terminal.count(RELOADED) == 0

    boot1_, boot2 = seam.wait_for_boots(2, READY_WITHIN_S)
    assert boot1_["pid"] == boot1["pid"]
    assert boot2["pid"] != boot1["pid"]
    assert wait_pids_gone([boot1["pid"]], EXIT_WITHIN_S) == set()
    assert owned_app_pids(tracked_copy, seam.pids()) == {boot2["pid"]}
    assert read_health(boot2)["boot_id"] == boot2["boot_id"] != boot1["boot_id"]

    terminal.press(CTRL_C)
    assert terminal.wait_exit(EXIT_WITHIN_S) == 0
    ended_cleanly(terminal, tracked_copy)


def test_setting_the_key_reloads_once_with_the_new_key(open_terminal, tracked_copy, seam):
    terminal = open_terminal(seam.env(tracked_copy))
    terminal.expect(OPENING, READY_WITHIN_S)
    (boot1,) = seam.wait_for_boots(1, READY_WITHIN_S)
    assert read_health(boot1)["key"] == "placeholder"

    (tracked_copy / "app" / ".env").write_text(f"SIGNALGATE_API_KEY={DUMMY_KEY}\n")
    _, boot2 = seam.wait_for_boots(2, READY_WITHIN_S)
    assert wait_for_health(boot2)["key"] == "set"
    # The key reaches the app through app/.env only, never its command line.
    assert "uvicorn" in command_of(boot2["pid"])
    assert DUMMY_KEY not in command_of(boot2["pid"])

    # One save, one reload, and at the opening no line about it.
    terminal.drain_output(RELOAD_DEBOUNCE_S * 4)
    assert len(seam.boots()) == 2
    assert terminal.count(RELOADED) == 0

    terminal.press(CTRL_C)
    assert terminal.wait_exit(EXIT_WITHIN_S) == 0
    assert DUMMY_KEY not in terminal.raw
    assert DUMMY_KEY not in (tracked_copy / ".playground" / "app.log").read_text()
    ended_cleanly(terminal, tracked_copy)


def test_a_broken_save_names_the_line_and_a_fix_recovers(open_terminal, tracked_copy, seam):
    routes = tracked_copy / "app" / "routes.py"
    original = routes.read_text()
    broken_line = len(original.splitlines()) + 1

    terminal = open_terminal(seam.env(tracked_copy))
    terminal.expect(OPENING, READY_WITHIN_S)

    routes.write_text(original + "def broken(:\n")
    # The error's name may sit between the two, when the app's output tells it.
    terminal.expect("didn't start:", READY_WITHIN_S)
    terminal.expect(f"app/routes.py:{broken_line}", REPLY_WITHIN_S)

    terminal.type("/start")
    terminal.expect("The sample app isn't running. Fix the error above", REPLY_WITHIN_S)

    # The save that fixes it is news even at the opening.
    routes.write_text(original)
    terminal.expect(RELOADED, READY_WITHIN_S)

    terminal.type("/start")
    wait_for_run_panel(terminal)
    terminal.press(CTRL_C)
    terminal.expect("Run stopped after", REPLY_WITHIN_S)
    terminal.press(CTRL_C)
    assert terminal.wait_exit(EXIT_WITHIN_S) == 0
    ended_cleanly(terminal, tracked_copy)


def test_saves_during_a_run_wait_for_the_close(open_terminal, tracked_copy, seam):
    routes = tracked_copy / "app" / "routes.py"
    terminal = open_terminal(seam.env(tracked_copy))
    terminal.expect(OPENING, READY_WITHIN_S)
    (boot1,) = seam.wait_for_boots(1, READY_WITHIN_S)

    terminal.type("/start")
    wait_for_run_panel(terminal)
    for number in range(3):
        append_comment(routes, f"save {number}")
        time.sleep(RELOAD_DEBOUNCE_S * 2)  # three separate saves, not one burst
    terminal.expect("change detected, reloading after this run", REPLY_WITHIN_S)

    terminal.expect(CLOSE_QUESTION, RUN_WITHIN_S)
    # The run's tally is out, and the app is still the one the run used;
    # the three saves then restart it once.
    assert seam.pids() == [boot1["pid"]]
    seam.wait_for_boots(2, READY_WITHIN_S)
    terminal.drain_output(RELOAD_DEBOUNCE_S * 4)
    assert len(seam.boots()) == 2
    assert terminal.count("change detected, reloading after this run") == 1
    # The run said the reload would wait; once it is in, one line says so.
    terminal.expect(RELOADED, REPLY_WITHIN_S)
    assert terminal.count(RELOADED) == 1

    terminal.press(CTRL_C)
    assert terminal.wait_exit(EXIT_WITHIN_S) == 0
    ended_cleanly(terminal, tracked_copy)


# -- the app stops on its own ----------------------------------------------------------


def test_an_app_killed_mid_run_invalidates_the_run_and_comes_back(
    open_terminal, tracked_copy, seam
):
    terminal = open_terminal(seam.env(tracked_copy))
    terminal.expect(OPENING, READY_WITHIN_S)
    (boot1,) = seam.wait_for_boots(1, READY_WITHIN_S)

    terminal.type("/start")
    wait_for_run_panel(terminal)
    os.kill(boot1["pid"], signal.SIGKILL)

    terminal.expect("so this run doesn't count", RUN_WITHIN_S)
    terminal.expect("Type /start to run it again.", REPLY_WITHIN_S)
    terminal.expect("so I restarted it.", READY_WITHIN_S)

    terminal.type("/start")
    wait_for_run_panel(terminal)
    terminal.press(CTRL_C)
    terminal.expect("Run stopped after", REPLY_WITHIN_S)
    terminal.press(CTRL_C)
    assert terminal.wait_exit(EXIT_WITHIN_S) == 0
    ended_cleanly(terminal, tracked_copy)


# -- other ways out -------------------------------------------------------------------


def test_sighup_exits_cleanly(open_terminal, tracked_copy, seam):
    terminal = open_terminal(seam.env(tracked_copy))
    terminal.expect(OPENING, READY_WITHIN_S)

    terminal.process.send_signal(signal.SIGHUP)

    assert terminal.wait_exit(EXIT_WITHIN_S) == 0
    ended_cleanly(terminal, tracked_copy)


def test_closing_the_terminal_exits_cleanly(open_terminal, tracked_copy, seam):
    terminal = open_terminal(seam.env(tracked_copy))
    terminal.expect(OPENING, READY_WITHIN_S)
    seam.wait_for_boots(1, READY_WITHIN_S)

    terminal.hang_up()

    assert terminal.wait_exit(EXIT_WITHIN_S) == 0
    ended_cleanly(terminal, tracked_copy)


# -- saved progress ---------------------------------------------------------------------


def test_a_killed_console_leaves_nothing_behind_and_resumes(open_terminal, tracked_copy, seam):
    first = open_terminal(seam.env(tracked_copy))
    first.expect(OPENING, READY_WITHIN_S)
    (boot1,) = seam.wait_for_boots(1, READY_WITHIN_S)
    first.type("/start")
    wait_for_run_panel(first)

    first.process.kill()
    first.process.wait()
    assert wait_pids_gone([boot1["pid"]], ORPHAN_GONE_WITHIN_S) == set()
    assert wait_until_gone(tracked_copy, seam.pids(), EXIT_WITHIN_S) == set()
    lock = filelock.FileLock(str(tracked_copy / ".playground" / "console.lock"), timeout=0)
    lock.acquire()
    lock.release()

    second = open_terminal(seam.env(tracked_copy))
    second.expect(WELCOME_BACK, READY_WITHIN_S)
    second.type("/continue")
    second.expect("Your last run was interrupted, so it doesn't count.", REPLY_WITHIN_S)
    second.expect("Type /start to run it again.", REPLY_WITHIN_S)
    boots = seam.wait_for_boots(2, READY_WITHIN_S)
    assert owned_app_pids(tracked_copy, seam.pids()) == {boots[1]["pid"]}

    second.type("/start")
    second.expect(CLOSE_QUESTION, RUN_WITHIN_S)
    second.type("/quit")
    second.expect(BYE, REPLY_WITHIN_S)
    assert second.wait_exit(EXIT_WITHIN_S) == 0
    assert (tracked_copy / ".playground" / "state.json").exists()
    ended_cleanly(second, tracked_copy)


def test_continue_after_quit_shows_the_same_close(open_terminal, tracked_copy, seam):
    first = open_terminal(seam.env(tracked_copy))
    first.expect(OPENING, READY_WITHIN_S)
    first.type("/start")
    first.expect("20.0% of actions turned into a success.", RUN_WITHIN_S)
    first.expect(CLOSE_QUESTION, REPLY_WITHIN_S)
    first.type("/quit")
    first.expect(SAVED, REPLY_WITHIN_S)
    first.expect(BYE, REPLY_WITHIN_S)
    assert first.wait_exit(EXIT_WITHIN_S) == 0

    second = open_terminal(seam.env(tracked_copy))
    second.expect(WELCOME_BACK, READY_WITHIN_S)
    second.type("/continue")
    second.expect("Picking up where you left off.", REPLY_WITHIN_S)
    second.expect("20.0% of actions turned into a success.", REPLY_WITHIN_S)
    second.expect(CLOSE_QUESTION, REPLY_WITHIN_S)
    second.expect(PROMPT, REPLY_WITHIN_S)

    # Ctrl-D at the empty prompt is /quit: saved (there is something to
    # resume), then bye.
    second.press(CTRL_D)
    second.expect(SAVED, REPLY_WITHIN_S)
    second.expect(BYE, REPLY_WITHIN_S)
    assert second.wait_exit(EXIT_WITHIN_S) == 0
    assert (tracked_copy / ".playground" / "state.json").exists()
    ended_cleanly(first, tracked_copy)
    ended_cleanly(second, tracked_copy)
