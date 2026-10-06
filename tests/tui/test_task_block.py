"""The "Your turn" block prints its code exactly as it goes into the file.

Someone copies these lines straight from the terminal into ``app/routes.py``,
so each code line must reach the screen whole, byte for byte, starting at
column 0 with the file's own indentation: no frame, gutter or display indent
on it, never wrapped, not even on a terminal narrower than the line. The
frame is drawn only on the line above the block and the line below it.
"""

from __future__ import annotations

import io
import re

import pytest
from rich.console import Console

from playground.copy.en import TASKS, TEXT, task_plain, text
from playground.machine.events import Say
from playground.tui.console import InteractiveRenderer
from playground.tui.render import render_task

_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
_FRAME_CHARACTERS = "╭╮╰╯─│+|-"

# (level, Rich colour system, ASCII mode, terminal width)
_MODES = [
    ("none", None, False, 80),
    ("none", None, False, 120),
    ("truecolor", "truecolor", False, 80),
    ("256", "256", False, 80),
    ("16", "standard", False, 80),
    ("none", None, True, 80),
]


def _render(task_key: str, level: str, colour_system, ascii: bool, width: int) -> str:
    buffer = io.StringIO()
    console = Console(file=buffer, width=width, color_system=colour_system, force_terminal=True)
    InteractiveRenderer(console, level, ascii=ascii).show(Say(task_key))
    return _ANSI.sub("", buffer.getvalue())


def _snippets(task_key: str) -> list[list[str]]:
    _title, edits, _footer = TASKS[task_key]
    return [TEXT[snippet].split("\n") for _location, snippet in edits]


@pytest.mark.parametrize("task_key", sorted(TASKS))
@pytest.mark.parametrize(("level", "colour_system", "ascii", "width"), _MODES)
def test_every_snippet_line_is_printed_whole_and_in_order(
    task_key, level, colour_system, ascii, width
):
    lines = _render(task_key, level, colour_system, ascii, width).split("\n")
    at = 0
    for snippet in _snippets(task_key):
        # The snippet's lines, one after the other, each a whole output line.
        while lines[at : at + len(snippet)] != snippet:
            at += 1
            assert at < len(lines), f"snippet not printed whole:\n{snippet}"
        # Exactly one blank line before it and one after it.
        assert lines[at - 1] == "" and lines[at - 2] != ""
        assert lines[at + len(snippet)] == "" and lines[at + len(snippet) + 1] != ""
        at += len(snippet)


@pytest.mark.parametrize("task_key", sorted(TASKS))
@pytest.mark.parametrize(("level", "colour_system", "ascii", "width"), _MODES)
def test_the_frame_is_only_above_and_below_the_block(task_key, level, colour_system, ascii, width):
    block = _render(task_key, level, colour_system, ascii, width).split("\n")[:-1]
    top, bottom = ("+", "+") if ascii else ("╭", "╰")
    assert block[0].startswith(top) and block[-1].startswith(bottom)
    inside = block[1:-1]
    if not ascii:
        assert not any("╭" in line or "╰" in line for line in inside)
    code = {line for snippet in _snippets(task_key) for line in snippet}
    for line in inside:
        if line in code:
            assert not line.startswith(tuple(_FRAME_CHARACTERS)), line


@pytest.mark.parametrize("task_key", sorted(TASKS))
def test_inside_the_frame_is_the_plain_task(task_key):
    """Wide enough for every line, the block is the plain task (the text
    ``--ci`` shows) with the frame drawn around it."""
    block = _render(task_key, "truecolor", "truecolor", False, 120).split("\n")[:-1]
    plain = task_plain(task_key).split("\n")
    assert block[1:-1] == plain[1:-1]
    assert plain[0] in block[0] and plain[-1] in block[-1]


def test_code_longer_than_the_terminal_is_never_wrapped():
    gate = TEXT["snippet.ch5.gate"].split("\n")
    long_lines = [line for line in gate if len(line) > 80]
    assert long_lines, "the chapter 5 gate has lines longer than 80 columns"
    for level, colour_system in (("none", None), ("truecolor", "truecolor")):
        lines = _render("ch5.task", level, colour_system, False, 80).split("\n")
        for line in long_lines:
            assert line in lines


def test_code_lines_start_at_column_0_with_the_files_own_indentation():
    lines = _render("ch3.task", "none", None, False, 100).split("\n")
    assert text("snippet.ch3.import") in lines  # a top-level line: no indent at all
    action = TEXT["snippet.ch3.action_log"].split("\n")
    assert action[0].startswith("    ev = ") and action[0] in lines
    assert "        sg().log(ev)" in lines


def test_code_is_syntax_coloured_at_a_colour_level_without_changing_a_character():
    buffer = io.StringIO()
    console = Console(file=buffer, width=100, color_system="truecolor", force_terminal=True)
    InteractiveRenderer(console, "truecolor").show(Say("ch5.task"))
    raw = buffer.getvalue()
    assert "\x1b[" in raw
    stripped = _ANSI.sub("", raw).split("\n")
    for line in TEXT["snippet.ch5.gate"].split("\n"):
        assert line in stripped


def test_the_block_is_one_text_per_line_with_code_marked_as_never_wrapping():
    rendered = render_task("ch3.task", level="none", ascii=False, width=100)
    code = [line for line in rendered if line.overflow == "ignore"]
    assert [line.plain for line in code] == [
        row for snippet in _snippets("ch3.task") for row in snippet
    ]
    assert all(line.no_wrap for line in code)
