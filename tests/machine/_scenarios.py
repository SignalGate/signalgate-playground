"""Shared scenario builders for the machine test suite.

Not a test module itself (pytest only collects ``test_*.py``): every other
module under ``tests/machine`` reaches every state the machine can be in by
composing these builders, so the matrix, the AST guard and the copy-render
check all agree on what a "state" is.
"""

from __future__ import annotations

from playground.copy.en import text
from playground.machine.events import AppDown, AppReady, Event, RunFinished, RunStopped, Say
from playground.machine.machine import Machine

# The chapter 1 numbers a finished run reports, matching the public /demo
# constants (2.7 attack visitors per real one, 74% of real visitors succeed).
TOTAL = 10_000
ACTIONS = 10_000
SUCCESSES = 2_000
ATTACK = 7_297
REAL = 2_703
RUN_FINISHED = RunFinished(
    total=TOTAL, actions=ACTIONS, successes=SUCCESSES, attack=ATTACK, real=REAL
)
EXPECTED_CONVERSION = SUCCESSES / ACTIONS
EXPECTED_ATTACK_IN_TEN = round(10 * ATTACK / TOTAL)

# A run that completed with not one action going through (the sample app
# stopped answering the instant chapter 1 started).
ZERO_ACTION_RUN = RunFinished(total=TOTAL, actions=0, successes=0, attack=0, real=0)

# A run stopped partway through.
STOPPED_DONE = 4_321
STOPPED_ACTIONS = 4_321
STOPPED_SUCCESSES = 864
RUN_STOPPED = RunStopped(
    done=STOPPED_DONE, total=TOTAL, actions=STOPPED_ACTIONS, successes=STOPPED_SUCCESSES
)

CHAPTER_1_TITLE = text("chapter.1")

GARBAGE_INPUTS = ("banana", "  what now  ", "/nope")
BLANK_INPUTS = ("", "   ")


def say(key: str, **params: object) -> Say:
    """Build the ``Say`` event ``Machine`` would build: key plus its params."""
    return Say(key, params)


def boot_machine() -> Machine:
    """A freshly constructed machine: the sample app hasn't answered yet."""
    return Machine()


def opening_machine() -> Machine:
    """After ``AppReady``: awaiting ``/start``."""
    machine = boot_machine()
    machine.on_event(AppReady())
    return machine


def running_machine() -> Machine:
    """After ``/start``: chapter 1 is sending its offline visitors."""
    machine = opening_machine()
    machine.handle("/start")
    return machine


def close_machine() -> Machine:
    """After ``RunFinished``: awaiting the yes/no close question."""
    machine = running_machine()
    machine.on_event(RUN_FINISHED)
    return machine


def end_machine() -> Machine:
    """After answering ``yes`` at the close: chapter 2 asks for the API key."""
    machine = close_machine()
    machine.handle("yes")
    return machine


def stopped_machine() -> Machine:
    """After ``RunStopped``: back to awaiting ``/start``, mid-content."""
    machine = running_machine()
    machine.on_event(RUN_STOPPED)
    return machine


def app_down_machine() -> Machine:
    """After the opening, the sample app goes down: still awaiting ``/start``,
    but ``/start`` now answers ``app.not_ready`` instead of running chapter 1."""
    machine = opening_machine()
    machine.on_event(AppDown())
    return machine


def offer_pending_machine() -> Machine:
    """A fresh console whose ``AppReady`` finds a resumable saved session
    (a close-pending snapshot): awaiting ``/start``, offering ``/continue``."""
    saved = close_machine().to_dict()
    machine = Machine(saved=saved)
    machine.on_event(AppReady())
    return machine


def offer_running_machine() -> Machine:
    """A fresh console offered a snapshot saved mid-run."""
    saved = running_machine().to_dict()
    machine = Machine(saved=saved)
    machine.on_event(AppReady())
    return machine


def offer_end_machine() -> Machine:
    """A fresh console offered a snapshot saved where chapter 2 asks for the key."""
    saved = end_machine().to_dict()
    machine = Machine(saved=saved)
    machine.on_event(AppReady())
    return machine


def offer_start_chapter_done_machine() -> Machine:
    """A fresh console offered a snapshot with ``awaiting == "start"`` and
    chapter 1 already marked done.

    Ordinary play never saves this combination (once chapter 1 is done,
    ``awaiting`` moves to ``"any"``), but ``from_dict`` validates each field
    on its own and accepts it, so the fourth ``/continue`` branch needs a
    snapshot built directly rather than reached by driving the machine.
    """
    saved = {
        **Machine().to_dict(),
        "offline_total": TOTAL,
        "running": False,
        "awaiting": "start",
        "chapter1_done": True,
        "last_conversion": EXPECTED_CONVERSION,
        "last_attack_in_ten": EXPECTED_ATTACK_IN_TEN,
    }
    machine = Machine(saved=saved)
    machine.on_event(AppReady())
    return machine


def continued_from_offer_pending_machine() -> Machine:
    """After ``/continue`` picks up a close-pending offer: awaiting yes/no."""
    machine = offer_pending_machine()
    machine.handle("/continue")
    return machine


def continued_from_offer_running_machine() -> Machine:
    """After ``/continue`` picks up a mid-run offer: back at the opening."""
    machine = offer_running_machine()
    machine.handle("/continue")
    return machine


def continued_from_offer_end_machine() -> Machine:
    """After ``/continue`` picks up a chapter 2 offer: the key question again."""
    machine = offer_end_machine()
    machine.handle("/continue")
    return machine


def continued_from_offer_start_chapter_done_machine() -> Machine:
    """After ``/continue`` picks up a start/chapter1-done offer: the opening."""
    machine = offer_start_chapter_done_machine()
    machine.handle("/continue")
    return machine


def unattended_running_machine() -> Machine:
    """An unattended (``--yes``) machine mid-run, for the zero-action turn reply."""
    machine = Machine(unattended=True)
    machine.on_event(AppReady())
    machine.handle("/start")
    return machine


STATE_BUILDERS = {
    "boot": boot_machine,
    "opening": opening_machine,
    "running": running_machine,
    "close": close_machine,
    "end": end_machine,
    "stopped": stopped_machine,
    "app_down": app_down_machine,
    "offer_pending": offer_pending_machine,
}


def assert_say_renders(events: list[Event]) -> None:
    """Every ``Say`` in ``events`` must resolve through ``text()`` with no error."""
    for event in events:
        if isinstance(event, Say):
            text(event.key, **event.params)
