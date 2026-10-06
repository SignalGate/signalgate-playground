"""Colour-level detection, ASCII detection and palette resolution."""

from __future__ import annotations

from playground.tui.colour import detect_ascii, detect_level, style, to_ascii


def test_no_color_wins_over_everything():
    env = {"NO_COLOR": "", "COLORTERM": "truecolor", "TERM": "xterm-256color"}
    assert detect_level(env=env) == "none"


def test_plain_flag_forces_none():
    assert detect_level(plain=True, env={"COLORTERM": "truecolor"}) == "none"


def test_colorterm_truecolor_is_truecolor():
    assert detect_level(env={"COLORTERM": "truecolor"}) == "truecolor"


def test_colorterm_24bit_is_truecolor():
    assert detect_level(env={"COLORTERM": "24bit"}) == "truecolor"


def test_wt_session_is_truecolor():
    assert detect_level(env={"WT_SESSION": "anything"}) == "truecolor"


def test_256color_term_without_colorterm_is_256():
    assert detect_level(env={"TERM": "xterm-256color"}) == "256"


def test_plain_term_falls_back_to_16():
    assert detect_level(env={"TERM": "xterm"}) == "16"


def test_no_env_at_all_falls_back_to_16():
    assert detect_level(env={}) == "16"


def test_ascii_true_for_non_utf8_encoding():
    assert detect_ascii(encoding="ascii") is True
    assert detect_ascii(encoding="cp1252") is True


def test_ascii_false_for_utf8_encoding():
    assert detect_ascii(encoding="utf-8") is False
    assert detect_ascii(encoding="UTF8") is False


def test_ascii_true_when_encoding_unknown():
    assert detect_ascii(encoding="") is True


def test_style_is_empty_at_none_level():
    assert style("accent", "none") == ""
    assert style("accent", "none", bold=True) == ""


def test_style_uses_hex_at_truecolor():
    assert style("accent", "truecolor") == "#635bff"


def test_style_uses_pinned_256_index_for_accent():
    assert style("accent", "256") == "color(99)"


def test_style_bold_is_appended():
    assert style("accent", "truecolor", bold=True) == "#635bff bold"


def test_style_16_level_uses_named_colour():
    result = style("accent", "16")
    assert result and "#" not in result and "color(" not in result


def test_to_ascii_replaces_known_glyphs():
    assert to_ascii("done ✓ · failed ✗") == "done v - failed x"
    assert to_ascii("wait…").isascii()


def test_to_ascii_covers_bars_dots_achievements_and_frames():
    assert to_ascii("▕██░░▏ ★ ●○") == "[##--] * #-"
    assert to_ascii("━━ › 2 × log()") == "== > 2 x log()"
    assert to_ascii("╭─╮│╰─╯") == "+-+|+-+"
