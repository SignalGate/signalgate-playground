"""How the interactive console draws each kind of block, as pure functions.

Each function turns copy from ``playground.copy.en`` into Rich renderables
for one colour level (see ``playground.tui.colour``) and ASCII mode. None of
them prints or keeps state; ``playground.tui.console`` decides what goes
where and how it is spaced.

- Paragraphs wrap at ``min(terminal width, PARAGRAPH_COLUMNS)``.
- Code the user pastes is never wrapped, padded or indented: each line is a
  ``Text`` with ``no_wrap`` set and overflow ``"ignore"``, which the console
  prints in soft-wrap mode so the terminal gets the line exactly as written.
- A "Your turn" task and the completion card get a frame line above and
  below, never at the start of a code line.
- ``**bold**`` spans in copy (``playground.copy.en.segments``) render bold
  at every colour level except ``"none"``, where there are no escapes at all.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from pygments.lexers import PythonLexer
from pygments.token import Token
from rich import box
from rich.cells import cell_len
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from playground.copy.en import TASKS, TEXT, segments, text
from playground.machine.events import DeliverProgress, RunProgress, Say
from playground.progress import eta_words
from playground.tui.colour import Level, ascii_only, style

# Paragraphs never run wider than this, however wide the terminal is.
PARAGRAPH_COLUMNS = 100

# How many chapters the tutorial has (the progress dots in a heading).
CHAPTERS = 6

# The delivery bar's cells: the full bar, and the fewest it shrinks to when
# the line would not fit on one terminal row.
BAR_CELLS = 16
BAR_CELLS_MIN = 4

# What separates the parts of a delivery line.
_SEPARATOR = " · "

# A result's leading mark and the colour it gets.
_MARK_COLOURS = {"✓": "good", "✗": "bad", "!": "warn", "◌": "dim"}

# The glyphs the console draws itself (ASCII mode swaps them via to_ascii).
_RULE = "━"
_FRAME = "─"
_DOT_DONE = "●"
_DOT_TODO = "○"
_BAR_FULL = "█"
_BAR_EMPTY = "░"
_BAR_LEFT = "▕"
_BAR_RIGHT = "▏"
_ELLIPSIS = "…"


def paragraph_columns(width: int) -> int:
    """The width a paragraph wraps at on a terminal ``width`` columns wide."""
    return max(1, min(width, PARAGRAPH_COLUMNS))


def _glyphs(value: str, ascii: bool) -> str:
    """``value`` as drawn: in ASCII mode, nothing outside ASCII (see ``ascii_only``)."""
    return ascii_only(value) if ascii else value


def _bold(level: Level) -> str:
    """Bold, except at level ``"none"``, which has no escapes at all."""
    return "" if level == "none" else "bold"


def copy_text(
    key: str, params: Mapping[str, Any] | None = None, *, level: Level, ascii: bool
) -> Text:
    """The copy for ``key`` as a ``Text``, with its ``**bold**`` spans bold.

    At level ``"none"`` nothing is styled, bold included.
    """
    line = Text()
    bold = _bold(level)
    for piece, is_bold in segments(key, **dict(params or {})):
        line.append(_glyphs(piece, ascii), style=bold if is_bold else "")
    return line


def _leading_mark(plain: str) -> tuple[int, str] | None:
    """Where a result's leading mark is (after any spaces), and which it is."""
    at = len(plain) - len(plain.lstrip(" "))
    mark = plain[at : at + 1]
    if mark in _MARK_COLOURS and plain[at + 1 : at + 2] in ("", " "):
        return at, mark
    return None


# The finding lines whose label is shown bold, and the name of their one param.
_LABEL_KEYS = (("verify.why", "why"), ("verify.fix", "fix"))


def _labels() -> tuple[str, ...]:
    """How those lines start: ``"  Why:"`` and ``"  Fix:"``."""
    return tuple(text(key, **{name: ""}).rstrip() for key, name in _LABEL_KEYS)


