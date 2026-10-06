"""The verifier's evidence, read back from the sample app's control routes.

Nothing here talks to SignalGate. Everything the verifier knows comes from
two places: what the console itself sent (which envelope went into which
field, for which user and address), and what the sample app recorded while
it handled those requests:

- ``/calls``: every SDK call the app's code made (``kind: call``), every
  attempt the kit transport sent or answered locally, with SignalGate's own
  reply (``kind: attempt``), and every client built (``kind: construct``);
- ``/ledger``: the actions, inbox deliveries and confirms, in order;
- ``/metrics``: the SDK's counters, read as a delta over a window;
- ``/health``: which boot of the app answered, and how many clients it built.

A window starts with ``mark()`` and ends with ``collect()``. Records are
joined by the request's ``ref`` and ``run``, a check call to its attempts by
``call_seq``, a log call to its attempts by ``log_job``, and every call to
what the console sent by the envelope's nonce.
"""

from __future__ import annotations

import secrets
from collections.abc import AsyncIterator, Iterable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from playground.supervisor.control_client import ControlClient, Health, delta
from playground.traffic.local_constants import LOOPBACK_REQUEST_TIMEOUT_S
from playground.verify.model import Site

Record = Mapping[str, Any]

# How one log or check job ended, from its attempts.
ACCEPTED = "accepted"  # SignalGate answered 2xx
DUPLICATE = "duplicate"  # a 422 on a retry after a timed-out attempt
REJECTED = "rejected"  # SignalGate answered 4xx
LOCAL = "local"  # answered on the laptop only (a canary or the method guard)
UNREACHED = "unreached"  # no answer from SignalGate: a timeout, a network error, a 5xx


class EvidenceUnavailable(Exception):
    """The window can't be read as one piece; ``reason`` says why."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class AppLink:
    """How the verifier reaches the running sample app on your laptop."""

    base_url: str
    token: str = field(repr=False)
    transport: httpx.AsyncBaseTransport | None = None


@asynccontextmanager
async def connect(link: AppLink) -> AsyncIterator[tuple[httpx.AsyncClient, ControlClient]]:
    """A visitor-side HTTP client and a control client for ``link``, closed afterwards."""
    control = ControlClient(link.base_url, link.token, transport=link.transport)
    visitor = httpx.AsyncClient(
        base_url=link.base_url,
        trust_env=False,
        timeout=LOOPBACK_REQUEST_TIMEOUT_S,
        transport=link.transport,
    )
    try:
        yield visitor, control
    finally:
        await visitor.aclose()
        await control.aclose()


def new_run_id(prefix: str) -> str:
    """A fresh run id for ``X-Playground-Run``, so the ledger can be read per run."""
    return f"{prefix}-{secrets.token_hex(6)}"


@dataclass(frozen=True)
class Mark:
    """Where a window of evidence starts."""

    boot_id: str
    cursor: int
    metrics: dict[str, int]
    clients_created: int


async def _end_of_calls(control: ControlClient) -> int:
    cursor = 0
    while True:
        page = await control.calls(cursor=cursor)
        cursor = page.cursor
        if not page.more:
            return cursor


async def mark(control: ControlClient, health: Health | None = None) -> Mark:
    """Start a window now: the app's boot, the end of its records and its counters."""
    health = health or await control.health()
    cursor = await _end_of_calls(control)
    metrics = await control.metrics()
    return Mark(health.boot_id, cursor, dict(metrics), health.clients_created)


@dataclass(frozen=True)
class Evidence:
    """Everything the app recorded in one window, for one run."""

    boot_id: str
    records: tuple[Record, ...]
    ledger: tuple[Record, ...]
    metrics: dict[str, int]
    clients_created: int

    def calls(self, op: str | None = None, *, ref: str | None = None) -> list[Record]:
        """The SDK calls the app's code made, optionally one ``op`` and one ``ref``."""
        return [
            r
            for r in self.records
            if r.get("kind") == "call" and op in (None, r.get("op")) and ref in (None, r.get("ref"))
        ]

    def constructs(self) -> list[Record]:
        return [r for r in self.records if r.get("kind") == "construct"]

    def entries(self, kind: str, *, ref: str | None = None) -> list[Record]:
        """This run's ledger entries of one ``kind``, optionally for one ``ref``."""
        return [e for e in self.ledger if e.get("kind") == kind and ref in (None, e.get("ref"))]

    def attempts(self, call: Record) -> list[Record]:
        """The attempts one check or log call made, in order."""
        if call.get("op") == "check":
            key, value, endpoint = "call_seq", call.get("seq"), "check"
        elif call.get("op") == "log" and call.get("log_job") is not None:
            key, value, endpoint = "log_job", call.get("log_job"), "log"
        else:
            return []
        found = [
            r
            for r in self.records
            if r.get("kind") == "attempt" and r.get("endpoint") == endpoint and r.get(key) == value
        ]
        return sorted(found, key=lambda r: r.get("attempt", 0))

    def outcome(self, call: Record) -> str:
        """How one check or log call ended at SignalGate (see ``job_outcome``)."""
        return job_outcome(self.attempts(call))

    def verdict(self, call: Record) -> str | None:
        """The verdict SignalGate's reply carried for a check call, if any."""
        for attempt in self.attempts(call):
            if attempt.get("verdict") is not None:
                return attempt["verdict"]
        return call.get("action")


