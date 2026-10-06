"""The interactive prompt: history and slash-command completion."""

from __future__ import annotations

import asyncio
import contextlib
import io
import os
import re
import select
import signal
import sys

import pytest
from prompt_toolkit.application import get_app_session
from prompt_toolkit.data_structures import Size
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput
from prompt_toolkit.output.vt100 import Vt100_Output

from playground.tui.prompt import (
    PROMPT_GLYPH,
    PROMPT_GLYPH_ASCII,
    PROMPT_GLYPH_CONFIRM,
    PROMPT_GLYPH_CONFIRM_ASCII,
    Prompt,
    prompt_glyph,
)

# A generous outer bound for anything these tests wait on: a read that
# should already be done, or a background effect (a signal handler firing,
# the prompt drawing, a keystroke or a print reaching the screen) that
# should show up almost at once. It is generous so a slow or busy machine
# still passes; a regression would otherwise hang a test instead of failing
# it. Polls check every _POLL_S and stop as soon as the effect shows up.
_READ_TASK_BOUND_S = 5.0
_POLL_S = 0.01
_POLL_ATTEMPTS = round(_READ_TASK_BOUND_S / _POLL_S)

# The fake terminal the "lands above the prompt" test draws on, and the two
# escape sequences it looks for: erase from the cursor down, and any CSI
# control sequence at all (cursor moves, modes, colours), which draws nothing.
_TERMINAL_SIZE = Size(rows=24, columns=80)
_ERASE_DOWN = "\x1b[J"
_CONTROL_SEQUENCE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")

# How many bytes to take from the pseudo-terminal's far end per read.
_PTY_READ_CHUNK = 4096


def test_read_line_returns_typed_text():
    with create_pipe_input() as pipe_input:
        pipe_input.send_text("/start\n")
        prompt = Prompt(input=pipe_input, output=DummyOutput())
        result = asyncio.run(prompt.read_line("start"))
        assert result == "/start"


def test_history_persists_across_calls():
    with create_pipe_input() as pipe_input:
        pipe_input.send_text("yes\nno\n")
        prompt = Prompt(input=pipe_input, output=DummyOutput())
        first = asyncio.run(prompt.read_line("yes_no"))
        second = asyncio.run(prompt.read_line("yes_no"))
        assert (first, second) == ("yes", "no")
        assert list(prompt._session.history.get_strings()) == ["yes", "no"]


def test_completer_offers_commands_and_answers():
    with create_pipe_input() as pipe_input:
        prompt = Prompt(input=pipe_input, output=DummyOutput())
        words = list(prompt._session.completer.words)
        assert "/start" in words
        assert "/quit" in words
        assert "yes" in words
        assert "no" in words


def test_glyph_switches_for_ascii_mode():
    assert PROMPT_GLYPH != PROMPT_GLYPH_ASCII
    assert PROMPT_GLYPH_ASCII.isascii()


def test_a_yes_no_question_gets_the_question_mark_glyph():
    assert PROMPT_GLYPH_CONFIRM == "? › "
    assert PROMPT_GLYPH_CONFIRM_ASCII == "? > "
    assert prompt_glyph("yes_no") == PROMPT_GLYPH_CONFIRM
    assert prompt_glyph("yes_no", ascii=True) == PROMPT_GLYPH_CONFIRM_ASCII
    for expect in ("continue", "start", "any"):
        assert prompt_glyph(expect) == PROMPT_GLYPH
        assert prompt_glyph(expect, ascii=True) == PROMPT_GLYPH_ASCII


@pytest.mark.parametrize(
    ("expect", "ascii", "glyph"),
    [
        ("yes_no", False, "? › "),
        ("yes_no", True, "? > "),
        ("continue", False, "› "),
        ("start", True, "> "),
    ],
)
def test_read_line_draws_the_glyph_for_what_is_due(expect, ascii, glyph):
    shown: list[str] = []

    async def scenario() -> str | None:
        with create_pipe_input() as pipe_input:
            prompt = Prompt(ascii=ascii, input=pipe_input, output=DummyOutput())

            async def _capture(message: str, **_kwargs: object) -> str:
                shown.append(message)
                return "yes"

            prompt._session.prompt_async = _capture  # type: ignore[method-assign]
            return await prompt.read_line(expect)

    assert asyncio.run(scenario()) == "yes"
    assert shown == [glyph]


def test_ctrl_c_at_the_prompt_reads_as_quit():
    with create_pipe_input() as pipe_input:
        pipe_input.send_text("\x03")
        prompt = Prompt(input=pipe_input, output=DummyOutput())
        assert asyncio.run(prompt.read_line("start")) is None