def render_say(say: Say, *, level: Level, ascii: bool) -> Text:
    """One ``Say`` as a paragraph, styled for its block.

    Headings, tasks and cards have their own functions; any other block is
    drawn here: a note is dim, an achievement is good and bold, and a result
    colours only its leading mark (✓ good, ✗ bad, ! warn, ◌ dim), with a
    finding's "Why:" / "Fix:" label in bold.
    """
    params = dict(say.params)
    line = copy_text(say.key, params, level=level, ascii=ascii)
    if level == "none":
        return line
    if say.block == "note":
        line.style = style("dim", level)
    elif say.block == "achievement":
        line.style = style("good", level, bold=True)
    elif say.block == "result":
        plain = text(say.key, **params)
        found = _leading_mark(plain)
        if found is not None:
            at, mark = found
            line.stylize(style(_MARK_COLOURS[mark], level), at, at + 1)
        if say.key == "verify.line":
            for label in _labels():
                if plain.startswith(label):
                    start = len(label) - len(label.lstrip(" "))
                    line.stylize(_bold(level), start, len(label))
    return line


# -- code --------------------------------------------------------------------------


def _token_colour(token: Any) -> str | None:
    if token in Token.Comment:
        return "dim"
    if token in Token.Literal.String:
        return "good"
    if token in Token.Literal.Number:
        return "warn"
    if token in Token.Keyword:
        return "accent"
    return None


def _code_line(value: str) -> Text:
    # no_wrap + overflow "ignore": never wrapped, cropped or padded.
    return Text(value, no_wrap=True, overflow="ignore")


def code_lines(code: str, *, level: Level) -> list[Text]:
    """``code`` as one ``Text`` per line, exactly as written.

    At a colour level the lines are syntax-coloured (keywords accent,
    strings good, comments dim, numbers warn); the characters, whitespace
    included, never change.
    """
    rows = code.split("\n")
    if level == "none":
        return [_code_line(row) for row in rows]
    if all(set(row) <= {" ", "^", "~"} and row.strip() for row in rows):
        # The marker line Python prints under a syntax error.
        return [
            Text(row, style=style("bad", level), no_wrap=True, overflow="ignore") for row in rows
        ]
    coloured = Text()
    for token, value in PythonLexer(stripnl=False, ensurenl=False).get_tokens(code):
        colour = _token_colour(token)
        coloured.append(value, style=style(colour, level) if colour else "")
    lines = list(coloured.split("\n", allow_blank=True))[: len(rows)]
    if [line.plain for line in lines] != rows:
        return [_code_line(row) for row in rows]
    for line in lines:
        line.no_wrap = True
        line.overflow = "ignore"
    return lines


# -- frames, headings, tasks and the card ------------------------------------------------


def _frame_line(
    left: str,
    words: Text | None,
    right: str,
    *,
    level: Level,
    ascii: bool,
    columns: int,
) -> Text:
    """``left + " " + words + " " + ─── + right``, ``columns`` wide."""
    accent = style("accent", level)
    line = Text(no_wrap=True, overflow="crop")
    line.append(_glyphs(left, ascii), style=accent)
    if words is not None and words.plain:
        line.append(" ")
        line.append_text(words)
        line.append(" ")
    fill = max(1, columns - cell_len(line.plain) - cell_len(right))
    line.append(_glyphs(_FRAME * fill, ascii), style=accent)
    line.append(_glyphs(right, ascii), style=accent)
    return line


def render_heading(chapter: int, title: str, *, level: Level, ascii: bool, width: int) -> Text:
    """A chapter heading: a rule with the chapter, its title and where it sits.

    ``"━━ Chapter 3 of 6 · Teach SignalGate ●●●○○○ ━━━…"``, as wide as a
    paragraph (ASCII: ``"== Chapter 3 of 6 - Teach SignalGate [###---] ===…"``).
    The console puts one blank line before it.
    """
    columns = paragraph_columns(width)
    accent = style("accent", level)
    dim = style("dim", level)
    done = max(0, min(CHAPTERS, chapter))
    line = Text(no_wrap=True, overflow="crop")
    line.append(_glyphs(_RULE * 2, ascii), style=accent)
    line.append(" ")
    words = _glyphs(text("chapter.heading", chapter=chapter, title=title), ascii)
    line.append(words, style=_bold(level))
    line.append(" ")
    if ascii:
        line.append("[")
    line.append(_glyphs(_DOT_DONE * done, ascii), style=accent)
    line.append(_glyphs(_DOT_TODO * (CHAPTERS - done), ascii), style=dim)
    if ascii:
        line.append("]")
    line.append(" ")
    fill = max(2, columns - cell_len(line.plain))
    line.append(_glyphs(_RULE * fill, ascii), style=accent)
    return line


