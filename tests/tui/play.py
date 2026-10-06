"""A console session drawn the way a terminal shows it, for transcript tests.

``Play`` hands a real ``Machine``'s events to an ``InteractiveRenderer`` on a
recorded console. Where the real console hands the input line to the user,
it does what the terminal would show: the input line opens (anything printed
meanwhile lands above it), then the typed answer stays on screen as one
line, after the glyph the input line starts with. The effects the runner
would carry out are handed back to the test, which answers them with made-up
outcomes; an effect the runner answers at once (a save, say) can be answered
in place by ``respond``.

Not a test module (pytest only collects ``test_*.py``).
"""

from __future__ import annotations

import io
from collections.abc import Callable, Iterable

import pytest
from rich.console import Console

import playground.tui.console as console_module
from playground.machine.events import (
    AppFailed,
    AppReady,
    AwaitInput,
    ChapterDone,
    Event,
    Exit,
    Expect,
    Say,
    ShowLogo,
)
from playground.machine.machine import Machine
from playground.tui.colour import Level
from playground.tui.console import InteractiveRenderer
from playground.tui.prompt import prompt_glyph

# What the runner hands to the renderer. ChapterDone only marks where a
# chapter ends; everything else is an effect the runner carries out.
SHOWABLE = (Say, ShowLogo, AwaitInput, Exit, AppReady, AppFailed)

# The Rich colour system for each colour level.
COLOUR_SYSTEMS: dict[Level, str | None] = {
    "truecolor": "truecolor",
    "256": "256",
    "16": "standard",
    "none": None,
}


class Play:
    """One console session on a recorded console, answered by the test.

    The console is not a terminal, so a live display (the run panel) prints
    only its final state, once, when it stops: what stays on screen.
    """

    def __init__(
        self,
        monkeypatch: pytest.MonkeyPatch,
        machine: Machine | None = None,
        *,
        level: Level = "none",
        ascii: bool = False,
        width: int = 100,
        respond: Callable[[Event], Event | None] | None = None,
    ) -> None:
        self.machine = machine or Machine()
        self.respond = respond
        self.ascii = ascii
        self.buffer = io.StringIO()
        self.console = Console(file=self.buffer, width=width, color_system=COLOUR_SYSTEMS[level])
        self.renderer = InteractiveRenderer(self.console, level, ascii=ascii)
        # The kind of answer the open input line waits for; None while closed.
        self.expect: Expect | None = None
        self.shown: list[Event] = []
        self.chapters_done: list[int] = []
        monkeypatch.setattr(console_module, "_prompt_open", lambda: self.expect is not None)

    def show(self, events: Iterable[Event]) -> list[Event]:
        """Draw what the renderer is given; return the effects, in order."""
        effects: list[Event] = []
        for event in events:
            if isinstance(event, ChapterDone):
                self.chapters_done.append(event.chapter)
            elif isinstance(event, SHOWABLE):
                self.renderer.show(event)
                self.shown.append(event)
                if isinstance(event, AwaitInput):
                    self.expect = event.expect
            else:
                outcome = self.respond(event) if self.respond is not None else None
                if outcome is None:
                    effects.append(event)
                else:
                    # Carried out in place, as the runner does before going on.
                    effects += self.feed(outcome)
        return effects

    def start(self) -> list[Event]:
        return self.show(self.machine.start())

    def feed(self, event: Event) -> list[Event]:
        """An outcome or notice from the runner's side."""
        return self.show(self.machine.on_event(event))

    def answer(self, line: str) -> list[Event]:
        """The user types ``line`` and presses Enter."""
        assert self.expect is not None, "no input line is open"
        glyph = prompt_glyph(self.expect, ascii=self.ascii)
        self.expect = None
        self.console.print(glyph + line, markup=False, highlight=False, emoji=False)
        return self.show(self.machine.handle(line))

    @property
    def output(self) -> str:
        return self.buffer.getvalue()

    def says(self) -> list[Say]:
        return [event for event in self.shown if isinstance(event, Say)]


__all__ = ["COLOUR_SYSTEMS", "SHOWABLE", "Play"]
