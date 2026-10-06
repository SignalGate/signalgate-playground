"""The interface both console renderers implement.

The runner drives either renderer through this Protocol only; it never
branches on which one it holds. Reading interactive input is not part of
it: the interactive console reads through ``playground.tui.prompt``, while
the ``--ci`` renderer reads through its own ``read_line`` (see
``playground.tui.jsonl``), since only one of the two needs slash-command
completion.

Optional methods a renderer may offer, for the wait while the sample app's
SDK sends its queued log events (the runner falls back when one is missing):

- ``drain_begin(first: Say, *, inline: bool = True)``: the wait starts, and
  ``first`` is its first line (``drain.waiting`` or ``drain.waiting_quit``).
  ``inline`` is False while the input line is open, so nothing may be
  redrawn in place. Without it, the runner shows ``first`` as any ``Say``.
- ``drain_progress(event: DeliverProgress)``: how far the wait has got.
  Without it, nothing is shown.
- ``drain_end()``: the wait is over; the runner shows its last line next.
  Without it, nothing happens.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from playground.machine.events import Event, RunProgress


@runtime_checkable
class Renderer(Protocol):
    """What the runner needs from any renderer."""

    def show(self, event: Event) -> None:
        """Render one machine output event (``Say``, ``ShowLogo``, ...)."""
        ...

    def run_started(self, total: int) -> None:
        """A chapter run of ``total`` visitors is starting.

        A run through SignalGate also passes ``title=<copy key>``, and a
        renderer may offer ``wait(say)`` / ``wait_ended()`` for the spinner
        line shown while the console waits for SignalGate.
        """
        ...

    def progress(self, event: RunProgress) -> None:
        """Live counters during a run."""
        ...

    def run_ended(self) -> None:
        """The run's live display, if any, is done."""
        ...


__all__ = ["Renderer"]