def render_task(task_key: str, *, level: Level, ascii: bool, width: int) -> list[Text]:
    """A "Your turn" task (a ``TASKS`` key), one ``Text`` per line.

    A frame line with the title on top and one with the footer below. In
    between, for each edit: its location line (with its bold spans), a blank
    line, the code exactly as it goes into the file (see ``code_lines``),
    and a blank line.
    """
    columns = paragraph_columns(width)
    title_key, edits, footer_key = TASKS[task_key]
    title = copy_text(title_key, level=level, ascii=ascii)
    title.stylize(_bold(level))
    lines = [_frame_line("╭", title, "╮", level=level, ascii=ascii, columns=columns)]
    for location_key, snippet_key in edits:
        lines.append(copy_text(location_key, level=level, ascii=ascii))
        lines.append(Text())
        lines.extend(code_lines(TEXT[snippet_key], level=level))
        lines.append(Text())
    footer = copy_text(footer_key, level=level, ascii=ascii)
    lines.append(_frame_line("╰", footer, "╯", level=level, ascii=ascii, columns=columns))
    return lines


def render_card(say: Say, *, level: Level, ascii: bool, width: int) -> list[Text]:
    """The completion card: its first line as a framed title, the other lines
    plain, and a frame line below."""
    columns = paragraph_columns(width)
    rows = list(copy_text(say.key, say.params, level=level, ascii=ascii).split("\n"))
    title = rows[0] if rows else Text()
    title.stylize(_bold(level))
    lines = [_frame_line("╭", title, "╮", level=level, ascii=ascii, columns=columns)]
    lines.extend(rows[1:])
    lines.append(_frame_line("╰", None, "╯", level=level, ascii=ascii, columns=columns))
    return lines


# -- delivery ------------------------------------------------------------------------


