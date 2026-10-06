"""The recording client behind `sg()`, and the hook that counts every `Client`.

`RecordingClient` wraps one SDK `Client` and forwards exactly the members in
`FORWARDED`. Each call returns the SDK's own value, or raises the SDK's own
exception, and leaves one record in `app.kit.calls` on the way. Recording
never changes what the call does.

`install_client_hook()` wraps `signalgate.Client.__init__` so every client
built in this process, by the kit or by your own code, is counted and
recorded. It only counts and records; it never changes the client.

The probes at the bottom are read-only. The boot self-test uses them to
check that the facade and the hook are in place; none of them builds a
client or reads the key.
"""

from __future__ import annotations

import contextlib
import functools
import threading
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from typing import Any

import signalgate

from app.kit import calls as calls_module
from app.kit.calls import bind_call, capture, j
from app.kit.kit_transport import KitTransport
from app.kit.sequence import next_seq

# The attribute that marks the wrapper installed over `signalgate.Client.__init__`.
HOOK_MARKER = "_playground_client_hook"

# The members of `signalgate.Client` that `RecordingClient` forwards.
FORWARDED = ("metrics", "log", "check", "close", "__enter__", "__exit__")

# `close`/`enter`/`exit` take no event, so every event-shaped field is null.
_NO_EVENT_FIELDS: dict[str, Any] = {
    "arg_type": None,
    "payload_type": None,
    "method": None,
    "user_id": None,
    "ip": None,
    "timestamp": None,
    "nonce": None,
}


def _safe(read: Callable[[], Any], default: Any) -> Any:
    """``read()``, or ``default`` when it raises. Recording must never break the caller."""
    try:
        return read()
    except Exception:  # noqa: BLE001 - recording must never break the caller
        return default


def _field(source: Any, name: str) -> Any:
    """Read ``name`` off ``source``, whichever shape it is: a mapping or an object.

    User code may call ``check``/``log`` with a real ``Event``, a plain dict,
    or anything else the transparency tests choose to pass; a record still
    shows what it can about it.
    """
    if isinstance(source, Mapping):
        return source.get(name)
    return getattr(source, name, None)


def _event_fields(event: Any) -> dict[str, Any]:
    """The record fields a ``check``/``log`` argument supplies, read defensively."""
    payload = _safe(lambda: _field(event, "payload"), None)
    payload_type = _safe(lambda: type(payload).__name__, None) if payload is not None else None
    raw_nonce = _safe(lambda: _field(payload, "nonce"), None) if payload is not None else None
    return {
        "arg_type": _safe(lambda: type(event).__name__, None),
        "payload_type": payload_type,
        "method": _safe(lambda: j(_field(event, "method")), None),
        "user_id": _safe(lambda: j(_field(event, "user_id")), None),
        "ip": _safe(lambda: j(_field(event, "ip")), None),
        "timestamp": _safe(lambda: j(_field(event, "timestamp")), None),
        "nonce": raw_nonce if isinstance(raw_nonce, str) else None,
    }


