"""Snapshot tests for the interactive renderer: copy text and the live panel."""

from __future__ import annotations

import io
import sys

import pytest
from rich.console import Console

from playground.copy.en import TEXT, text
from playground.machine.events import AwaitInput, Exit, RunProgress, Say, ShowLogo
from playground.tui.colour import ascii_only, to_ascii
from playground.tui.console import InteractiveRenderer, render_panel
from tests.tui.fixtures import FIXTURE_EVENTS, ONLINE_FIXTURE_EVENTS

# Keys the interactive renderer can actually be asked to show as a "Say".
# Command-line help/error text and the panel/mark/branding keys are used
# elsewhere (cli.py, render_panel, logo.py), not through Say; the verifier's
# words are put together by playground.verify.messages (tested in tests/verify).
# The parts of a "Your turn" task, of the delivery line and of its time left
# are put together by the renderer (a task is shown as one Say of its TASKS
# key).
NON_SAY_PREFIXES = (
    "cli.",
    "mark.",
    "ch1.panel.",
    "app.wordmark",
    "app.tagline",
    "verify.",
    "finding.",
    "snippet.",
    "ch3.edit.",
    "ch5.edit.",
    "ch3.task.",
    "ch5.task.",
    "eta.",
    "drain.sending",
    "drain.count",
    "drain.skip_hint",
    "drain.quit_hint",
)
CHAPTER_TITLE_KEYS = {f"chapter.{n}" for n in range(1, 7)} | {"chapter.heading"}


def test_fixture_events_cover_every_say_key():
    events = (*FIXTURE_EVENTS, *ONLINE_FIXTURE_EVENTS)
    covered = {event.key for event in events if hasattr(event, "key")}
    covered |= {"app.ready", "app.failed"}  # rendered via AppReady/AppFailed, not Say
    expected = {
        key
        for key in TEXT
        if not key.startswith(NON_SAY_PREFIXES) and key not in CHAPTER_TITLE_KEYS
    }
    missing = expected - covered
    assert not missing, f"fixture events don't cover: {sorted(missing)}"


def _render_fixture_transcript(level: str, colour_system, *, ascii: bool = False) -> str:
    buffer = io.StringIO()
    console = Console(
        record=True, file=buffer, width=100, color_system=colour_system, force_terminal=True
    )
    renderer = InteractiveRenderer(console, level, ascii=ascii)
    for event in FIXTURE_EVENTS:
        renderer.show(event)
    return buffer.getvalue()


def test_say_keys_transcript_snapshot(snapshot):
    snapshot("console_transcript_truecolor", _render_fixture_transcript("truecolor", "truecolor"))


def test_say_keys_transcript_is_plain_at_none_level(snapshot):
    output = _render_fixture_transcript("none", None)
    assert "\x1b[" not in output
    snapshot("console_transcript_none", output)


def test_say_keys_transcript_is_pure_ascii_in_ascii_mode(snapshot):
    output = _render_fixture_transcript("none", None, ascii=True)
    assert output.isascii()
    snapshot("console_transcript_ascii", output)


def test_the_online_chapters_render_in_every_mode():
    for level, colour_system, ascii_mode in (
        ("truecolor", "truecolor", False),
        ("none", None, False),
        ("none", None, True),
    ):
        buffer = io.StringIO()
        console = Console(
            record=True, file=buffer, width=100, color_system=colour_system, force_terminal=True
        )
        renderer = InteractiveRenderer(console, level, ascii=ascii_mode)
        for event in ONLINE_FIXTURE_EVENTS:
            renderer.show(event)
        output = buffer.getvalue()
        assert output.isascii() or not ascii_mode
        if level == "none":
            assert "\x1b[" not in output


def test_every_copy_string_is_pure_ascii_after_to_ascii():
    for key, value in TEXT.items():
        assert to_ascii(value).isascii(), f"{key} has no ASCII fallback for one of its glyphs"


def _panel_text(*, done: int, actions: int, successes: int) -> str:
    buffer = io.StringIO()
    console = Console(
        record=True, file=buffer, width=60, color_system="truecolor", force_terminal=True
    )
    console.print(
        render_panel(
            level="truecolor",
            ascii=False,
            total=10_000,
            done=done,
            actions=actions,
            successes=successes,
        )
    )
    return buffer.getvalue()


def test_panel_at_zero_progress_shows_dash_conversion(snapshot):
    output = _panel_text(done=0, actions=0, successes=0)
    assert "-" in output
    snapshot("panel_progress_0", output)


def test_panel_at_partial_progress(snapshot):
    output = _panel_text(done=4_200, actions=3_800, successes=760)
    assert "20.0%" in output
    snapshot("panel_progress_partial", output)


