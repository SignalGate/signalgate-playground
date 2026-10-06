"""Snapshot tests for the logo at every colour level and layout mode."""

from __future__ import annotations

import io
import re

import pytest
from rich.console import Console

from playground.tui.logo import NARROW_COLUMNS, render_logo

COLOR_SYSTEM_BY_LEVEL = {"truecolor": "truecolor", "256": "256", "16": "standard", "none": None}

# ESC[ ... m where the parameters include a colour introducer (3x/4x/9x/38/48).
COLOUR_SGR_RE = re.compile(r"\x1b\[[0-9;]*(?:3[0-9]|4[0-9]|9[0-9]|38|48)[0-9;]*m")


def _render(level: str, *, ascii: bool = False, width: int = 60) -> str:
    # A real file object (rather than export_text(styles=True), which always
    # renders as if truecolor were available) so the escapes we capture are
    # the ones this exact colour level would actually emit.
    buffer = io.StringIO()
    console = Console(
        record=True,
        file=buffer,
        width=width,
        color_system=COLOR_SYSTEM_BY_LEVEL[level],
        force_terminal=True,
    )
    console.print(render_logo(width, level=level, ascii=ascii))
    return buffer.getvalue()


@pytest.mark.parametrize("level", ["truecolor", "256", "16", "none"])
def test_logo_snapshot_per_colour_level(snapshot, level):
    snapshot(f"logo_{level}", _render(level))


def test_none_level_has_no_colour_escapes():
    output = _render("none")
    assert "\x1b[" not in output


def test_truecolor_level_has_colour_escapes():
    output = _render("truecolor")
    assert COLOUR_SGR_RE.search(output)


def test_ascii_mode_is_pure_ascii(snapshot):
    output = _render("truecolor", ascii=True)
    assert output.isascii()
    snapshot("logo_ascii", output)


def test_narrow_width_renders_wordmark_only(snapshot):
    output = _render("truecolor", width=NARROW_COLUMNS - 1)
    snapshot("logo_narrow", output)
    # The half-block mark needs real width; under the threshold we fall back
    # to plain wordmark + tagline text only.
    assert "█" not in output and "▀" not in output and "▄" not in output
