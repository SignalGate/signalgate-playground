"""Your app's SignalGate client, ready to use.

`sg()` returns the one shared client for this app, created the first time
you use it. `sg_event(...)` turns a request and the envelope your frontend
sent into a SignalGate `Event`, or returns None when the envelope is missing
or malformed, so this pattern never raises:

    ev = sg_event(request, user, envelope, method)
    if ev:
        sg().log(ev)

The client the playground gives you records what your code asks of it, so
the console can check your integration. It behaves exactly like the SDK's
own `Client`.
"""

from __future__ import annotations

import threading
from datetime import datetime, timezone
from typing import Any

from signalgate import ConfigError, EncryptedPayload, Event

from app.kit import facade, settings
from app.kit.facade import RecordingClient, install_client_hook
from app.kit.local_constants import CLIENT_CLOSE_TIMEOUT_S

# Count and record every Client this process builds, from here on.
install_client_hook()

_lock = threading.Lock()
_client: RecordingClient | None = None
# Once True, get_client() never builds a new client again (but keeps
# returning one already built).
_closed = False


def get_client() -> RecordingClient:
    """The shared SignalGate client, created on first use.

    Raises ``signalgate.ConfigError`` when no usable API key is set. After
    ``close_client()`` it never creates a client: it returns the shared one,
    now closed, if one was created, and raises ``signalgate.ConfigError``
    otherwise.
    """
    # In your app: Client(api_key=os.environ["SIGNALGATE_API_KEY"]).
    if _client is not None:
        return _client
    with _lock:
        if _client is not None:
            return _client
        return _build_locked()


def _build_locked() -> RecordingClient:
    global _client
    if _closed:
        raise ConfigError("the SignalGate client is closed")
    key = settings.load_api_key()
    if key is None:
        raise ConfigError("no usable SignalGate API key was found")
    # Call through the module attribute, never a name bound at import time,
    # so a test can swap in a fake builder by patching `facade.build`.
    _client = facade.build(key)
    return _client


def sg() -> RecordingClient:
    """The shared SignalGate client (the same object as ``get_client()``)."""
    return get_client()


def close_client() -> None:
    """Close the shared client, if one was created. Safe to call more than once.

    Once this returns, no shared client can be created any more in this
    boot, and one created earlier, or while this waited for it, is closed.
    It never creates a client. ``client_state()`` and ``metrics_snapshot()``
    keep describing the shared client afterwards (see each).
    """
    global _closed
    with _lock:
        _closed = True
        client = _client
    if client is None:
        return
    # Bypass the facade's recording: the kit's own shutdown is never a
    # user-code close() call, so it leaves no close record.
    client._inner.close(timeout_s=CLIENT_CLOSE_TIMEOUT_S)
    if client._kit is not None:
        client._kit.close()


def sg_event(
    request: Any,
    user: str,
    envelope: Any,
    method: str,
    custom: dict[str, Any] | None = None,
) -> Event | None:
    """Build the ``Event`` for ``user`` from ``request`` and ``envelope``.

    Returns None, without raising, when the envelope is missing or malformed.
    ``v`` is passed through unchanged.
    """
    # In production set forwarded_allow_ips to your proxy's address.
    try:
        if not isinstance(envelope, dict):
            return None
        encrypted = envelope.get("encrypted")
        nonce = envelope.get("nonce")
        timestamp = envelope.get("timestamp")
        if not isinstance(encrypted, str) or not isinstance(nonce, str):
            return None
        if not isinstance(timestamp, int) or isinstance(timestamp, bool):
            return None
        client = request.client
        if client is None:
            return None
        return Event(
            user_id=user,
            ip=client.host,
            method=method,
            timestamp=datetime.now(timezone.utc).isoformat(),
            payload=EncryptedPayload.from_mapping(envelope),
            custom=custom,
        )
    except Exception:  # noqa: BLE001 - this pattern must never raise
        return None


def client_state() -> tuple[int, bool]:
    """``(clients_created, client_ready)``, read without taking any lock.

    ``clients_created`` counts every ``Client`` this process built, the
    shared one included. ``client_ready`` turns True once the shared client
    has been created and stays True after ``close_client()``: it means
    "created in this boot", not "still open". Only ``_reset()`` clears it.
    """
    return (facade.clients_created(), _client is not None)


def metrics_snapshot() -> dict[str, int]:
    """The shared client's counters, flat, in the SDK's own spelling.

    ``{}`` while no shared client exists; a new client with no counters yet
    may give ``{}`` too, so ``client_state()`` is what tells the two apart.
    After ``close_client()`` it gives the closed client's final counters.
    It never creates the client.
    """
    client = _client
    if client is None:
        return {}
    return client.metrics.snapshot_flat()


def key_state() -> str:
    """What the API key sources held at boot: absent, placeholder, invalid or set."""
    return settings.key_state()


def _reset() -> None:
    """Forget the shared client and re-read the key sources (for tests only).

    ``client_state()`` goes back to ``(0, False)`` and the module is open
    again. The key snapshot is read afresh from ``SIGNALGATE_API_KEY`` in
    the file ``settings.ENV_PATH`` names at the time of the call, then in
    the environment, exactly as at import.
    """
    global _client, _closed
    with _lock:
        _client = None
        _closed = False
    facade._reset_clients_created()
    settings._reload()


__all__ = [
    "client_state",
    "close_client",
    "get_client",
    "key_state",
    "metrics_snapshot",
    "sg",
    "sg_event",
]