def test_panel_at_complete_progress(snapshot):
    output = _panel_text(done=10_000, actions=10_000, successes=2_000)
    assert "20.0%" in output
    snapshot("panel_progress_complete", output)


def test_show_handles_await_input_and_exit_without_raising():
    buffer = io.StringIO()
    console = Console(record=True, file=buffer, width=60, color_system=None, force_terminal=True)
    renderer = InteractiveRenderer(console, "none")
    renderer.show(AwaitInput("yes_no"))
    renderer.show(Exit(0))
    renderer.show(ShowLogo())
    assert "SignalGate" in buffer.getvalue()  # the wordmark, at least, always shows


def test_run_lifecycle_updates_the_live_panel_and_stops_cleanly():
    buffer = io.StringIO()
    console = Console(
        record=True, file=buffer, width=60, color_system="truecolor", force_terminal=True
    )
    renderer = InteractiveRenderer(console, "truecolor")
    renderer.run_started(10_000)
    renderer.progress(RunProgress(done=100, total=10_000, actions=95, successes=19))
    renderer.run_ended()
    output = buffer.getvalue()
    assert "10,000" in output
    assert renderer._live is None


def test_a_say_during_a_run_prints_through_the_live_console():
    """A notice shown while Live is up lands above the panel, not lost."""
    buffer = io.StringIO()
    console = Console(record=True, file=buffer, width=100, color_system=None, force_terminal=True)
    renderer = InteractiveRenderer(console, "none")
    renderer.run_started(10)
    renderer.show(Say("reload.deferred", {}))
    renderer.run_ended()
    output = buffer.getvalue()
    assert text("reload.deferred") in output


class _BrokenConsoleFile:
    def write(self, data: str) -> int:
        raise OSError(5, "Input/output error")

    def flush(self) -> None:
        pass

    def isatty(self) -> bool:
        return True


class _SwitchableConsoleFile:
    """Works until ``raising`` is set, so ``Live.start()`` can succeed first."""

    def __init__(self) -> None:
        self.raising = False

    def write(self, data: str) -> int:
        if self.raising:
            raise OSError(5, "Input/output error")
        return len(data)

    def flush(self) -> None:
        if self.raising:
            raise OSError(5, "Input/output error")

    def isatty(self) -> bool:
        return True


def test_show_handles_await_input_and_exit_without_touching_the_file():
    console = Console(file=_BrokenConsoleFile(), width=60, color_system=None, force_terminal=True)
    renderer = InteractiveRenderer(console, "none")
    renderer.show(AwaitInput("yes_no"))  # a no-op branch, never touches the file
    renderer.show(Exit(0))  # ditto
    assert not renderer._quiet


def test_show_swallows_oserror_on_a_fresh_renderer_with_a_raising_file():
    """``show``'s own guard, exercised on a renderer that was never quiet."""
    console = Console(file=_BrokenConsoleFile(), width=60, color_system=None, force_terminal=True)
    renderer = InteractiveRenderer(console, "none")
    assert not renderer._quiet
    renderer.show(Say("reply.no", {}))  # must not raise
    assert renderer._quiet


def test_run_started_on_a_raising_file_goes_quiet_and_nothing_after_it_raises():
    """``run_started``'s own guard: the terminal is already gone when a run
    starts, so the live panel never comes up, the renderer goes quiet, and
    the rest of the run's calls do nothing instead of raising.
    """
    stdout_before, stderr_before = sys.stdout, sys.stderr
    console = Console(file=_BrokenConsoleFile(), width=60, color_system=None, force_terminal=True)
    renderer = InteractiveRenderer(console, "none")
    assert not renderer._quiet

    renderer.run_started(10)  # must not raise
    assert renderer._quiet
    assert renderer._live is None
    # A panel that never started must not leave prints elsewhere routed into it.
    assert sys.stdout is stdout_before
    assert sys.stderr is stderr_before

    renderer.progress(RunProgress(done=1, total=10, actions=1, successes=0))  # must not raise
    renderer.run_ended()  # must not raise
    renderer.show(Say("reply.no", {}))  # must not raise
    assert renderer._quiet
    assert renderer._live is None


