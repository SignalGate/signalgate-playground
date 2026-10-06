"""How the terminal tests match screen text: wrap-proof by default, exact on request.

The console wraps its paragraphs to the terminal, so a line of copy can
reach the screen broken where a space was. ``Terminal.expect`` and
``Terminal.count`` compare with every run of whitespace made one space, on
both sides; ``Terminal.expect_exact`` compares the text as it is. No console
is started here: a ``Terminal`` is given its screen text directly.
"""

from __future__ import annotations

import pytest

from tests.e2e.conftest import Terminal, flatten

# Long enough for a failed wait to give up at once.
_GIVE_UP_S = 0.05


def _screen(text: str) -> Terminal:
    terminal = Terminal.__new__(Terminal)
    terminal.fd = None
    terminal.raw = text
    terminal._cursor = 0
    terminal._flat_cursor = 0
    return terminal


def test_expect_matches_across_a_line_break_and_extra_spaces():
    terminal = _screen("a\nb   c")
    terminal.expect("a b c", _GIVE_UP_S)
    assert terminal.count("a b c") == 1
    assert terminal.count("a\n b") == 1


def test_expect_exact_does_not_match_a_wrapped_line():
    with pytest.raises(AssertionError, match="did not see exactly"):
        _screen("a\nb   c").expect_exact("a b c", _GIVE_UP_S)
    _screen("a\nb   c").expect_exact("a\nb", _GIVE_UP_S)


def test_a_wrapped_question_still_matches():
    screen = (
        "20.0% of actions turned into a success. 7 in 10 were the attack. Ready to connect\n"
        "SignalGate? (yes/no)\n"
    )
    terminal = _screen(screen)
    terminal.expect("Ready to connect SignalGate? (yes/no)", _GIVE_UP_S)
    assert "Ready to connect SignalGate? (yes/no)" in terminal.flat
    assert "Ready to connect SignalGate? (yes/no)" not in screen  # the wrap broke it


def test_expect_moves_past_each_match_in_order():
    terminal = _screen("one\ntwo one\nthree")
    terminal.expect("one", _GIVE_UP_S)
    terminal.expect("one", _GIVE_UP_S)
    with pytest.raises(AssertionError, match="did not see"):
        terminal.expect("one", _GIVE_UP_S)
    terminal.expect("three", _GIVE_UP_S)


def test_both_kinds_of_expect_share_one_cursor():
    terminal = _screen("x first\nsecond   x")
    terminal.expect("first second", _GIVE_UP_S)
    with pytest.raises(AssertionError):
        terminal.expect_exact("first", _GIVE_UP_S)  # already behind the cursor
    terminal.expect_exact("x", _GIVE_UP_S)
    with pytest.raises(AssertionError):
        terminal.expect("x", _GIVE_UP_S)


def test_the_screen_text_only_grows_so_the_cursor_stays_valid():
    terminal = _screen("alpha  ")
    terminal.expect("alpha", _GIVE_UP_S)
    terminal.raw += "\n  beta\ngamma"
    terminal.expect("beta gamma", _GIVE_UP_S)
    assert terminal.flat == flatten(terminal.text) == "alpha beta gamma"
