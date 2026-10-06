"""Bold marks in the copy: ``**words**`` in a template, never in a value.

``text()`` is plain text without the marks (what ``--ci``, a no-colour
terminal and ASCII mode show); ``segments()`` hands a renderer the same text
in pieces, each flagged bold or not.
"""

from __future__ import annotations

import re
import string

import pytest

from playground.copy.en import SNIPPET_PREFIX, TASKS, TEXT, segments, text
from tests.tui.fixtures import FIXTURE_EVENTS, ONLINE_FIXTURE_EVENTS

_WELL_FORMED = re.compile(r"\*\*(?=\S)(.+?)(?<=\S)\*\*")

_TEMPLATE_KEYS = [key for key in TEXT if not key.startswith(SNIPPET_PREFIX)]
_SNIPPET_KEYS = [key for key in TEXT if key.startswith(SNIPPET_PREFIX)]


def _fields(piece: str) -> list[str]:
    return [name for _, name, _, _ in string.Formatter().parse(piece) if name is not None]


@pytest.mark.parametrize("key", _TEMPLATE_KEYS)
def test_every_mark_is_well_formed_within_one_line(key):
    for line in TEXT[key].split("\n"):
        assert "**" not in _WELL_FORMED.sub("", line), (key, line)


@pytest.mark.parametrize("key", _TEMPLATE_KEYS)
def test_no_format_field_straddles_a_mark(key):
    pieces = [piece for piece, _bold in segments(key)]
    assert "".join(pieces) == text(key)
    assert sorted(name for piece in pieces for name in _fields(piece)) == sorted(
        _fields(_WELL_FORMED.sub(r"\1", TEXT[key]))
    )


def test_text_never_returns_a_mark():
    for key in [*_TEMPLATE_KEYS, *TASKS]:
        assert "**" not in text(key), key
    for event in (*FIXTURE_EVENTS, *ONLINE_FIXTURE_EVENTS):
        key = getattr(event, "key", None)
        if key is None:
            continue
        assert "**" not in text(key, **dict(event.params)), key


def test_the_pieces_join_to_exactly_the_text():
    for event in (*FIXTURE_EVENTS, *ONLINE_FIXTURE_EVENTS):
        key = getattr(event, "key", None)
        if key is None:
            continue
        params = dict(event.params)
        assert "".join(piece for piece, _ in segments(key, **params)) == text(key, **params)
        assert all(piece for piece, _ in segments(key, **params)), key


def test_a_location_line_has_one_bold_span():
    spans = segments("ch3.edit.2")
    assert [piece for piece, bold in spans if bold] == [
        "2. In action(), right below perform_action(body.user):"
    ]
    assert text("ch3.edit.2") == "2. In action(), right below perform_action(body.user):"


def test_a_question_is_bold_and_its_choices_are_not():
    spans = segments("ch1.close", conversion=0.2, attack_in_ten=7)
    assert ("Ready to connect SignalGate?", True) in spans
    assert spans[-1] == (" (yes/no)", False)
    assert text("ch1.close", conversion=0.2, attack_in_ten=7) == (
        "20.0% of actions turned into a success. 7 in 10 were the attack. "
        "Ready to connect SignalGate? (yes/no)"
    )


def test_a_bold_span_is_formatted_like_the_rest():
    spans = segments("ch3.topup", shortfall=1_200)
    assert ("Send 1,200 more visitors?", True) in spans


def test_marks_in_a_value_are_shown_as_they_are():
    assert text("verify.line", line="2**8 and **this**") == "2**8 and **this**"
    assert segments("verify.line", line="2**8") == [("2**8", False)]
    assert text("ch2.git.key_in_file", file="**x**.py").startswith(
        "✗ Your API key is written in **x**.py"
    )


@pytest.mark.parametrize("key", _SNIPPET_KEYS)
def test_snippets_are_returned_exactly_as_written(key):
    assert text(key) == TEXT[key]
    assert text(key, unused="value") == TEXT[key]
    assert segments(key) == [(TEXT[key], False)]


def test_a_template_without_params_keeps_its_braces_as_written():
    # As before: a template is only formatted when params are given.
    assert text("drain.count") == "{done:,} / {total:,}"
    assert text("drain.count", done=1_478, total=2_400) == "1,478 / 2,400"


def test_an_unknown_key_still_fails_loudly():
    with pytest.raises(KeyError):
        text("no.such.key")
    with pytest.raises(KeyError):
        segments("no.such.key")