def test_progress_and_run_ended_each_swallow_their_own_oserror():
    """``Live.start()`` succeeds first, so ``_live`` is set and the renderer
    is not yet quiet. ``Live.update()`` never writes synchronously by
    itself (it only marks the renderable dirty for the next auto-refresh
    tick), so ``progress``'s own guard is exercised by making ``update``
    itself raise directly; ``run_ended``'s guard is exercised for real, by
    switching the file to raising before ``Live.stop()``'s own synchronous
    refresh runs. Neither must inherit an early quiet flag from the other.
    """
    file = _SwitchableConsoleFile()
    console = Console(file=file, width=60, color_system=None, force_terminal=True)
    renderer = InteractiveRenderer(console, "none")

    renderer.run_started(10)
    assert not renderer._quiet
    assert renderer._live is not None

    def _raise(*_args: object, **_kwargs: object) -> None:
        raise OSError(5, "Input/output error")

    renderer._live.update = _raise  # type: ignore[method-assign]
    renderer.progress(RunProgress(done=1, total=10, actions=1, successes=0))  # must not raise
    assert renderer._quiet  # progress's own guard caught it

    file.raising = True  # now Live.stop()'s own synchronous refresh fails too
    live = renderer._live
    renderer.run_ended()  # must not raise; run_ended never gates on _quiet
    assert renderer._live is None
    # Stopped all the same: no refresh thread is left writing to the terminal.
    assert not live.is_started


def test_a_run_through_signalgate_shows_its_log_counters_in_the_panel():
    buffer = io.StringIO()
    console = Console(record=True, file=buffer, width=100, color_system=None, force_terminal=True)
    panel = render_panel(
        level="none",
        ascii=True,
        total=2_000,
        done=2_000,
        actions=2_000,
        successes=400,
        title="panel.learn.title",
        event=RunProgress(2_000, 2_000, 2_000, 400, posted=2_400, delivered=2_400, accepted=2_398),
    )
    console.print(panel)
    output = buffer.getvalue()
    assert output.isascii()
    for key, value in (
        ("panel.learn.title", ""),
        ("panel.posted", "2,400"),
        ("panel.accepted", "2,398"),
        ("panel.stored", "-"),
    ):
        assert to_ascii(text(key)) in output
        assert value in output


def test_the_spinner_line_comes_and_goes_without_a_clock():
    buffer = io.StringIO()
    console = Console(record=True, file=buffer, width=100, color_system=None, force_terminal=True)
    renderer = InteractiveRenderer(console, "none", ascii=True)
    renderer.wait(Say("wait.queued", {}))
    renderer.wait(Say("wait.dry_running", {"stage": 1, "stages_total": 3}))
    renderer.wait_ended()
    renderer.wait_ended()  # a second end is a no-op
    output = buffer.getvalue()
    assert "dry run" in output
    assert not any(ch.isdigit() and f"{ch}s" in output for ch in "0123456789")


def test_the_drawing_functions_are_importable_from_the_console_module():
    from playground.tui import console as console_module
    from playground.tui import render

    for name in (
        "render_card",
        "render_drain_line",
        "render_heading",
        "render_panel",
        "render_task",
    ):
        assert getattr(console_module, name) is getattr(render, name)
        assert name in console_module.__all__


# -- ASCII mode on a terminal that takes ASCII only ---------------------------------------


def _ascii_only_terminal() -> tuple[io.BytesIO, Console]:
    """A console whose stream raises on any character outside ASCII."""
    raw = io.BytesIO()
    stream = io.TextIOWrapper(raw, encoding="ascii", errors="strict", write_through=True)
    return raw, Console(file=stream, width=100, color_system=None, force_terminal=False)


def test_ascii_only_keeps_ascii_and_marks_the_rest():
    assert ascii_only("plain text, 2**8 = 256") == "plain text, 2**8 = 256"
    assert ascii_only("✓ done → next") == "v done -> next"
    assert ascii_only('msg = "naïve café"') == 'msg = "na?ve caf?"'
    assert ascii_only("Ω 中文").isascii()


@pytest.mark.parametrize("level", ["truecolor", "256", "16", "none"])
def test_ascii_mode_never_writes_a_character_the_terminal_cannot_take(level):
    raw, console = _ascii_only_terminal()
    renderer = InteractiveRenderer(console, level, ascii=True)
    # Text the copy doesn't control: the line Python showed for a failed
    # start, and an error message from the user's app.
    for event in (
        Say("app.down.error", {"error": "IndentationError", "location": "app/routes.py:57"}),
        Say("app.down.code", {"code": '    msg = "naïve → café"'}),
        Say("app.down.code", {"code": "        ^^^^"}),
        Say("verify.line", {"line": "  Error: ValueError: no “naïve” values at app/routes.py:9"}),
    ):
        renderer.show(event)
    renderer.wait(Say("wait.protecting"))
    renderer.wait_ended()
    written = raw.getvalue().decode("ascii")
    assert '    msg = "na?ve -> caf?"' in written
    assert "Error: ValueError: no ?na?ve? values at app/routes.py:9" in written
