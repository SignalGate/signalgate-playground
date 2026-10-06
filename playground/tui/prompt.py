"""Interactive input: history and slash-command completion via prompt_toolkit."""

from __future__ import annotations

import os
from typing import Any

from prompt_toolkit import PromptSession
from prompt_toolkit.completion import WordCompleter
from prompt_toolkit.history import InMemoryHistory
from prompt_toolkit.input.typeahead import clear_typeahead
from prompt_toolkit.patch_stdout import patch_stdout

from playground.machine.events import ANSWERS, COMMANDS, Expect

try:
    import termios
except ImportError:  # pragma: no cover - Windows has no termios
    termios = None  # type: ignore[assignment]

# The input line's glyph shows what kind of answer is due: a plain one where
# Enter goes on, and one with a question mark where only yes or no answers.
PROMPT_GLYPH = "› "  # "› "
PROMPT_GLYPH_ASCII = "> "
PROMPT_GLYPH_CONFIRM = "? › "
PROMPT_GLYPH_CONFIRM_ASCII = "? > "


def prompt_glyph(expect: Expect, *, ascii: bool = False) -> str:
    """The glyph the input line starts with while the console waits for ``expect``."""
    if expect == "yes_no":
        return PROMPT_GLYPH_CONFIRM_ASCII if ascii else PROMPT_GLYPH_CONFIRM
    return PROMPT_GLYPH_ASCII if ascii else PROMPT_GLYPH


class Prompt:
    """One input line at a time, remembering history across calls."""

    def __init__(self, *, ascii: bool = False, **session_kwargs: Any) -> None:
        self.ascii = ascii
        self._session: PromptSession[str] = PromptSession(
            history=InMemoryHistory(),
            completer=WordCompleter(list(COMMANDS) + list(ANSWERS)),
            **session_kwargs,
        )

    async def read_line(self, expect: Expect) -> str | None:
        """Read one line of input.

        Ctrl-C returns ``None`` (quit now, no drain). Ctrl-D on an empty line
        returns the text ``"/quit"``, handled like a typed ``/quit`` (drain,
        save, bye). A hung-up terminal (``OSError``, for example EIO on a
        closed pty) also returns ``None``. ``expect`` picks the glyph: a
        yes/no question gets the question-mark one (see ``prompt_glyph``);
        the completer offers every command and answer regardless of it.
        """
        glyph = prompt_glyph(expect, ascii=self.ascii)
        try:
            # patch_stdout(raw=True): a print from elsewhere while this read
            # is pending lands above the prompt instead of clobbering it, and
            # the text typed so far is kept. handle_sigint=False: the console
            # owns Ctrl-C. At the prompt the terminal is in raw mode, so
            # Ctrl-C still arrives as a key and quits; outside the prompt the
            # console's own handler must stay put.
            with patch_stdout(raw=True):
                return await self._session.prompt_async(glyph, handle_sigint=False)
        except KeyboardInterrupt:
            return None
        except EOFError:
            return "/quit"
        except OSError:
            return None

    def discard_typeahead(self) -> None:
        """Forget the keys typed while no input line was open.

        The console calls this when a check or a run it started ends, before
        it shows the outcome and opens the next input line: an Enter pressed
        while the check was running would otherwise answer the next question
        by itself (and start the same check again). Keys wait in two places,
        and both are emptied: prompt_toolkit's own buffer of keys it read
        past the last answer, and the terminal's queue of keys nobody has
        read yet (when the input is a terminal). Never raises.
        """
        session_input = self._session.input
        clear_typeahead(session_input)
        if termios is None:
            return
        try:
            fd = session_input.fileno()
            if os.isatty(fd):
                termios.tcflush(fd, termios.TCIFLUSH)
        except (NotImplementedError, OSError, ValueError, termios.error):
            pass


__all__ = [
    "PROMPT_GLYPH",
    "PROMPT_GLYPH_ASCII",
    "PROMPT_GLYPH_CONFIRM",
    "PROMPT_GLYPH_CONFIRM_ASCII",
    "Prompt",
    "prompt_glyph",
]
