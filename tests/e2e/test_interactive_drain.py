"""The wait for the sample app's queued log events, on a real terminal.

After a run, and before the app restarts or the console exits, the console
waits for the app's SDK to send the log events it still holds. The start-up
hook holds those deliveries (``gated_seam``), so the wait is there to see:
its line says what Ctrl-C will do, and Ctrl-C does it, skipping the wait
after a run, or quitting at once when the wait belongs to a reload or to
``/quit``. Once the deliveries go through, the wait ends with one line
saying every queued log was sent.

The text looked for is only what the waiting line says in every form it
takes: a line printed once, or one redrawn in place as the count goes up.
"""

from __future__ import annotations

import sys
import time

import pytest

from playground.copy.en import text
from tests.e2e.conftest import (
    BYE,
    CLOSE_QUESTION,
    CTRL_C,
    CTRL_D,
    EXIT_WITHIN_S,
    OPENING,
    PROMPT,
    READY_WITHIN_S,
    REPLY_WITHIN_S,
    RUN_WITHIN_S,
    SAVED,
    append_comment,
    ended_cleanly,
    use_logging_app,
)

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(sys.platform == "win32", reason="needs a POSIX pseudo-terminal"),
]

WAITING = "queued log"
SKIP_HINT = "Ctrl-C to skip"
QUIT_HINT = "Ctrl-C to quit now"


def test_drain_lines_say_what_ctrl_c_will_do(open_terminal, tracked_copy, gated_seam):
    use_logging_app(tracked_copy)
    terminal = open_terminal(gated_seam.env(tracked_copy))
    terminal.expect(OPENING, READY_WITHIN_S)

    terminal.type("/start")
    # After the run, its logs are still held: the first Ctrl-C skips the wait.
    terminal.expect(WAITING, RUN_WITHIN_S)
    terminal.expect(SKIP_HINT, REPLY_WITHIN_S)
    terminal.press(CTRL_C)
    terminal.expect("Skipped the wait;", REPLY_WITHIN_S)
    terminal.expect(CLOSE_QUESTION, REPLY_WITHIN_S)

    # At the prompt, a save means a reload, and its wait can only be quit.
    append_comment(tracked_copy / "app" / "routes.py")
    terminal.expect(WAITING, READY_WITHIN_S)
    terminal.expect(QUIT_HINT, REPLY_WITHIN_S)
    terminal.press(CTRL_C)
    assert terminal.wait_exit(EXIT_WITHIN_S) == 0
    # The app never came back up: no reload was announced.
    assert terminal.count(text("app.reloaded")) == 0
    assert terminal.count(text("drain.done")) == 0
    ended_cleanly(terminal, tracked_copy)


def test_the_wait_on_quit_can_be_cut_short(open_terminal, tracked_copy, gated_seam):
    use_logging_app(tracked_copy)
    terminal = open_terminal(gated_seam.env(tracked_copy))
    terminal.expect(OPENING, READY_WITHIN_S)

    terminal.type("/start")
    terminal.expect(SKIP_HINT, RUN_WITHIN_S)
    terminal.press(CTRL_C)
    terminal.expect(CLOSE_QUESTION, REPLY_WITHIN_S)

    terminal.type("/quit")
    terminal.expect(SAVED, REPLY_WITHIN_S)
    terminal.expect(BYE, REPLY_WITHIN_S)
    terminal.expect(QUIT_HINT, REPLY_WITHIN_S)
    terminal.press(CTRL_C)
    started = time.monotonic()
    assert terminal.wait_exit(EXIT_WITHIN_S) == 0
    assert time.monotonic() - started <= EXIT_WITHIN_S
    ended_cleanly(terminal, tracked_copy)


def test_a_wait_that_empties_ends_with_all_logs_sent(open_terminal, tracked_copy, gated_seam):
    use_logging_app(tracked_copy)
    terminal = open_terminal(gated_seam.env(tracked_copy))
    terminal.expect(OPENING, READY_WITHIN_S)

    terminal.type("/start")
    terminal.expect(WAITING, RUN_WITHIN_S)
    terminal.expect(SKIP_HINT, REPLY_WITHIN_S)
    # Let the held deliveries through (well inside the wait's own stall bound).
    gated_seam.release()
    terminal.expect(text("drain.done"), RUN_WITHIN_S)
    terminal.expect(CLOSE_QUESTION, REPLY_WITHIN_S)
    assert terminal.count(text("drain.done")) == 1
    assert "Skipped the wait;" not in terminal.flat

    # Nothing is left to send, so Ctrl-D at the prompt quits without a wait.
    terminal.expect(PROMPT, REPLY_WITHIN_S)
    terminal.press(CTRL_D)
    terminal.expect(BYE, REPLY_WITHIN_S)
    assert terminal.wait_exit(EXIT_WITHIN_S) == 0
    assert terminal.count(text("drain.done")) == 1
    ended_cleanly(terminal, tracked_copy)
