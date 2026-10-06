"""The interactive console renderer: styled blocks, a live progress panel, a
one-line delivery bar, and a spinner line while the console waits for
SignalGate (never a clock).

What it prints is grouped into blocks (``Say.block``, see
``playground.copy.blocks``), with one blank line between blocks of different
kinds and never two blank lines in a row. How each block looks is drawn by
``playground.tui.render``; this module decides where things go:

- one blank line before a chapter heading, and before and after the run's
  live panel;
- the question right before the input line (an ``ask``) sits directly above
  it; anything else gets a blank line before the input line, and the first
  thing printed after an answer gets one too;
- "Your turn" tasks, achievements and the card always stand apart;
- a "Saved" line right after the same line isn't printed again, so several
  saves in a row show one line.
"""

from __future__ import annotations

from typing import Any

from prompt_toolkit.application import get_app_or_none
from rich.console import Console, ConsoleOptions, Group, RenderResult
from rich.constrain import Constrain
from rich.live import Live
from rich.panel import Panel
from rich.status import Status
from rich.text import Text

from playground.copy.en import TASKS, text
from playground.machine.events import (
    AppFailed,
    AppReady,
    AwaitInput,
    DeliverProgress,
    Event,
    Exit,
    RunProgress,
    Say,
    ShowLogo,
)
from playground.tui.colour import Level, ascii_only, style
from playground.tui.logo import render_logo
from playground.tui.render import (
    code_lines,
    paragraph_columns,
    render_card,
    render_drain_line,
    render_heading,
    render_panel,
    render_say,
    render_task,
)

# Blocks that always stand apart, even from a neighbour of the same kind.
_ALONE = frozenset({"task", "card", "achievement"})

# The one line shown only once for several saves in a row.
_SAVED_KEY = "app.reloaded"

# The first line of a wait for the app's queued log events that says Ctrl-C
# quits now (rather than skipping the wait).
_DRAIN_QUIT_KEY = "drain.waiting_quit"


def _prompt_open() -> bool:
    """Whether the input line is open right now.

    While it is, whatever the console prints lands above it, so that output
    comes before the answer, not after it.
    """
    app = get_app_or_none()
    return app is not None and app.is_running


class _DrainLine:
    """The delivery line, drawn afresh at the console's width on every refresh."""

    def __init__(self, renderer: InteractiveRenderer, event: DeliverProgress, quit_hint: bool):
        self._renderer = renderer
        self.event = event
        self.quit_hint = quit_hint

    def __rich_console__(self, console: Console, options: ConsoleOptions) -> RenderResult:
        yield render_drain_line(
            self.event,
            quit_hint=self.quit_hint,
            level=self._renderer.level,
            ascii=self._renderer.ascii,
            width=options.max_width,
        )


