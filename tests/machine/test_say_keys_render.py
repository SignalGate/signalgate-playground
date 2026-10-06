"""Every ``Say`` the machine can emit must resolve through ``en.text()``.

Drives the machine through every state x input combination the state matrix
covers, collects every ``Say`` event it produces along the way, and asserts
each one's key exists in ``en.TEXT`` and formats without error with the exact
params the machine passed.
"""

from __future__ import annotations

from playground.copy.en import text
from playground.machine.events import (
    COMMANDS,
    AppDown,
    AppFailed,
    AppReady,
    AppReloaded,
    BootFailure,
    ChangeDetected,
    Event,
    RunFinished,
    RunInvalidated,
    Say,
    StateSaved,
)
from playground.machine.machine import Machine
from tests.machine._scenarios import (
    BLANK_INPUTS,
    GARBAGE_INPUTS,
    RUN_FINISHED,
    RUN_STOPPED,
    STATE_BUILDERS,
    offer_end_machine,
    offer_pending_machine,
    offer_running_machine,
    offer_start_chapter_done_machine,
    running_machine,
)

_PROBE_INPUTS = (*COMMANDS, "yes", "no", "/copy 2", "/copy 9", *GARBAGE_INPUTS, *BLANK_INPUTS)

# A start that failed, with every detail the console can show.
_FAILURE = BootFailure(
    error="IndentationError",
    message="unexpected indent",
    location="app/routes.py:7",
    source="        return {'ok': 2**8}",
    caret="        ^",
    hint="app.down.hint.indent",
)


def _say_events_in(events: list[Event]) -> list[Say]:
    return [event for event in events if isinstance(event, Say)]


def _collect_say_events() -> list[Say]:
    say_events: list[Say] = []

    say_events += _say_events_in(Machine().start())
    say_events += _say_events_in(Machine().on_event(AppFailed("app.failed")))
    say_events += _say_events_in(Machine().on_event(AppReady()))

    for build in STATE_BUILDERS.values():
        for probe in _PROBE_INPUTS:
            say_events += _say_events_in(build().handle(probe))

    say_events += _say_events_in(STATE_BUILDERS["running"]().on_event(RUN_FINISHED))
    say_events += _say_events_in(STATE_BUILDERS["running"]().on_event(RUN_STOPPED))

    # The notices and the zero-action / resume turn replies: not reached by
    # typed input, so the matrix above never sees their Say events.
    say_events += _say_events_in(
        running_machine().on_event(
            RunFinished(total=10_000, actions=0, successes=0, attack=1, real=1)
        )
    )
    say_events += _say_events_in(running_machine().on_event(RunInvalidated()))
    say_events += _say_events_in(running_machine().on_event(ChangeDetected()))
    say_events += _say_events_in(Machine().on_event(AppReloaded(1, "change")))
    say_events += _say_events_in(Machine().on_event(AppReloaded(1, "crash")))
    say_events += _say_events_in(Machine().on_event(AppDown()))
    say_events += _say_events_in(Machine().on_event(AppDown("app/routes.py:1")))
    say_events += _say_events_in(Machine(unattended=True).on_event(AppDown()))
    say_events += _say_events_in(Machine().on_event(AppDown(_FAILURE.location, _FAILURE)))
    say_events += _say_events_in(STATE_BUILDERS["app_down"]().on_event(AppReloaded(2, "change")))
    say_events += _say_events_in(Machine().on_event(AppReloaded(1, "request")))
    say_events += _say_events_in(STATE_BUILDERS["close"]().on_event(StateSaved(True)))
    say_events += _say_events_in(STATE_BUILDERS["boot"]().on_event(StateSaved(False)))
    say_events += _say_events_in(offer_pending_machine().handle("/continue"))
    say_events += _say_events_in(offer_running_machine().handle("/continue"))
    say_events += _say_events_in(offer_end_machine().handle("/continue"))
    say_events += _say_events_in(offer_start_chapter_done_machine().handle("/continue"))

    return say_events


def test_every_emitted_say_key_exists_and_formats():
    say_events = _collect_say_events()
    assert say_events, "the scenarios above must exercise at least one Say event"
    for event in say_events:
        # Raises KeyError for an unknown key, or a formatting error for a
        # param mismatch; either failure means the machine and en.py drifted.
        text(event.key, **event.params)