def _status_or_none(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _code_or_none(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def _error_fields(exc: BaseException) -> dict[str, Any]:
    """``{type, status, code}`` for a call record. Never ``str(exc)``: an SDK
    or transport exception's text can embed request details.

    Every read is guarded, because this runs inside the ``except`` block
    just before the bare ``raise``: an exception attribute that raises when
    read leaves its field null instead of replacing the caller's exception.
    """
    return {
        "type": _safe(lambda: type(exc).__name__, "Exception"),
        "status": _safe(lambda: _status_or_none(getattr(exc, "status_code", None)), None),
        "code": _safe(lambda: _code_or_none(getattr(exc, "code", None)), None),
    }


def _store(record: Mapping[str, Any]) -> None:
    """Hand ``record`` to the call log. A failure to record never reaches the caller."""
    with contextlib.suppress(Exception):
        calls_module.calls.append(record)


def _append_call(
    op: str,
    seq: int,
    captured: Mapping[str, Any],
    event_fields: Mapping[str, Any],
    *,
    t0: float | None,
    t1: float | None,
    outcome: str,
    action: str | None,
    processing_time_us: int | None,
    log_job: int | None,
    error: Mapping[str, Any] | None,
) -> None:
    record = {
        "kind": "call",
        "op": op,
        "seq": seq,
        "ref": captured["ref"],
        "run": captured["run"],
        "site": captured["site"],
        "thread": captured["thread"],
        "on_loop": captured["on_loop"],
        **event_fields,
        "t0": t0,
        "t1": t1,
        "outcome": outcome,
        "action": action,
        "processing_time_us": processing_time_us,
        "log_job": log_job,
        "error": dict(error) if error is not None else None,
    }
    _store(record)


class RecordingClient:
    """A transparent, recording stand-in for one ``signalgate.Client``."""

    def __init__(self, inner: signalgate.Client, kit: signalgate.Transport | None = None) -> None:
        self._inner = inner
        self._kit = kit
        # Serialises `log()` so its `log_job` reading of `log_enqueued_total`
        # before and after one enqueue is never crossed by another thread's.
        self._log_lock = threading.Lock()

    @property
    def metrics(self) -> signalgate.Metrics:
        return self._inner.metrics

    def log(self, event: signalgate.Event) -> None:
        seq = next_seq()
        captured = capture()
        outcome = "raised"
        log_job: int | None = None
        error: dict[str, Any] | None = None
        try:
            with self._log_lock:
                before = self._inner.metrics.get("log_enqueued_total")
                self._inner.log(event)
                after = self._inner.metrics.get("log_enqueued_total")
            if after == before + 1:
                log_job = after
                outcome = "enqueued"
            else:
                outcome = "not_enqueued"
        except Exception as exc:
            error = _error_fields(exc)
            raise
        finally:
            _append_call(
                "log",
                seq,
                captured,
                _event_fields(event),
                t0=None,
                t1=None,
                outcome=outcome,
                action=None,
                processing_time_us=None,
                log_job=log_job,
                error=error,
            )

    def check(self, event: signalgate.Event) -> signalgate.CheckResult:
        """Forward to the SDK's ``check``; the record's ``t0`` and ``t1`` are
        ``time.perf_counter()`` readings taken around it."""
        seq = next_seq()
        captured = capture()
        outcome = "raised"
        action: str | None = None
        processing_time_us: int | None = None
        error: dict[str, Any] | None = None
        t0 = time.perf_counter()
        t1: float | None = None
        try:
            with bind_call(seq):
                result = self._inner.check(event)
            t1 = time.perf_counter()
            outcome = "failed_open" if result.failed_open else "verdict"
            action = result.action
            processing_time_us = result.processing_time_us
            return result
        except Exception as exc:
            t1 = time.perf_counter()
            error = _error_fields(exc)
            raise
        finally:
            _append_call(
                "check",
                seq,
                captured,
                _event_fields(event),
                t0=t0,
                t1=t1,
                outcome=outcome,
                action=action,
                processing_time_us=processing_time_us,
                log_job=None,
                error=error,
            )

    def close(self, timeout_s: float | None = None) -> None:
        seq = next_seq()
        captured = capture()
        outcome = "raised"
        error: dict[str, Any] | None = None
        try:
            self._inner.close(timeout_s=timeout_s)
            outcome = "returned"
        except Exception as exc:
            error = _error_fields(exc)
            raise
        finally:
            _append_call(
                "close",
                seq,
                captured,
                _NO_EVENT_FIELDS,
                t0=None,
                t1=None,
                outcome=outcome,
                action=None,
                processing_time_us=None,
                log_job=None,
                error=error,
            )

    def __enter__(self) -> RecordingClient:  # noqa: PYI034 - typing.Self needs Python 3.11
        seq = next_seq()
        captured = capture()
        outcome = "raised"
        error: dict[str, Any] | None = None
        try:
            self._inner.__enter__()
            outcome = "returned"
            return self
        except Exception as exc:
            error = _error_fields(exc)
            raise
        finally:
            _append_call(
                "enter",
                seq,
                captured,
                _NO_EVENT_FIELDS,
                t0=None,
                t1=None,
                outcome=outcome,
                action=None,
                processing_time_us=None,
                log_job=None,
                error=error,
            )

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        seq = next_seq()
        captured = capture()
        outcome = "raised"
        error: dict[str, Any] | None = None
        try:
            self._inner.__exit__(exc_type, exc, tb)
            outcome = "returned"
        except Exception as raised:
            error = _error_fields(raised)
            raise
        finally:
            _append_call(
                "exit",
                seq,
                captured,
                _NO_EVENT_FIELDS,
                t0=None,
                t1=None,
                outcome=outcome,
                action=None,
                processing_time_us=None,
                log_job=None,
                error=error,
            )

    def __repr__(self) -> str:
        return "RecordingClient(api_key=<redacted>)"


def build(key: str) -> RecordingClient:
    """Build ``Client(key, http=KitTransport(key))`` with the SDK's defaults, wrapped."""
    kit = KitTransport(key)
    try:
        with _marking_singleton():
            inner = signalgate.Client(key, http=kit)
    except Exception:
        kit.close()
        raise
    return RecordingClient(inner, kit=kit)


# --- the construction hook ----------------------------------------------------

_construction_lock = threading.Lock()
_clients_created = 0
_singleton_flag = threading.local()


@contextmanager
def _marking_singleton() -> Iterator[None]:
    """Mark every ``signalgate.Client`` built on this thread, for the duration
    of the block, as the one behind ``sg()`` (only ``build()`` uses this)."""
    previous = getattr(_singleton_flag, "active", False)
    _singleton_flag.active = True
    try:
        yield
    finally:
        _singleton_flag.active = previous


def _is_marked_singleton() -> bool:
    return getattr(_singleton_flag, "active", False)


def _record_construction(instance: signalgate.Client) -> None:
    global _clients_created
    seq = next_seq()
    captured = capture()
    with _construction_lock:
        _clients_created += 1
    transport = getattr(instance, "_transport", None)
    record = {
        "kind": "construct",
        "seq": seq,
        "ref": captured["ref"],
        "run": captured["run"],
        "site": captured["site"],
        "thread": captured["thread"],
        "singleton": _is_marked_singleton(),
        "kit_transport": isinstance(transport, KitTransport),
    }
    _store(record)


def clients_created() -> int:
    """How many ``signalgate.Client`` instances this process has ever built.

    A lock-free read of a plain module global: the health route and
    ``signalgate_client.client_state()`` call this without ever taking a
    lock or touching the event loop.
    """
    return _clients_created


def _reset_clients_created() -> None:
    """Zero the construction counter (tests only, via ``signalgate_client._reset()``)."""
    global _clients_created
    with _construction_lock:
        _clients_created = 0


def install_client_hook() -> None:
    """Wrap ``signalgate.Client.__init__`` to count and record every client. Idempotent."""
    if client_hook_installed():
        return
    original = signalgate.Client.__init__

    @functools.wraps(original)
    def wrapper(self: signalgate.Client, *args: Any, **kwargs: Any) -> None:
        original(self, *args, **kwargs)
        _record_construction(self)

    setattr(wrapper, HOOK_MARKER, True)
    signalgate.Client.__init__ = wrapper


# --- read-only probes (the boot self-test uses these) ------------------------


def client_hook_installed() -> bool:
    """Whether ``signalgate.Client.__init__`` is the playground's counting wrapper."""
    return getattr(signalgate.Client.__init__, HOOK_MARKER, False) is True


def missing_members() -> tuple[str, ...]:
    """The public ``Client`` members, plus ``__enter__``/``__exit__``, that
    ``RecordingClient`` doesn't define itself, sorted. Empty when it covers
    the installed SDK."""
    public = {name for name in dir(signalgate.Client) if not name.startswith("_")}
    wanted = public | {"__enter__", "__exit__"}
    return tuple(sorted(name for name in wanted if name not in RecordingClient.__dict__))


def is_recording_client(obj: Any) -> bool:
    """Whether ``obj`` is a ``RecordingClient``."""
    return isinstance(obj, RecordingClient)


__all__ = [
    "FORWARDED",
    "HOOK_MARKER",
    "RecordingClient",
    "build",
    "client_hook_installed",
    "clients_created",
    "install_client_hook",
    "is_recording_client",
    "missing_members",
]