def test_ctrl_d_on_an_empty_line_reads_as_slash_quit():
    with create_pipe_input() as pipe_input:
        pipe_input.send_text("\x04")
        prompt = Prompt(input=pipe_input, output=DummyOutput())
        assert asyncio.run(prompt.read_line("yes_no")) == "/quit"


def test_an_oserror_out_of_the_prompt_reads_as_quit():
    """The mapping on its own: an ``OSError`` raised by the read means quit now."""

    async def scenario() -> str | None:
        with create_pipe_input() as pipe_input:
            prompt = Prompt(input=pipe_input, output=DummyOutput())

            async def _raise(*_args: object, **_kwargs: object) -> str:
                raise OSError(5, "Input/output error")

            prompt._session.prompt_async = _raise  # type: ignore[method-assign]
            return await prompt.read_line("start")

    assert asyncio.run(scenario()) is None


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX pseudo-terminal")
def test_a_hung_up_terminal_reads_as_quit():
    """A real pseudo-terminal whose far end closes while a read is pending.

    That is what closing the terminal window does: the input reaches end of
    file and the next write to the terminal fails with EIO. ``read_line``
    must come back with ``None`` (quit now) within a bound, not hang and not
    raise.
    """
    from prompt_toolkit.input.vt100 import Vt100Input

    async def scenario() -> str | None:
        far_end, near_end = os.openpty()
        # Separate read and write handles on the same terminal: a terminal is
        # not seekable, so a single "r+" file object fails on some platforms.
        terminal_in = os.fdopen(near_end, "r", closefd=False)
        terminal = os.fdopen(near_end, "w", buffering=1)
        try:
            prompt = Prompt(
                input=Vt100Input(terminal_in),
                output=Vt100_Output.from_pty(terminal, term="xterm"),
            )
            read_task = asyncio.create_task(prompt.read_line("start"))

            drawn = b""
            for _ in range(_POLL_ATTEMPTS):
                if select.select([far_end], [], [], 0)[0]:
                    drawn += os.read(far_end, _PTY_READ_CHUNK)
                if PROMPT_GLYPH.strip().encode() in drawn:
                    break
                await asyncio.sleep(_POLL_S)
            else:
                pytest.fail("the prompt never drew on the terminal")

            os.close(far_end)
            far_end = -1

            done, _ = await asyncio.wait({read_task}, timeout=_READ_TASK_BOUND_S)
            if not done:
                read_task.cancel()
                await asyncio.gather(read_task, return_exceptions=True)
                pytest.fail("read_line kept waiting after the terminal hung up")
            return read_task.result()
        finally:
            if far_end >= 0:
                os.close(far_end)
            # Closing flushes to a terminal that is gone, which fails with EIO.
            with contextlib.suppress(OSError):
                terminal_in.close()
            with contextlib.suppress(OSError):
                terminal.close()

    assert asyncio.run(scenario()) is None


# -- keys typed while no input line was open ------------------------------------------


def test_keys_read_past_an_answer_are_kept_for_the_next_read_unless_discarded():
    async def scenario(discard: bool) -> tuple[str | None, str | None]:
        with create_pipe_input() as pipe_input:
            prompt = Prompt(input=pipe_input, output=DummyOutput())
            pipe_input.send_text("first\r\r\r")  # Enter, then two more, all at once
            first = await prompt.read_line("continue")
            if discard:
                prompt.discard_typeahead()
            pipe_input.send_text("/hint\r")
            return first, await prompt.read_line("continue")

    # Kept, the second Enter answers the next question by itself ...
    assert asyncio.run(scenario(discard=False)) == ("first", "")
    # ... discarded, the next question waits for what is typed after it.
    assert asyncio.run(scenario(discard=True)) == ("first", "/hint")


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX pseudo-terminal")
def test_keys_still_waiting_in_the_terminal_are_discarded_too():
    from prompt_toolkit.input.vt100 import Vt100Input

    async def scenario() -> str | None:
        far_end, near_end = os.openpty()
        terminal_in = os.fdopen(near_end, "r", closefd=False)
        terminal = os.fdopen(near_end, "w", buffering=1)
        try:
            prompt = Prompt(
                input=Vt100Input(terminal_in),
                output=Vt100_Output.from_pty(terminal, term="xterm"),
            )
            # Typed while no input line was open: the terminal holds them.
            os.write(far_end, b"\r\r\r")
            for _ in range(_POLL_ATTEMPTS):
                if select.select([near_end], [], [], 0)[0]:
                    break
                await asyncio.sleep(_POLL_S)
            prompt.discard_typeahead()
            assert not select.select([near_end], [], [], 0)[0]
            os.write(far_end, b"/hint\r")
            return await asyncio.wait_for(prompt.read_line("continue"), _READ_TASK_BOUND_S)
        finally:
            os.close(far_end)
            with contextlib.suppress(OSError):
                terminal_in.close()
            with contextlib.suppress(OSError):
                terminal.close()

    assert asyncio.run(scenario()) == "/hint"


