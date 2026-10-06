"""Colour-level and ASCII-mode detection, plus the console's palette.

The console never guesses a terminal's capabilities from Rich alone: it
picks one of four levels up front, and every renderer resolves the palette
through :func:`style` for that level. This keeps the same escape codes for a
given level regardless of what any other level happened to render first in
the same process (Rich caches resolved styles by their string, so reusing
one string across levels would be unsafe; distinct per-level strings are
not).
"""

from __future__ import annotations

import os
import sys
from collections.abc import Mapping
from typing import Literal

Level = Literal["truecolor", "256", "16", "none"]

# Colour name -> the level's own way to say it. "accent" at 256 colours is
# pinned to index 99 (the palette's chosen approximation of #635bff);
# the rest are the nearest xterm-256 / standard-16 match, computed once with
# rich.color.Color(...).downgrade() against the truecolor values below.
_TRUECOLOR = {
    "accent": "#635bff",
    "good": "#0e9f6e",
    "bad": "#e63946",
    "warn": "#f59e0b",
    "dim": "#8b8f98",
}
_INDEX_256 = {"accent": 99, "good": 35, "bad": 167, "warn": 214, "dim": 245}
_NAME_16 = {
    "accent": "bright_blue",
    "good": "green",
    "bad": "bright_red",
    "warn": "yellow",
    "dim": "white",
}


def detect_level(*, plain: bool = False, env: Mapping[str, str] | None = None) -> Level:
    """Pick a colour level from the environment (and ``--plain``).

    ``NO_COLOR`` (any value) or ``plain`` always wins. Otherwise: truecolor
    when ``COLORTERM`` says so or a Windows Terminal session is detected;
    then 256 colours when ``TERM`` advertises them; else the safe 16.
    """
    env = os.environ if env is None else env
    if plain or "NO_COLOR" in env:
        return "none"
    if env.get("COLORTERM", "").lower() in {"truecolor", "24bit"} or "WT_SESSION" in env:
        return "truecolor"
    if "256color" in env.get("TERM", ""):
        return "256"
    return "16"


def detect_ascii(*, encoding: str | None = None) -> bool:
    """True when the given (or stdout's) encoding isn't UTF-8."""
    if encoding is None:
        encoding = getattr(sys.stdout, "encoding", None)
    normalized = (encoding or "").lower().replace("-", "")
    return normalized != "utf8"


def style(name: str, level: Level, *, bold: bool = False) -> str:
    """A Rich style string for a named palette colour, resolved for ``level``.

    Returns ``""`` at ``"none"``, so callers may pass it straight through
    without a separate no-colour branch.
    """
    if level == "none":
        return ""
    if level == "truecolor":
        colour = _TRUECOLOR[name]
    elif level == "256":
        colour = f"color({_INDEX_256[name]})"
    else:
        colour = _NAME_16[name]
    parts = [colour]
    if bold:
        parts.append("bold")
    if name == "dim":
        parts.append("dim")
    return " ".join(parts)


# Substitutions applied to copy text (and the prompt glyph) in ASCII mode.
# The marks themselves (check/cross/note/not-run) come from playground.copy.en;
# this only covers the punctuation and marks that aren't already ASCII.
ASCII_SUBSTITUTIONS: dict[str, str] = {
    "·": "-",  # middle dot
    "—": "--",  # em dash
    "…": "...",  # ellipsis
    "✓": "v",  # check mark
    "✗": "x",  # cross mark
    "◌": "o",  # not-run circle
    "▲": "^",  # up triangle
    "→": "->",  # right arrow
    "▶": ">",  # play (a dashboard button)
    "■": "[]",  # stop (a dashboard button)
    "─": "-",  # rule
    "•": "*",  # bullet
    "⚠": "!",  # warning glyph (the dashboard's)
    "★": "*",  # achievement star
    "×": "x",  # times, as in "2 × log()"
    "›": ">",  # the prompt glyph
    "●": "#",  # chapter done (progress dots)
    "○": "-",  # chapter to come (progress dots)
    "━": "=",  # heavy rule (chapter headings)
    "█": "#",  # bar, filled
    "░": "-",  # bar, empty
    "▕": "[",  # bar, left end
    "▏": "]",  # bar, right end
    "╭": "+",  # frame corners
    "╮": "+",
    "╰": "+",
    "╯": "+",
    "│": "|",  # frame side
}


def to_ascii(text: str) -> str:
    """Replace the known non-ASCII glyphs in ``text`` with ASCII look-alikes."""
    for glyph, replacement in ASCII_SUBSTITUTIONS.items():
        text = text.replace(glyph, replacement)
    return text


def ascii_only(text: str) -> str:
    """``text`` as ASCII mode prints it: the known glyphs as their look-alikes
    (``to_ascii``), and any other character outside ASCII as ``?``.

    The copy itself never needs the second step; text that comes from
    elsewhere does: a line of the user's code, or the message of an error
    their app raised. A terminal that can't show UTF-8 is never sent a
    character it can't take.
    """
    return to_ascii(text).encode("ascii", errors="replace").decode("ascii")


__all__ = [
    "ASCII_SUBSTITUTIONS",
    "Level",
    "ascii_only",
    "detect_ascii",
    "detect_level",
    "style",
    "to_ascii",
]
