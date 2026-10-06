"""The opening logo: the mark, the wordmark and the tagline."""

from __future__ import annotations

from rich.console import Group, RenderableType
from rich.table import Table
from rich.text import Text

from playground.copy.en import text
from playground.tui.colour import Level, style, to_ascii
from playground.tui.logo_art import BITMAP, BITMAP_ASCII

# Below this width there's no room for the mark; show the wordmark alone.
NARROW_COLUMNS = 40


def render_logo(width: int, *, level: Level, ascii: bool = False) -> RenderableType:
    """The renderable for the ``ShowLogo`` event, sized to ``width`` columns."""
    wordmark = Text(text("app.wordmark"), style=style("accent", level, bold=True))
    tagline_text = text("app.tagline")
    if ascii:
        tagline_text = to_ascii(tagline_text)
    tagline = Text(tagline_text, style=style("dim", level))

    if width < NARROW_COLUMNS:
        return Group(wordmark, tagline)

    rows = BITMAP_ASCII if ascii else BITMAP
    mark = Text("\n".join(rows), style=style("accent", level))

    header = Table.grid(padding=(0, 2))
    header.add_column()
    header.add_column(vertical="middle")
    header.add_row(mark, wordmark)

    return Group(header, tagline)


__all__ = ["NARROW_COLUMNS", "render_logo"]
