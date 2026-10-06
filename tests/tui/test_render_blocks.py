"""How each kind of block looks: headings, bold words, results, notes,
achievements, the completion card and paragraph width, at every colour
level and in ASCII mode."""

from __future__ import annotations

import io
import re

import pytest
from rich.console import Console
from rich.text import Text

from playground.copy.en import TEXT, text
from playground.machine.events import Say
from playground.tui.colour import style
from playground.tui.console import InteractiveRenderer
from playground.tui.render import (
    CHAPTERS,
    PARAGRAPH_COLUMNS,
    render_card,
    render_heading,
    render_say,
)
from tests.tui.fixtures import FIXTURE_EVENTS, ONLINE_FIXTURE_EVENTS

_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
_BOLD_ON = "\x1b[1m"

_LEVELS = [("truecolor", "truecolor"), ("256", "256"), ("16", "standard"), ("none", None)]


def _render(events, level="truecolor", colour_system="truecolor", *, ascii=False, width=100):
    buffer = io.StringIO()
    console = Console(file=buffer, width=width, color_system=colour_system, force_terminal=True)
    renderer = InteractiveRenderer(console, level, ascii=ascii)
    for event in events:
        renderer.show(event)
    return buffer.getvalue()


def _every_say() -> list:
    headings = [
        Say("chapter.heading", {"chapter": n, "title": text(f"chapter.{n}")})
        for n in range(1, CHAPTERS + 1)
    ]
    tasks = [Say("ch3.task"), Say("ch5.task")]
    return [*FIXTURE_EVENTS, *ONLINE_FIXTURE_EVENTS, *headings, *tasks]


def _sgr(name: str, level: str = "truecolor") -> str:
    """The escape code a palette colour opens with at ``level``."""
    buffer = io.StringIO()
    console = Console(file=buffer, color_system="truecolor", force_terminal=True)
    console.print(Text("x", style=style(name, level)), end="")
    return buffer.getvalue().split("x")[0]


# -- bold words --------------------------------------------------------------------------


def test_a_bold_span_is_bold_at_a_colour_level_and_its_markers_never_show():
    output = _render([Say("ch3.ask")])
    assert _BOLD_ON in output
    bold_part = output.split(_BOLD_ON, 1)[1].split("\x1b[0m", 1)[0]
    assert bold_part == "Press Enter"
    assert "**" not in output


@pytest.mark.parametrize(("level", "colour_system"), _LEVELS)
def test_no_copy_ever_shows_its_bold_markers(level, colour_system):
    for ascii in (False, True):
        output = _render(_every_say(), level, colour_system, ascii=ascii)
        assert "**" not in output


def test_level_none_has_no_escapes_at_all():
    output = _render(_every_say(), "none", None)
    assert "\x1b[" not in output


def test_ascii_mode_is_pure_ascii_and_shows_the_chapter_as_a_bar():
    output = _render(_every_say(), "none", None, ascii=True)
    assert output.isascii()
    assert "[###---]" in output


def test_bold_words_in_a_param_are_shown_as_written():
    output = _render([Say("verify.line", {"line": "2**8 is **not** bold"})], "none", None)
    assert "2**8 is **not** bold" in output


# -- headings ---------------------------------------------------------------------------


def test_a_heading_is_one_rule_with_the_chapter_title_and_progress_dots():
    line = render_heading(3, text("chapter.3"), level="none", ascii=False, width=150)
    assert line.plain.startswith("━━ Chapter 3 of 6 · Teach SignalGate ●●●○○○ ━")
    assert len(line.plain) == PARAGRAPH_COLUMNS
    narrow = render_heading(3, text("chapter.3"), level="none", ascii=False, width=70)
    assert len(narrow.plain) == 70


def test_a_heading_in_ascii_mode():
    line = render_heading(3, text("chapter.3"), level="none", ascii=True, width=100)
    assert line.plain.startswith("== Chapter 3 of 6 - Teach SignalGate [###---] =")
    assert line.plain.isascii()


def test_a_heading_is_styled_accent_rule_bold_title_and_dim_dots_to_come():
    line = render_heading(2, text("chapter.2"), level="truecolor", ascii=False, width=100)
    styles = {line.plain[span.start : span.end]: str(span.style) for span in line.spans}
    title = text("chapter.heading", chapter=2, title=text("chapter.2"))
    assert styles[title] == "bold"
    assert styles["━━"] == style("accent", "truecolor")
    assert styles["●●"] == style("accent", "truecolor")
    assert styles["○○○○"] == style("dim", "truecolor")


