"""Final-copy snapshots: a real ``Machine`` driven through every state.

``tests/tui/test_console.py`` already snapshots every ``Say`` key against a
flat fixture list. This module drives the actual state machine instead --
booting, the app coming up, a finished run, a stopped run, every command and
both answers -- so the transcript a reviewer reads here is exactly what the
console would print in that sequence, not just each string in isolation.
``tests.tui.play.Play`` draws it as a terminal shows it: each typed answer
stays on screen after its input line's glyph.

Snapshots live under ``snapshots_final/`` (not ``snapshots/``, which holds
the logo and panel snapshots) and follow the same convention: regenerate only
with ``PLAYGROUND_UPDATE_SNAPSHOTS=1``.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path

import pytest

from playground.machine.events import (
    AppDown,
    AppReady,
    AppReloaded,
    BootFailure,
    ChangeDetected,
    Event,
    RunInvalidated,
    SaveState,
    StateSaved,
)
from playground.machine.machine import Machine
from playground.tui.colour import Level
from tests.machine._scenarios import RUN_FINISHED, RUN_STOPPED
from tests.tui.play import Play

SNAPSHOT_DIR = Path(__file__).parent / "snapshots_final"


def _check_snapshot(name: str, actual: str) -> None:
    if os.environ.get("PLAYGROUND_SKIP_SNAPSHOTS") == "1":
        pytest.skip("snapshot comparison skipped (PLAYGROUND_SKIP_SNAPSHOTS=1)")
    path = SNAPSHOT_DIR / f"{name}.txt"
    if os.environ.get("PLAYGROUND_UPDATE_SNAPSHOTS") == "1":
        SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
        path.write_text(actual)
        return
    assert path.exists(), f"missing snapshot {path}; rerun with PLAYGROUND_UPDATE_SNAPSHOTS=1"
    expected = path.read_text()
    assert actual == expected, f"{name} snapshot mismatch"


def _saves(ok: bool) -> Callable[[Event], Event | None]:
    """The runner's part of the ``SaveState`` / ``StateSaved`` round trip: the
    save's own notice renders before "Bye", never after."""

    def respond(event: Event) -> Event | None:
        return StateSaved(ok) if isinstance(event, SaveState) else None

    return respond


def _drive_finish_then_every_command(play: Play) -> None:
    """Boot -> ready -> run -> finish (20.0%) -> unknown/no/help/hint/status
    -> Enter (asks again) -> yes (chapter 2) -> quit."""
    play.start()
    play.feed(AppReady())
    play.answer("/start")
    play.feed(RUN_FINISHED)
    for line in ("banana", "no", "/help", "/hint", "/status", "", "yes", "/quit"):
        play.answer(line)


def _drive_stopped_run(play: Play) -> None:
    """Boot -> ready -> run -> stopped by the first Ctrl-C -> status -> quit."""
    play.start()
    play.feed(AppReady())
    play.answer("/start")
    play.feed(RUN_STOPPED)
    play.answer("/status")
    play.answer("/quit")


def _drive_resume(play: Play) -> None:
    """A first console reaches the close question and quits with a save.
    A second console boots, is offered that save, and /continue picks it
    back up to the same close question -> yes -> quit."""
    first = Machine()
    first.start()
    first.on_event(AppReady())
    first.handle("/start")
    first.on_event(RUN_FINISHED)

    play.machine = Machine(saved=first.to_dict())
    play.start()
    play.feed(AppReady())
    play.answer("/continue")
    play.answer("yes")
    play.answer("/quit")


def _drive_reload_notices(play: Play) -> None:
    """Boot -> ready -> run -> a change is deferred mid-run -> the run
    finishes -> no (keep the question open) -> the app comes back once from
    the change the run held back (said once, as the run promised) and once
    from a crash respawn -> quit."""
    play.start()
    play.feed(AppReady())
    play.answer("/start")
    play.feed(ChangeDetected())
    play.feed(RUN_FINISHED)
    play.answer("no")
    play.feed(AppReloaded(2, "change"))
    play.feed(AppReloaded(3, "crash"))
    play.answer("/quit")


def _drive_app_down(play: Play) -> None:
    """The sample app never starts (a known failure location) -> /start is
    refused -> fixing it reloads the app -> quit with a failed save."""
    play.start()
    play.feed(AppDown("app/routes.py:9"))
    play.answer("/start")
    play.feed(AppReloaded(1, "change"))
    play.answer("/quit")