def render_bar(done: int, total: int, *, cells: int, level: Level, ascii: bool) -> Text:
    """``▕████░░░░▏``: ``done`` of ``total`` as ``cells`` cells (empty when
    ``total`` is 0; full only once ``done`` reaches it)."""
    filled = 0 if total <= 0 else max(0, min(cells, done * cells // total))
    bar = Text()
    bar.append(_glyphs(_BAR_LEFT, ascii))
    bar.append(_glyphs(_BAR_FULL * filled, ascii), style=style("accent", level))
    bar.append(_glyphs(_BAR_EMPTY * (cells - filled), ascii), style=style("dim", level))
    bar.append(_glyphs(_BAR_RIGHT, ascii))
    return bar


def _count(done: int, total: int) -> str:
    return text("drain.count", done=max(0, done), total=max(0, total))


def render_drain_line(
    event: DeliverProgress, *, quit_hint: bool, level: Level, ascii: bool, width: int
) -> Text:
    """The one line shown while the app's SDK sends its queued log events.

    ``"Sending your app's queued logs to SignalGate ▕████░░░░▏ 480 / 922 ·
    about 30 s left · Ctrl-C to skip"``, without the time when there's no
    estimate, and with "Ctrl-C to quit now" when ``quit_hint``. It is one
    terminal row, at most ``width`` columns. The count and what Ctrl-C does
    always stay; when the line doesn't fit, the bar first shrinks from
    ``BAR_CELLS`` to ``BAR_CELLS_MIN`` cells, then the words in front go,
    then the time left, then the bar shrinks away to nothing. Only a line
    that still doesn't fit is cut short, with an ellipsis.
    """
    width = max(1, width)
    count = _count(event.done, event.total)
    eta = eta_words(event.eta_s)
    hint = text("drain.quit_hint" if quit_hint else "drain.skip_hint")
    head = text("drain.sending")

    def build(cells: int | None, *, with_head: bool, with_eta: bool) -> Text:
        line = Text(no_wrap=True, overflow="crop")
        if with_head:
            line.append(_glyphs(head, ascii))
            line.append(" ")
        if cells is not None:
            bar = render_bar(event.done, event.total, cells=cells, level=level, ascii=ascii)
            line.append_text(bar)
            line.append(" ")
        parts = [count, *([eta] if with_eta and eta is not None else []), hint]
        line.append(_glyphs(_SEPARATOR.join(parts), ascii))
        return line

    def fitted(*, with_head: bool, with_eta: bool, fewest: int) -> Text | None:
        """The line with the widest bar between ``fewest`` and ``BAR_CELLS`` cells that fits."""
        line = build(BAR_CELLS, with_head=with_head, with_eta=with_eta)
        over = cell_len(line.plain) - width
        if over <= 0:
            return line
        if BAR_CELLS - over >= fewest:
            return build(BAR_CELLS - over, with_head=with_head, with_eta=with_eta)
        return None

    # What gives way, in turn: the bar down to its fewest cells, the words in
    # front, the time left, and at last the bar itself.
    for with_head, with_eta, fewest in (
        (True, True, BAR_CELLS_MIN),
        (False, True, BAR_CELLS_MIN),
        (False, False, 0),
    ):
        line = fitted(with_head=with_head, with_eta=with_eta, fewest=fewest)
        if line is not None:
            return line
    line = build(None, with_head=False, with_eta=False)
    if cell_len(line.plain) > width:
        ellipsis = _glyphs(_ELLIPSIS, ascii)
        line.truncate(max(0, width - cell_len(ellipsis)), overflow="crop")
        line.append(ellipsis)
        line.truncate(width, overflow="crop")
    return line


def _delivered(event: RunProgress, *, level: Level, ascii: bool) -> Text | str:
    """The panel's "Delivered by your app" value: a bar of delivered / logged,
    the counts, and the time left once it is known."""
    if event.delivered is None or event.posted is None:
        return _glyphs(text("panel.pending"), ascii)
    value = render_bar(event.delivered, event.posted, cells=BAR_CELLS, level=level, ascii=ascii)
    value.append(" ")
    value.append(_glyphs(_count(event.delivered, event.posted), ascii))
    eta = eta_words(event.eta_s)
    if eta is not None:
        value.append(_glyphs(_SEPARATOR + eta, ascii))
    return value


def render_panel(
    *,
    level: Level,
    ascii: bool,
    total: int,
    done: int,
    actions: int,
    successes: int,
    title: str = "ch1.panel.title",
    event: RunProgress | None = None,
) -> Panel:
    """The progress panel at a given point in a run.

    A run through SignalGate (``event`` carries its log counters) adds what
    your code logged, what your app delivered (as a bar, with the time left
    once every visitor is done), what SignalGate accepted and, once the run
    is over, what it stored.
    """

    def rendered(key: str, **params: Any) -> str:
        return _glyphs(text(key, **params), ascii)

    conversion = "-" if actions == 0 else f"{successes / actions:.1%}"
    table = Table.grid(padding=(0, 2))
    table.add_column(justify="left", style=style("dim", level))
    table.add_column(justify="right")
    table.add_row(
        rendered("ch1.panel.visitors"), rendered("ch1.panel.progress", done=done, total=total)
    )
    table.add_row(rendered("ch1.panel.actions"), f"{actions:,}")
    table.add_row(rendered("ch1.panel.successes"), f"{successes:,}")
    table.add_row(rendered("ch1.panel.conversion"), conversion)
    if event is not None and event.posted is not None:
        pending = rendered("panel.pending")
        table.add_row(rendered("panel.posted"), f"{event.posted:,}")
        table.add_row(rendered("panel.delivered"), _delivered(event, level=level, ascii=ascii))
        for key, value in (("panel.accepted", event.accepted), ("panel.stored", event.stored)):
            table.add_row(rendered(key), pending if value is None else f"{value:,}")
    return Panel(
        table,
        title=rendered(title),
        border_style=style("accent", level),
        box=box.ASCII if ascii else box.ROUNDED,
    )


__all__ = [
    "BAR_CELLS",
    "BAR_CELLS_MIN",
    "CHAPTERS",
    "PARAGRAPH_COLUMNS",
    "code_lines",
    "copy_text",
    "paragraph_columns",
    "render_bar",
    "render_card",
    "render_drain_line",
    "render_heading",
    "render_panel",
    "render_say",
    "render_task",
]
