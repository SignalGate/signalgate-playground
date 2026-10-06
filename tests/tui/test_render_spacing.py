"""The blank lines between the console's blocks, and the one "Saved" line.

A recorded console stands in for the terminal. Where the real console hands
the input line to the user, these tests do what the terminal would show:
the input line opens (anything printed meanwhile lands above it), then the
typed answer stays on screen as one line.
"""

from __future__ import annotations

import io
import re

import pytest
from rich.console import Console

import playground.tui.console as console_module
from playground.copy.en import text
from playground.machine.events import AwaitInput, RunProgress, Say, ShowLogo
from playground.tui.console import InteractiveRenderer
from playground.tui.prompt import PROMPT_GLYPH

_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


class _Terminal:
    """A renderer on a recorded console, with the input line played by hand."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, *, width: int = 100) -> None:
        self.buffer = io.StringIO()
        self.console = Console(
            file=self.buffer, width=width, color_system=None, force_terminal=True
        )
        self.renderer = InteractiveRenderer(self.console, "none")
        self._open = False
        monkeypatch.setattr(console_module, "_prompt_open", lambda: self._open)

    def show(self, *events) -> None:
        for event in events:
            self.renderer.show(event)
            if isinstance(event, AwaitInput):
                self._open = True

    def answer(self, line: str) -> None:
        """The user types ``line`` and presses Enter: the input line stays."""
        assert self._open, "no input line is open"
        self._open = False
        self.console.print(PROMPT_GLYPH + line, markup=False, highlight=False)

    @property
    def lines(self) -> list[str]:
        return _ANSI.sub("", self.buffer.getvalue()).split("\n")[:-1]

    def index(self, needle: str) -> int:
        return next(at for at, line in enumerate(self.lines) if needle in line)


def _no_two_blank_lines_in_a_row(lines: list[str]) -> None:
    for at in range(1, len(lines)):
        assert lines[at] or lines[at - 1], f"two blank lines at {at}: {lines}"


def _first_line(key: str, **params) -> str:
    return text(key, **params).split("\n")[0]


def test_a_heading_follows_exactly_one_blank_line(monkeypatch):
    terminal = _Terminal(monkeypatch)
    terminal.show(Say("ch3.lesson"), Say("chapter.heading", {"chapter": 4, "title": "Next"}))
    lines = terminal.lines
    at = terminal.index("Chapter 4 of 6")
    assert lines[at - 1] == "" and lines[at - 2] != ""
    # After an answer too: one blank line, not two.
    terminal.show(Say("ch4.ask"), AwaitInput("continue"))
    terminal.answer("yes")
    terminal.show(Say("chapter.heading", {"chapter": 5, "title": "Later"}))
    lines = terminal.lines
    at = terminal.index("Chapter 5 of 6")
    assert lines[at - 1] == "" and lines[at - 2] == PROMPT_GLYPH + "yes"
    _no_two_blank_lines_in_a_row(lines)


def test_different_blocks_get_one_blank_line_and_the_question_sits_on_the_input_line(
    monkeypatch,
):
    terminal = _Terminal(monkeypatch)
    terminal.show(Say("ch3.intro"), Say("ch3.task"), Say("ch3.lesson"), Say("ch3.ask"))
    terminal.show(AwaitInput("continue"))
    terminal.answer("")
    lines = terminal.lines
    intro, top = terminal.index(text("ch3.intro")), terminal.index("╭")
    bottom, lesson = terminal.index("╰"), terminal.index(_first_line("ch3.lesson")[:40])
    ask = terminal.index(text("ch3.ask"))
    assert top == intro + 2 and lines[intro + 1] == ""
    assert lesson == bottom + 2 and lines[bottom + 1] == ""
    assert lines[ask - 1] == "" and lines[ask - 2] != ""
    # No blank line between the question and the input line.
    assert lines[ask + 1] == PROMPT_GLYPH
    _no_two_blank_lines_in_a_row(lines)


def test_after_an_answer_the_next_output_starts_after_one_blank_line(monkeypatch):
    terminal = _Terminal(monkeypatch)
    terminal.show(Say("ch2.key.ask"), AwaitInput("continue"))
    terminal.answer("yes")
    terminal.show(Say("ch2.key.missing"), Say("ch2.key.ask"), AwaitInput("continue"))
    lines = terminal.lines
    answered = lines.index(PROMPT_GLYPH + "yes")
    assert lines[answered + 1] == ""
    assert lines[answered + 2].startswith(_first_line("ch2.key.missing")[:30])
    _no_two_blank_lines_in_a_row(lines)


def test_a_reply_that_is_not_a_question_gets_a_blank_line_before_the_input_line(monkeypatch):
    terminal = _Terminal(monkeypatch)
    terminal.show(Say("opening"), AwaitInput("start"))
    terminal.answer("/help")
    terminal.show(Say("help"), AwaitInput("start"))
    terminal.answer("/status")
    lines = terminal.lines
    last_help_line = text("help").split("\n")[-1]
    at = lines.index(last_help_line)
    assert lines[at + 1] == "" and lines[at + 2] == PROMPT_GLYPH + "/status"
    helped = lines.index(PROMPT_GLYPH + "/help")
    assert lines[helped + 1] == "" and lines[helped + 2] == text("help").split("\n")[0]
    _no_two_blank_lines_in_a_row(lines)


def test_tasks_cards_and_achievements_stand_apart_from_their_own_kind(monkeypatch):
    terminal = _Terminal(monkeypatch)
    terminal.show(
        Say("achievement.ch1"),
        Say("achievement.connected"),
        Say("complete.card", {"minutes": 25}),
        Say("complete.card.untimed"),
    )
    lines = terminal.lines
    first, second = (
        terminal.index(text("achievement.ch1")),
        terminal.index(text("achievement.connected")),
    )
    assert second == first + 2 and lines[first + 1] == ""
    tops = [at for at, line in enumerate(lines) if line.startswith("╭")]
    assert len(tops) == 2 and lines[tops[1] - 1] == "" and lines[tops[1] - 2].startswith("╰")
    _no_two_blank_lines_in_a_row(lines)


def test_one_blank_line_before_the_live_panel_and_one_after_it(monkeypatch):
    terminal = _Terminal(monkeypatch)
    terminal.show(Say("ch1.intro", {"total": 10}))
    terminal.renderer.run_started(10)
    terminal.renderer.progress(RunProgress(10, 10, 10, 2))
    terminal.renderer.run_ended()
    terminal.show(Say("ch1.close", {"conversion": 0.2, "attack_in_ten": 7}))
    lines = terminal.lines
    intro = terminal.index(_first_line("ch1.intro", total=10)[:40])
    top = next(at for at, line in enumerate(lines) if line.startswith("╭"))
    bottom = max(at for at, line in enumerate(lines) if line.startswith("╰"))
    close = terminal.index(_first_line("ch1.close", conversion=0.2, attack_in_ten=7)[:30])
    assert lines[top - 1] == "" and top - 2 >= intro
    assert lines[bottom + 1] == "" and close == bottom + 2


def test_off_a_terminal_the_panel_still_ends_its_line_and_stands_apart():
    buffer = io.StringIO()
    renderer = InteractiveRenderer(Console(file=buffer, width=60, color_system=None), "none")
    renderer.run_started(10)
    renderer.progress(RunProgress(10, 10, 10, 2))
    renderer.run_ended()
    renderer.show(Say("run.again"))
    lines = buffer.getvalue().split("\n")
    bottom = max(at for at, line in enumerate(lines) if line.startswith("╰"))
    assert lines[bottom].endswith("╯")
    assert lines[bottom + 1] == "" and lines[bottom + 2] == text("run.again")


def test_a_notice_while_the_input_line_is_open_still_leaves_a_blank_line_after_the_answer(
    monkeypatch,
):
    terminal = _Terminal(monkeypatch)
    terminal.show(Say("ch3.ask"), AwaitInput("continue"))
    # Printed above the open input line: after the question, one blank line.
    terminal.show(Say("app.reloaded"))
    terminal.answer("no")
    terminal.show(Say("reply.no"), AwaitInput("continue"))
    lines = terminal.lines
    saved = lines.index(text("app.reloaded"))
    assert lines[saved - 1] == "" and lines[saved - 2] == text("ch3.ask")
    answered = lines.index(PROMPT_GLYPH + "no")
    assert lines[answered + 1] == "" and lines[answered + 2] == text("reply.no")
    _no_two_blank_lines_in_a_row(lines)


def test_the_logo_stands_apart_from_what_follows(monkeypatch):
    terminal = _Terminal(monkeypatch)
    terminal.show(ShowLogo(), Say("app.starting"))
    lines = terminal.lines
    at = terminal.index(text("app.starting"))
    assert lines[at - 1] == "" and lines[at - 2] != ""


def test_the_lines_of_a_failed_start_keep_the_code_and_its_marker_together(monkeypatch):
    terminal = _Terminal(monkeypatch, width=40)
    source = "        ev = sg_event(request, body.user, body.signalgate_log, 'playground.action')"
    terminal.show(
        Say("app.down.error", {"error": "IndentationError", "location": "app/routes.py:57"}),
        Say("app.down.code", {"code": source}),
        Say("app.down.code", {"code": "        ^"}),
        Say("app.down.hint.indent"),
        Say("app.down.fix"),
    )
    lines = terminal.lines
    at = lines.index(source)  # longer than the terminal, never wrapped
    assert lines[at + 1] == "        ^"
    assert lines[at - 1] == "" and lines[at + 2] == ""
    _no_two_blank_lines_in_a_row(lines)


# -- several saves, one "Saved" line --------------------------------------------------


def test_three_saves_in_a_row_print_one_saved_line(monkeypatch):
    terminal = _Terminal(monkeypatch)
    terminal.show(Say("ch3.ask"), AwaitInput("continue"))
    terminal.show(Say("app.reloaded"), Say("app.reloaded"))
    terminal.show(AwaitInput("continue"))  # even with the input line handed over again
    terminal.show(Say("app.reloaded"))
    assert terminal.lines.count(text("app.reloaded")) == 1


def test_a_line_between_two_saves_shows_both(monkeypatch):
    terminal = _Terminal(monkeypatch)
    terminal.show(Say("ch3.ask"), AwaitInput("continue"), Say("app.reloaded"))
    terminal.answer("no")
    terminal.show(Say("reply.no"), AwaitInput("continue"), Say("app.reloaded"))
    terminal.show(Say("app.reloaded"))
    assert terminal.lines.count(text("app.reloaded")) == 2