# Pasted code with four spaces too many, as Python reports it.
_INDENTED = BootFailure(
    error="IndentationError",
    message="unexpected indent",
    location="app/routes.py:57",
    source='        ev = sg_event(request, body.user, body.signalgate_log, "playground.action")',
    hint="app.down.hint.indent",
)


def _drive_boot_failure(play: Play) -> None:
    """The sample app stops starting after a paste: what's wrong, the line,
    a hint and how to recover -> the fix reloads it -> quit."""
    play.start()
    play.feed(AppReady())
    play.feed(AppDown(_INDENTED.location, _INDENTED))
    play.feed(AppReloaded(2, "change"))
    play.answer("/quit")


def _drive_invalidated_run(play: Play) -> None:
    """Boot -> ready -> run -> the sample app dies mid-run, invalidating the
    run -> /start runs again -> quit."""
    play.start()
    play.feed(AppReady())
    play.answer("/start")
    play.feed(RunInvalidated())
    play.answer("/quit")


_SAVE_FAILS = frozenset({_drive_app_down})


def _transcript(
    monkeypatch: pytest.MonkeyPatch, drive: Callable[[Play], None], level: Level
) -> str:
    """What ``drive`` puts on a 100-column terminal at colour ``level``."""
    respond = _saves(drive not in _SAVE_FAILS)
    play = Play(monkeypatch, level=level, width=100, respond=respond)
    drive(play)
    output = play.output
    lines = output.split("\n")
    for at in range(1, len(lines)):
        assert lines[at] or lines[at - 1], f"two blank lines at {at}"
    return output


def test_finish_and_commands_truecolor(monkeypatch):
    _check_snapshot(
        "finish_and_commands_truecolor",
        _transcript(monkeypatch, _drive_finish_then_every_command, "truecolor"),
    )


def test_finish_and_commands_none_has_no_escape_codes(monkeypatch):
    output = _transcript(monkeypatch, _drive_finish_then_every_command, "none")
    assert "\x1b[" not in output
    _check_snapshot("finish_and_commands_none", output)


def test_stopped_run_truecolor(monkeypatch):
    _check_snapshot(
        "stopped_run_truecolor", _transcript(monkeypatch, _drive_stopped_run, "truecolor")
    )


def test_stopped_run_none_has_no_escape_codes(monkeypatch):
    output = _transcript(monkeypatch, _drive_stopped_run, "none")
    assert "\x1b[" not in output
    _check_snapshot("stopped_run_none", output)


def test_resume_truecolor(monkeypatch):
    _check_snapshot("resume_truecolor", _transcript(monkeypatch, _drive_resume, "truecolor"))


def test_resume_none_has_no_escape_codes(monkeypatch):
    output = _transcript(monkeypatch, _drive_resume, "none")
    assert "\x1b[" not in output
    _check_snapshot("resume_none", output)


def test_reload_notices_truecolor(monkeypatch):
    _check_snapshot(
        "reload_notices_truecolor", _transcript(monkeypatch, _drive_reload_notices, "truecolor")
    )


def test_reload_notices_none_has_no_escape_codes(monkeypatch):
    output = _transcript(monkeypatch, _drive_reload_notices, "none")
    assert "\x1b[" not in output
    _check_snapshot("reload_notices_none", output)


def test_app_down_truecolor(monkeypatch):
    _check_snapshot("app_down_truecolor", _transcript(monkeypatch, _drive_app_down, "truecolor"))


def test_app_down_none_has_no_escape_codes(monkeypatch):
    output = _transcript(monkeypatch, _drive_app_down, "none")
    assert "\x1b[" not in output
    _check_snapshot("app_down_none", output)


def test_invalidated_run_truecolor(monkeypatch):
    _check_snapshot(
        "invalidated_run_truecolor", _transcript(monkeypatch, _drive_invalidated_run, "truecolor")
    )


def test_invalidated_run_none_has_no_escape_codes(monkeypatch):
    output = _transcript(monkeypatch, _drive_invalidated_run, "none")
    assert "\x1b[" not in output
    _check_snapshot("invalidated_run_none", output)


def test_boot_failure_truecolor(monkeypatch):
    _check_snapshot(
        "boot_failure_truecolor", _transcript(monkeypatch, _drive_boot_failure, "truecolor")
    )


def test_boot_failure_none_has_no_escape_codes(monkeypatch):
    output = _transcript(monkeypatch, _drive_boot_failure, "none")
    assert "\x1b[" not in output
    _check_snapshot("boot_failure_none", output)
