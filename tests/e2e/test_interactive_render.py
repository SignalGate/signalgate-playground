"""How the interactive console lays out what it prints, on a real terminal.

The console runs in a pseudo-terminal, typed at the way a person would. These
tests check the layout the renderer promises: a chapter heading drawn as a
rule with the chapter's progress dots, the question-mark input line at a
yes/no question, a blank line between a reply and the input line, and that
Ctrl-C and Ctrl-D still end the session cleanly (exit code 0, no traceback,
no sample app left running).
"""

from __future__ import annotations

import sys

import pytest

from playground.copy.en import text
from playground.tui.prompt import PROMPT_GLYPH, PROMPT_GLYPH_ASCII, PROMPT_GLYPH_CONFIRM
from tests.e2e import conftest
from tests.e2e.conftest import (
    BYE,
    CLOSE_QUESTION,
    CTRL_C,
    CTRL_D,
    EXIT_WITHIN_S,
    HELP_LAST_COMMAND,
    OPENING,
    PROMPT,
    READY_WITHIN_S,
    REPLY_WITHIN_S,
    RUN_WITHIN_S,
    ended_cleanly,
)

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(sys.platform == "win32", reason="needs a POSIX pseudo-terminal"),
]

CHAPTER_1 = text("chapter.heading", chapter=1, title=text("chapter.1"))
CHAPTER_1_DOTS = "●○○○○○"
HELP_END = text("help").splitlines()[-1]


def _lines_after(screen: str, marker: str) -> list[str]:
    """The screen's lines from the last one holding ``marker`` on."""
    lines = screen.split("\n")
    at = max(index for index, line in enumerate(lines) if marker in line)
    return lines[at:]


def test_the_heading_and_the_yes_no_glyph_then_ctrl_d_says_bye(open_terminal, tracked_copy, seam):
    terminal = open_terminal(seam.env(tracked_copy))
    terminal.expect(OPENING, READY_WITHIN_S)
    terminal.expect(PROMPT, REPLY_WITHIN_S)

    terminal.type("/start")
    terminal.expect(CHAPTER_1, REPLY_WITHIN_S)
    heading = next(line for line in terminal.text.split("\n") if CHAPTER_1 in line)
    # One rule: the chapter and its title, then where it sits among the six.
    assert heading.startswith("━━ " + CHAPTER_1 + " " + CHAPTER_1_DOTS + " ━"), heading
    assert "\n\n" + heading in terminal.text  # a blank line before it

    terminal.expect(CLOSE_QUESTION, RUN_WITHIN_S)
    terminal.expect(PROMPT_GLYPH_CONFIRM.strip(), REPLY_WITHIN_S)
    after = "\n".join(_lines_after(terminal.text, "(yes/no)"))
    # The question sits right above its input line, which has the question mark.
    assert after.split("\n")[1].startswith(PROMPT_GLYPH_CONFIRM.strip()), after

    terminal.press(CTRL_D)
    terminal.expect(BYE, REPLY_WITHIN_S)
    assert terminal.wait_exit(EXIT_WITHIN_S) == 0
    ended_cleanly(terminal, tracked_copy)


def test_a_reply_stands_apart_from_the_input_line_and_ctrl_c_exits(
    open_terminal, tracked_copy, seam
):
    terminal = open_terminal(seam.env(tracked_copy))
    terminal.expect(OPENING, READY_WITHIN_S)
    terminal.expect(PROMPT, REPLY_WITHIN_S)

    terminal.type("/help")
    terminal.expect(HELP_LAST_COMMAND, REPLY_WITHIN_S)
    terminal.expect(HELP_END, REPLY_WITHIN_S)
    terminal.expect(PROMPT, REPLY_WITHIN_S)
    lines = _lines_after(terminal.text, HELP_END)
    # The reply ends, one blank line, then the input line.
    assert lines[1] == "", lines
    assert lines[2].startswith(PROMPT_GLYPH.strip()), lines
    # The answer typed at the opening is followed by one blank line too.
    typed = _lines_after(terminal.text, PROMPT_GLYPH + "/help")
    assert typed[1] == "" and typed[2] != "", typed

    terminal.press(CTRL_C)
    assert terminal.wait_exit(EXIT_WITHIN_S) == 0
    ended_cleanly(terminal, tracked_copy)


# The console on a terminal that takes ASCII only: its output stream raises
# on any other character, as it does where the terminal isn't UTF-8.
_ASCII_ONLY_CHILD = conftest._CHILD.replace(
    "fcntl.ioctl(0, termios.TIOCSCTTY, 0)\n",
    "fcntl.ioctl(0, termios.TIOCSCTTY, 0)\n"
    "sys.stdout.reconfigure(encoding='ascii', errors='strict')\n",
)


def test_a_terminal_without_utf8_is_shown_a_failed_start_it_can_take(
    open_terminal, tracked_copy, seam, monkeypatch
):
    assert _ASCII_ONLY_CHILD != conftest._CHILD
    monkeypatch.setattr(conftest, "_CHILD", _ASCII_ONLY_CHILD)
    routes = tracked_copy / "app" / "routes.py"
    original = routes.read_text()
    # The app can't start: a line the terminal can't show as written, and an
    # error message that runs over two lines (the second looks like an error
    # line too).
    routes.write_text(original + 'raise ValueError("bad settings\\nnote: naïve")\n')
    line = len(original.splitlines()) + 1

    terminal = open_terminal(seam.env(tracked_copy))
    where = f"app/routes.py:{line}"
    terminal.expect(text("app.down.error", error="ValueError", location=where), READY_WITHIN_S)
    terminal.expect('raise ValueError("bad settings\\nnote: na?ve")', REPLY_WITHIN_S)
    terminal.expect(OPENING, REPLY_WITHIN_S)
    terminal.expect(PROMPT_GLYPH_ASCII.strip(), REPLY_WITHIN_S)

    terminal.press(CTRL_D)
    terminal.expect(BYE, REPLY_WITHIN_S)
    assert terminal.wait_exit(EXIT_WITHIN_S) == 0
    assert terminal.raw.isascii()
    ended_cleanly(terminal, tracked_copy)