def _status(attempt: Record) -> int | None:
    status = attempt.get("status")
    return status if isinstance(status, int) and not isinstance(status, bool) else None


def probable_duplicate(attempts: Iterable[Record]) -> bool:
    """A 422 on a retry whose earlier attempt, with the same key, timed out."""
    timed_out = False
    for attempt in sorted(attempts, key=lambda a: a.get("attempt", 0)):
        if timed_out and _status(attempt) == 422:
            return True
        if attempt.get("timed_out"):
            timed_out = True
    return False


def job_outcome(attempts: list[Record]) -> str:
    """``ACCEPTED``, ``DUPLICATE``, ``REJECTED``, ``LOCAL`` or ``UNREACHED``."""
    if not attempts:
        return UNREACHED
    remote = [a for a in attempts if a.get("local") is None]
    if not remote:
        return LOCAL
    if any((_status(a) or 0) // 100 == 2 for a in remote):
        return ACCEPTED
    if probable_duplicate(remote):
        return DUPLICATE
    if any(400 <= (_status(a) or 0) < 500 for a in remote):
        return REJECTED
    return UNREACHED


def rejected_status(attempts: list[Record]) -> int | None:
    """The last 4xx SignalGate answered for one job, if any."""
    statuses = [_status(a) for a in attempts if a.get("local") is None]
    rejected = [s for s in statuses if s is not None and 400 <= s < 500]
    return rejected[-1] if rejected else None


async def collect(control: ControlClient, start: Mark, *, run: str) -> Evidence:
    """Read everything recorded since ``start``, and ``run``'s ledger entries.

    Raises ``EvidenceUnavailable("restarted")`` when a different boot of the
    app answers, and ``EvidenceUnavailable("records")`` when records of the
    window were dropped before they could be read.
    """
    health = await control.health()
    if health.boot_id != start.boot_id:
        raise EvidenceUnavailable("restarted")
    records: list[Record] = []
    cursor = start.cursor
    while True:
        page = await control.calls(cursor=cursor)
        if page.boot_id != start.boot_id:
            raise EvidenceUnavailable("restarted")
        if page.truncated:
            raise EvidenceUnavailable("records")
        records.extend(page.records)
        cursor = page.cursor
        if not page.more:
            break
    ledger: list[Record] = []
    cursor = 0
    while True:
        ledger_page = await control.ledger(run=run, cursor=cursor)
        ledger.extend(ledger_page.entries)
        cursor = ledger_page.cursor
        if not ledger_page.more:
            break
    metrics = await control.metrics()
    return Evidence(
        boot_id=health.boot_id,
        records=tuple(records),
        ledger=tuple(ledger),
        metrics=delta(start.metrics, metrics),
        clients_created=health.clients_created - start.clients_created,
    )


class Sources:
    """Reads the source lines findings point at, under the repo root ``root``."""

    def __init__(self, root: Path) -> None:
        self._root = root
        self._lines: dict[str, list[str]] = {}
        self._paths: dict[str, Path] = {}

    def relative(self, path: Path) -> str:
        """How findings name ``path``: relative to the root, or just its name outside it."""
        try:
            name = path.resolve().relative_to(self._root.resolve()).as_posix()
        except ValueError:
            name = path.name
        self._paths[name] = path
        return name

    def site(self, file: str, line: int) -> Site:
        if file not in self._lines:
            try:
                path = self._paths.get(file, self._root / file)
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                text = ""
            self._lines[file] = text.splitlines()
        lines = self._lines[file]
        code = lines[line - 1].strip() if 0 < line <= len(lines) else ""
        return Site(file, line, code)

    def of(self, record: Record | None) -> Site | None:
        """The site a call or construct record points at, or None."""
        where = record.get("site") if record else None
        if not isinstance(where, Mapping):
            return None
        file, line = where.get("file"), where.get("line")
        if not isinstance(file, str) or not isinstance(line, int):
            return None
        return self.site(file, line)


__all__ = [
    "ACCEPTED",
    "DUPLICATE",
    "LOCAL",
    "REJECTED",
    "UNREACHED",
    "AppLink",
    "Evidence",
    "EvidenceUnavailable",
    "Mark",
    "Record",
    "Sources",
    "collect",
    "connect",
    "job_outcome",
    "mark",
    "new_run_id",
    "probable_duplicate",
    "rejected_status",
]