class InteractiveRenderer:
    """Renders machine events to a live terminal, honouring ``level`` and ASCII mode."""

    def __init__(self, console: Console, level: Level, *, ascii: bool = False) -> None:
        self.console = console
        self.level = level
        self.ascii = ascii
        self._live: Live | None = None
        self._status: Status | None = None
        self._total = 0
        self._title = "ch1.panel.title"
        self._online = False
        self._quiet = False
        # The run panel as last drawn, and the dim note under it while the
        # app's queued log events are sent.
        self._panel: Panel | None = None
        self._note: Text | None = None
        # The wait for the app's queued log events: "line" (its own live
        # line), "panel" (a note under the run panel) or "static" (its first
        # line printed once); None when there's no such wait.
        self._drain_mode: str | None = None
        self._drain_live: Live | None = None
        self._drain_line: _DrainLine | None = None
        # Spacing: the block of the last thing printed (a Say's block, or
        # "logo", "panel", "wait", "drain"), whether the last line printed
        # was blank, and whether an answer is due before the next output.
        self._last_block: str | None = None
        self._last_blank = True
        self._answer_pending = False
        # The last Say printed, as (key, text), for the "Saved" line.
        self._last_say: tuple[str, str] | None = None

    # -- printing --------------------------------------------------------------

    def _text(self, key: str, **params: Any) -> str:
        rendered = text(key, **params)
        # The params may be anything (a line of the user's code, an error's
        # message): in ASCII mode nothing outside ASCII reaches the terminal.
        return ascii_only(rendered) if self.ascii else rendered

    def _columns(self) -> int:
        return paragraph_columns(self.console.width)

    def _blank(self) -> None:
        self.console.print()
        self._last_blank = True

    def _emit(self, line: Text) -> None:
        if line.overflow == "ignore":
            # Code: never wrapped, cropped or padded; the terminal gets it as is.
            self.console.print(line, soft_wrap=True, highlight=False)
        else:
            self.console.print(line, width=self._columns(), highlight=False)
        self._last_blank = not line.plain

    def _space_before(self, block: str) -> None:
        """Print the blank line ``block`` needs before it, if any."""
        if self._answer_pending and not _prompt_open():
            # The first output after an answer: the answer's line is above.
            self._answer_pending = False
            self._last_blank = False
            need = True
        else:
            follows = self._last_block is not None
            need = block == "heading" or (
                follows and (block != self._last_block or block in _ALONE)
            )
        if need and not self._last_blank:
            self._blank()

    def _lines(self, say: Say) -> list[Text]:
        width = self.console.width
        params = dict(say.params)
        if say.key in TASKS:
            return render_task(say.key, level=self.level, ascii=self.ascii, width=width)
        if say.block == "card":
            return render_card(say, level=self.level, ascii=self.ascii, width=width)
        if say.block == "heading" and "chapter" in params and "title" in params:
            heading = render_heading(
                int(params["chapter"]),
                str(params["title"]),
                level=self.level,
                ascii=self.ascii,
                width=width,
            )
            return [heading]
        if say.block == "code":
            return code_lines(self._text(say.key, **params), level=self.level)
        return [render_say(say, level=self.level, ascii=self.ascii)]

    def _say(self, say: Say) -> None:
        plain = text(say.key, **dict(say.params))
        if say.key == _SAVED_KEY and self._last_say == (say.key, plain):
            return  # the same line is already the last thing on screen
        block = say.block or "info"
        self._space_before(block)
        # A Say shown while a live display is up prints through this same
        # console, so Rich renders it above that display instead of
        # clobbering it.
        for line in self._lines(say):
            self._emit(line)
        self._last_block = block
        self._last_say = (say.key, plain)

    def show(self, event: Event) -> None:
        if self._quiet:
            return
        try:
            self._show(event)
        except OSError:
            # A hung-up terminal: go quiet from here on.
            self._quiet = True

    def _show(self, event: Event) -> None:
        if isinstance(event, Say):
            self._say(event)
        elif isinstance(event, ShowLogo):
            self._space_before("logo")
            logo = render_logo(self.console.width, level=self.level, ascii=self.ascii)
            self.console.print(logo)
            self._last_block = "logo"
            self._last_blank = False
            self._last_say = None
        elif isinstance(event, AwaitInput):
            # The input line itself is drawn by playground.tui.prompt. A
            # question sits right above it; anything else gets a blank line.
            if self._answer_pending and not _prompt_open():
                # Straight after an answer: its line is the last one shown.
                self._blank()
            elif self._last_block != "ask" and not self._last_blank:
                self._blank()
            self._answer_pending = True
        elif isinstance(event, Exit):
            pass  # the runner stops the console; nothing left to draw
        elif isinstance(event, AppReady):
            self._say(Say("app.ready"))
        elif isinstance(event, AppFailed):
            self._say(Say(event.reason))
        else:
            raise TypeError(f"InteractiveRenderer cannot show {event!r}")

    # -- the run panel -----------------------------------------------------------

    def _render_panel(
        self, *, done: int, actions: int, successes: int, event: RunProgress | None = None
    ) -> Panel:
        return render_panel(
            level=self.level,
            ascii=self.ascii,
            total=self._total,
            done=done,
            actions=actions,
            successes=successes,
            title=self._title,
            event=event,
        )

    def _panel_renderable(self) -> Panel | Group:
        assert self._panel is not None
        if self._note is None:
            return self._panel
        return Group(self._panel, Constrain(self._note, width=self._columns()))

    def run_started(self, total: int, *, title: str | None = None) -> None:
        if self._quiet:
            return
        self._stop_drain_live()
        self._total = total
        self._title = title or "ch1.panel.title"
        self._online = title is not None
        first = RunProgress(0, total, 0, 0, posted=0, delivered=0, accepted=0)
        self._panel = self._render_panel(
            done=0, actions=0, successes=0, event=first if self._online else None
        )
        self._note = None
        live = Live(self._panel, console=self.console, refresh_per_second=8)
        try:
            self._space_before("panel")
            live.start()
        except OSError:
            self._quiet = True
            return
        self._live = live
        # Lines printed while it is up land above it, right after the blank.
        self._last_block = "panel"
        self._last_blank = True
        self._last_say = None

    def progress(self, event: RunProgress) -> None:
        if self._quiet or self._live is None:
            return
        self._panel = self._render_panel(
            done=event.done, actions=event.actions, successes=event.successes, event=event
        )
        try:
            self._live.update(self._panel_renderable())
        except OSError:
            self._quiet = True

    def run_ended(self) -> None:
        if self._live is None:
            return
        self._note = None
        if self._drain_mode == "panel":
            self._drain_mode = None
        try:
            try:
                if self._panel is not None:
                    self._live.update(self._panel)
            finally:
                # Always stopped, so its refresh thread never outlives the run.
                self._live.stop()
            if not self.console.is_terminal:
                # Off a terminal, Rich prints the last panel once, at the
                # stop, and leaves its last line open.
                self.console.line()
        except OSError:
            self._quiet = True
        finally:
            self._live = None
            self._last_block = "panel"
            self._last_blank = False

    # -- the wait for the app's queued log events ---------------------------------

    def drain_begin(self, first: Say, *, inline: bool = True) -> None:
        """The wait for the app's queued log events starts.

        With the run panel up, a dim note goes under it (the panel's own
        delivery bar shows the progress). With the input line open
        (``inline`` False) or a spinner line up, ``first`` is printed once
        and the progress isn't shown. Otherwise a single line shows the
        progress in place until ``drain_end``.
        """
        if self._quiet:
            return
        self._stop_drain_live()
        quit_hint = first.key == _DRAIN_QUIT_KEY
        try:
            if self._live is not None and self._panel is not None:
                self._drain_mode = "panel"
                note = self._text("drain.note_quit" if quit_hint else "drain.note")
                self._note = Text(note, style=style("dim", self.level))
                self._live.update(self._panel_renderable(), refresh=True)
                return
            if not inline or self._status is not None:
                self._drain_mode = "static"
                self._say(first)
                return
            remaining = int(dict(first.params).get("remaining", 0) or 0)
            self._drain_mode = "line"
            self._drain_line = _DrainLine(self, DeliverProgress(0, remaining, None), quit_hint)
            self._space_before("drain")
            live = Live(
                self._drain_line, console=self.console, transient=True, refresh_per_second=4
            )
            live.start(refresh=True)
            self._drain_live = live
            self._last_block = "drain"
        except OSError:
            self._quiet = True

    def drain_progress(self, event: DeliverProgress) -> None:
        """How far that wait has got: redraws the delivery line, if one is up."""
        if self._quiet or self._drain_mode != "line" or self._drain_live is None:
            return
        assert self._drain_line is not None
        self._drain_line.event = event
        try:
            self._drain_live.refresh()
        except OSError:
            self._quiet = True

    def drain_end(self) -> None:
        """That wait is over: the delivery line goes away (or the note under
        the panel does), and the runner prints the wait's last line next."""
        mode, self._drain_mode = self._drain_mode, None
        if mode == "line":
            self._stop_drain_live()
        elif mode == "panel" and self._live is not None:
            self._note = None
            try:
                if self._panel is not None:
                    self._live.update(self._panel, refresh=True)
            except OSError:
                self._quiet = True

    def _stop_drain_live(self) -> None:
        live, self._drain_live = self._drain_live, None
        self._drain_line = None
        if self._drain_mode == "line":
            self._drain_mode = None
        if live is None:
            return
        try:
            live.stop()  # transient: the line is erased
        except OSError:
            self._quiet = True

    # -- the spinner line ----------------------------------------------------------

    def wait(self, event: Say) -> None:
        """Show (or change) the spinner line while the console waits for SignalGate."""
        if self._quiet:
            return
        words = self._text(event.key, **dict(event.params))
        try:
            if self._live is not None or self._drain_live is not None:
                self._say(event)
            elif self._status is None:
                self._space_before("wait")
                self._status = self.console.status(words, spinner="line" if self.ascii else "dots")
                self._status.start()
                self._last_block = "wait"
            else:
                self._status.update(words)
        except OSError:
            self._quiet = True

    def wait_ended(self) -> None:
        status, self._status = self._status, None
        if status is None:
            return
        try:
            status.stop()
        except OSError:
            self._quiet = True


__all__ = [
    "InteractiveRenderer",
    "render_card",
    "render_drain_line",
    "render_heading",
    "render_panel",
    "render_task",
]
