"""Which kind of block each copy entry is, so a renderer can space and style it.

The console groups what it prints into blocks: a chapter heading, an
explanation, a "Your turn" task, a line of code, a result, an achievement, a
dim note, the question right before the input line, and the completion
card. ``block_of(key)`` answers that for a copy key; ``Say`` fills its
``block`` from here unless the machine sets one itself.

This module imports nothing from ``playground.machine``: the machine's
events import it, so the question keys are listed here literally, and a
test checks them against the machine's own question table.
"""

from __future__ import annotations

from typing import Literal

Block = Literal["heading", "info", "task", "code", "result", "achievement", "note", "ask", "card"]

# The questions right before the input line.
_ASK = frozenset(
    {
        "ch2.key.ask",
        "ch2.consent",
        "ch2.session.retry",
        "ch3.ask",
        "ch3.again",
        "ch3.learn.again",
        "ch3.topup",
        "ch4.ask",
        "ch4.start",
        "ch5.wait.again",
        "ch5.ask",
        "ch5.again",
        "ch5.readiness.again",
        "ch5.round.again",
        "ch6.ask",
        "ch6.rerun.offer",
        "ch6.rerun.later",
        "ch6.rerun.anyway",
        "wf.paused",
        "ch6.keep",
        "ch6.done",
        "ch1.close",
        "ch1.stopped",
        "run.again",
        "continue.offer",
    }
)

_EXACT: dict[str, Block] = {
    "chapter.heading": "heading",
    "ch3.task": "task",
    "ch5.task": "task",
    "app.down.code": "code",
    # results
    "verify.line": "result",
    "ch2.key.ok": "result",
    "ch2.key.missing": "result",
    "ch2.key.public_key": "result",
    "ch2.key.format": "result",
    "ch3.done": "result",
    "ch4.prediction": "result",
    "ch5.live": "result",
    "ch5.done": "result",
    "ch5.readiness.live": "result",
    "ch5.readiness.weak": "result",
    "ch5.readiness.over_blocking": "result",
    "ch5.readiness.diagnose": "result",
    "ch5.readiness.no_checks": "result",
    "ch5.tripwire": "result",
    "run.none": "result",
    "run.degraded": "result",
    "run.stopped": "result",
    "run.invalidated": "result",
    "app.crashed": "result",
    "app.failed": "result",
    "app.down": "result",
    "app.down_at": "result",
    "app.down.error": "result",
    "drain.skipped": "result",
    "drain.gave_up": "result",
    "drain.done": "result",
    "ch6.kept": "result",
    "ch6.paused": "result",
    "ch6.complete_failed": "result",
    # notes
    "ch3.lesson": "note",
    "ch4.lesson": "note",
    "ch4.warning": "note",
    "ch4.why_block": "note",
    "ch5.lesson": "note",
    "app.reloaded": "note",
    "app.restarted": "note",
    "app.restarted_key": "note",
    "reload.deferred": "note",
    "app.down.fix": "note",
    "reply.no": "note",
    "reply.busy": "note",
    "console.saved": "note",
    "console.interrupt_again": "note",
}

# Checked in order, after the questions and the exact keys above.
_PREFIXES: tuple[tuple[str, Block], ...] = (
    ("achievement.", "achievement"),
    ("complete.card", "card"),
    ("ch2.git.", "result"),
    ("ch2.session.", "result"),
    ("ch2.clock.", "result"),
    ("results.", "result"),
    ("stage.", "result"),
    ("online.", "result"),
    ("wf.", "result"),
    ("copy.", "result"),
    ("report.", "result"),
    ("drain.note", "note"),
    ("app.down.hint.", "note"),
    ("state.", "note"),
)


def block_of(key: str) -> Block:
    """The block the copy entry ``key`` belongs to; ``"info"`` when nothing else fits."""
    if key in _ASK:
        return "ask"
    exact = _EXACT.get(key)
    if exact is not None:
        return exact
    for prefix, block in _PREFIXES:
        if key.startswith(prefix):
            return block
    return "info"


__all__ = ["Block", "block_of"]