def test_discarding_with_nothing_typed_is_harmless():
    with create_pipe_input() as pipe_input:
        prompt = Prompt(input=pipe_input, output=DummyOutput())
        prompt.discard_typeahead()
        prompt.discard_typeahead()
        pipe_input.send_text("yes\r")
        assert asyncio.run(prompt.read_line("yes_no")) == "yes"


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX signal handlers")
def test_a_loop_sigint_handler_survives_a_prompt():
    """The console's own Ctrl-C handler must still be installed after a prompt."""
    fired = []

    async def scenario() -> None:
        loop = asyncio.get_running_loop()
        loop.add_signal_handler(signal.SIGINT, lambda: fired.append(True))
        try:
            with create_pipe_input() as pipe_input:
                pipe_input.send_text("/start\n")
                prompt = Prompt(input=pipe_input, output=DummyOutput())
                assert await prompt.read_line("start") == "/start"
            os.kill(os.getpid(), signal.SIGINT)
            for _ in range(_POLL_ATTEMPTS):
                if fired:
                    break
                await asyncio.sleep(_POLL_S)
        finally:
            loop.remove_signal_handler(signal.SIGINT)

    try:
        asyncio.run(scenario())
    except KeyboardInterrupt:
        pytest.fail("the prompt removed the loop's SIGINT handler")
    assert fired == [True]


class _TerminalBuffer(io.StringIO):
    """Collects what a terminal would be sent, and says it is one."""

    def isatty(self) -> bool:
        return True


def test_a_print_during_a_pending_read_lands_above_the_prompt_and_keeps_typed_text(
    monkeypatch,
):
    """A print from another task while a read is pending: the prompt and the
    text typed so far are erased first, the notice is written where they
    were, and then they are drawn again below it. The answer still reads
    back whole.

    The console builds its ``Prompt`` on prompt_toolkit's default app
    session, and a patched print only erases and redraws the prompt when it
    can see that session's running application, so this test points that
    same default session at a fake terminal rather than making its own.
    """
    notice = "a notice from elsewhere"
    typed = "/sta"
    screen = _TerminalBuffer()
    terminal = Vt100_Output(screen, lambda: _TERMINAL_SIZE, term="xterm", enable_cpr=False)

    with create_pipe_input() as keyboard:
        session = get_app_session()
        monkeypatch.setattr(session, "_input", keyboard)
        monkeypatch.setattr(session, "_output", terminal)

        async def scenario() -> tuple[str | None, str]:
            prompt = Prompt()
            read_task = asyncio.create_task(prompt.read_line("start"))
            for _ in range(_POLL_ATTEMPTS):
                if session.app is not None:
                    break
                await asyncio.sleep(_POLL_S)
            else:
                pytest.fail("the prompt's application never started")

            keyboard.send_text(typed)
            for _ in range(_POLL_ATTEMPTS):
                if typed in screen.getvalue():
                    break
                await asyncio.sleep(_POLL_S)
            else:
                pytest.fail("the typed text never reached the prompt")

            mark = len(screen.getvalue())
            print(notice)
            for _ in range(_POLL_ATTEMPTS):
                since = screen.getvalue()[mark:]
                if notice in since and PROMPT_GLYPH + typed in since.split(notice, 1)[1]:
                    break
                await asyncio.sleep(_POLL_S)
            else:
                pytest.fail(f"the notice and a redrawn prompt never both arrived: {since!r}")

            keyboard.send_text("rt\n")
            result = await asyncio.wait_for(read_task, timeout=_READ_TASK_BOUND_S)
            return result, since

        result, since = asyncio.run(scenario())

    assert result == "/start"

    before_notice, after_notice = since.split(notice, 1)
    # The prompt line was erased before the notice was written...
    assert _ERASE_DOWN in before_notice, since
    # ...and nothing visible was drawn between the typed text and the notice.
    assert _CONTROL_SEQUENCE.sub("", before_notice).strip() == "", since
    # The prompt and the text typed so far come back below the notice.
    assert PROMPT_GLYPH + typed in after_notice, since