def test_the_heading_say_draws_the_rule():
    output = _ANSI.sub("", _render([Say("chapter.heading", {"chapter": 1, "title": "Meet"})]))
    assert "━━ Chapter 1 of 6 · Meet ●○○○○○ ━" in output


# -- results, notes and achievements ---------------------------------------------------


@pytest.mark.parametrize(
    ("mark", "colour"), [("✓", "good"), ("✗", "bad"), ("!", "warn"), ("◌", "dim")]
)
def test_a_results_leading_mark_is_coloured_and_only_the_mark(mark, colour):
    line = render_say(
        Say("verify.line", {"line": f"  {mark} a finding"}), level="truecolor", ascii=False
    )
    assert [(span.start, span.end, str(span.style)) for span in line.spans] == [
        (2, 3, style(colour, "truecolor"))
    ]
    output = _render([Say("verify.line", {"line": f"{mark} done"})])
    assert _sgr(colour) + mark in output


def test_a_result_without_a_leading_mark_is_plain():
    line = render_say(Say("drain.skipped", {"remaining": 3}), level="truecolor", ascii=False)
    assert line.spans == [] and not line.style


def test_the_why_and_fix_labels_of_a_finding_are_bold():
    for key, name in (("verify.why", "why"), ("verify.fix", "fix")):
        finding = text(key, **{name: "because it matters"})
        line = render_say(Say("verify.line", {"line": finding}), level="truecolor", ascii=False)
        label = finding.split(":")[0].strip() + ":"
        assert [line.plain[span.start : span.end] for span in line.spans] == [label]
        assert all(str(span.style) == "bold" for span in line.spans)
    output = _render([Say("verify.line", {"line": text("verify.why", why="x")})])
    assert _BOLD_ON + "Why:" in output


def test_marks_are_coloured_in_ascii_mode_too():
    line = render_say(Say("verify.line", {"line": "✓ done"}), level="truecolor", ascii=True)
    assert line.plain == "v done"
    assert [(span.start, span.end) for span in line.spans] == [(0, 1)]


def test_a_note_is_dim_and_an_achievement_is_good_and_bold():
    note = render_say(Say("ch3.lesson"), level="truecolor", ascii=False)
    assert str(note.style) == style("dim", "truecolor")
    achievement = render_say(Say("achievement.ch3"), level="truecolor", ascii=False)
    assert str(achievement.style) == style("good", "truecolor", bold=True)
    for level in ("256", "16"):
        assert render_say(Say("ch3.lesson"), level=level, ascii=False).style


def test_nothing_is_styled_at_level_none():
    for say in (Say("ch3.lesson"), Say("achievement.ch1"), Say("ch3.ask")):
        line = render_say(say, level="none", ascii=False)
        assert not line.style and line.spans == []


# -- the completion card ----------------------------------------------------------------


def test_the_card_is_framed_with_its_first_line_as_a_bold_title():
    lines = render_card(
        Say("complete.card", {"minutes": 25}), level="truecolor", ascii=False, width=100
    )
    body = text("complete.card", minutes=25).split("\n")
    assert lines[0].plain.startswith("╭ " + body[0] + " ─") and lines[0].plain.endswith("╮")
    assert [line.plain for line in lines[1:-1]] == body[1:]
    assert lines[-1].plain.startswith("╰─") and lines[-1].plain.endswith("╯")
    assert len(lines[0].plain) == len(lines[-1].plain) == PARAGRAPH_COLUMNS
    title = [span for span in lines[0].spans if lines[0].plain[span.start : span.end] == body[0]]
    assert title and str(title[0].style) == "bold"


def test_the_card_in_ascii_mode_and_without_a_time():
    lines = render_card(Say("complete.card.untimed"), level="none", ascii=True, width=60)
    assert all(line.plain.isascii() for line in lines)
    assert lines[0].plain.startswith("+ ") and lines[-1].plain.startswith("+-")
    assert len(lines) == len(TEXT["complete.card.untimed"].split("\n")) + 1


# -- width -------------------------------------------------------------------------------


def test_a_long_paragraph_wraps_at_100_columns_on_a_wide_terminal():
    say = Say("ch1.intro", {"total": 10_000})
    assert len(text(say.key, **say.params)) > 130
    output = _ANSI.sub("", _render([say], "none", None, width=150))
    lines = [line for line in output.split("\n") if line]
    assert len(lines) > 1
    assert all(len(line) <= PARAGRAPH_COLUMNS for line in lines)


def test_a_narrow_terminal_wraps_at_its_own_width():
    output = _ANSI.sub("", _render([Say("ch1.intro", {"total": 10_000})], "none", None, width=50))
    assert all(len(line) <= 50 for line in output.split("\n"))
