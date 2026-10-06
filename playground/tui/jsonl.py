"""The ``--ci`` renderer: one JSON object per line, no logo, no colour.

Every record carries ``"v": 1``. A ``say`` record also names the block the
line belongs to (``"block"``; see ``playground.copy.blocks``), and its text is
plain, without bold marks. While the console waits for the sample app's
queued log events it prints the first line of that wait as a ``say``, then
``progress`` records with ``"phase": "deliver"`` (a run's own ``progress``
records have no ``phase``).
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from collections.abc import Callable
from typing import Any, Protocol

from playground.copy.blocks import block_of
from playground.copy.en import text
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
from playground.traffic.local_constants import DELIVER_REPORT_EVERY_S

I4_VERSION = 1


class _Readable(Protocol):
    def readline(self) -> str: ...


class _Writable(Protocol):
    def write(self, data: str) -> object: ...
    def flush(self) -> object: ...


class JsonLinesRenderer:
    """Emits JSON lines to ``out`` and reads answers from ``inp``."""

    def __init__(
        self, out: _Writable, inp: _Readable, *, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self.out = out
        self.inp = inp
        self._quiet = False
        self._answers: asyncio.Queue[str] | None = None
        self._eof = False
        self._clock = clock
        # The wait for the app's queued log events: when its last deliver
        # record went out, and a newer count held back since.
        self._delivered_at: float | None = None
        self._deliver_held: DeliverProgress | None = None

    def _emit(self, payload: dict[str, Any]) -> None:
        if self._quiet:
            return
        try:
            self.out.write(json.dumps(payload, separators=(",", ":")) + "\n")
            self.out.flush()
        except OSError:
            # A hung-up terminal or closed pipe: go quiet from here on.
            self._quiet = True

    def _say(self, key: str, rendered: str, block: str | None) -> None:
        payload: dict[str, Any] = {"v": I4_VERSION, "type": "say", "key": key, "text": rendered}
        if block is not None:
            payload["block"] = block
        self._emit(payload)

    def show(self, event: Event) -> None:
        if isinstance(event, Say):
            self._say(event.key, text(event.key, **dict(event.params)), event.block)
        elif isinstance(event, ShowLogo):
            pass  # the JSON-lines output has no logo
        elif isinstance(event, AwaitInput):
            self._emit({"v": I4_VERSION, "type": "await", "expect": event.expect})
        elif isinstance(event, Exit):
            self._emit({"v": I4_VERSION, "type": "exit", "code": event.code})
        elif isinstance(event, AppReady):
            self._say("app.ready", text("app.ready"), block_of("app.ready"))
        elif isinstance(event, AppFailed):
            self._say(event.reason, text(event.reason), block_of(event.reason))
        else:
            raise TypeError(f"JsonLinesRenderer cannot show {event!r}")

    def run_started(self, total: int, *, title: str | None = None) -> None:
        pass  # progress lines already carry the total; nothing to announce

    def progress(self, event: RunProgress) -> None:
        # Throttling how often this is called is the caller's job.
        payload: dict[str, Any] = {
            "v": I4_VERSION,
            "type": "progress",
            "done": event.done,
            "total": event.total,
            "actions": event.actions,
            "successes": event.successes,
        }
        for name in ("posted", "delivered", "accepted", "stored"):
            value = getattr(event, name)
            if value is not None:
                payload[name] = value
        self._emit(payload)

    def run_ended(self) -> None:
        pass

    # -- waiting for the app's queued log events ------------------------------------

    def drain_begin(self, first: Say, *, inline: bool = True) -> None:
        """The wait starts: its first line, as a ``say`` record."""
        self._delivered_at = None
        self._deliver_held = None
        self.show(first)

    def drain_progress(self, event: DeliverProgress) -> None:
        """How far the wait has got: a deliver record now, or held back.

        The first count goes out at once; after that, at most one record per
        ``DELIVER_REPORT_EVERY_S``. A count held back goes out with the next
        record, or at ``drain_end``.
        """
        now = self._clock()
        if self._delivered_at is not None and now - self._delivered_at < DELIVER_REPORT_EVERY_S:
            self._deliver_held = event
            return
        self._deliver_held = None
        self._delivered_at = now
        self._deliver(event)

    def drain_end(self) -> None:
        """The wait is over: a count still held back goes out now."""
        held, self._deliver_held = self._deliver_held, None
        self._delivered_at = None
        if held is not None:
            self._deliver(held)

    def _deliver(self, event: DeliverProgress) -> None:
        self._emit(
            {
                "v": I4_VERSION,
                "type": "progress",
                "phase": "deliver",
                "done": event.done,
                "total": event.total,
            }
        )

    def wait(self, event: Say) -> None:
        """What the console is waiting for (a spinner line on a terminal)."""
        rendered = text(event.key, **dict(event.params))
        self._emit({"v": I4_VERSION, "type": "wait", "key": event.key, "text": rendered})

    def wait_ended(self) -> None:
        pass

    def _start_reader(self) -> asyncio.Queue[str]:
        """Start the one background reader thread, lazily, on the first read.

        A blocking ``inp.readline()`` must never sit in the loop's default
        executor: on shutdown, ``asyncio.run`` waits for that executor
        to drain, so a pending read there can hang the process. Instead, one
        daemon thread loops the blocking reads itself and hands lines to the
        loop with ``call_soon_threadsafe``; it is never joined, so the
        process still exits promptly while the read stays pending (for
        example on an open pipe with no more input). ``read_line`` then just
        awaits the queue, which loses nothing across a cancelled read: an
        item already queued, or an in-flight wakeup, is still there for the
        next call.
        """
        queue: asyncio.Queue[str] = asyncio.Queue()
        self._answers = queue
        loop = asyncio.get_running_loop()

        def push(item: str) -> None:
            try:
                loop.call_soon_threadsafe(queue.put_nowait, item)
            except RuntimeError:
                # The loop already closed; nobody is left to read this.
                pass

        def pump() -> None:
            try:
                while True:
                    line = self.inp.readline()
                    if line == "":
                        push("")
                        return
                    push(line)
            except Exception:  # noqa: BLE001 - any read failure means EOF, never a hang
                # A hung-up pipe, a decode error on bad bytes, and so on, is
                # treated as EOF: a broken reader means quit, never a hang,
                # so ``read_line`` is never left waiting forever.
                push("")

        threading.Thread(target=pump, name="jsonl-stdin-reader", daemon=True).start()
        return queue

    async def read_line(self) -> str:
        """Read one answer line from ``inp``; EOF answers ``/quit``."""
        if self._eof:
            return "/quit"
        answers = self._answers if self._answers is not None else self._start_reader()
        line = await answers.get()
        if line == "":
            self._eof = True
            return "/quit"
        return line.rstrip("\n")


__all__ = ["JsonLinesRenderer"]
