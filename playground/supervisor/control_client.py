"""A small async client for the sample app's ``/__playground/*`` control routes.

Every call dials ``127.0.0.1`` directly with the per-boot bearer token,
ignoring any proxy environment variables a developer's shell might have set
(a control call must never depend on, or be interceptable through, a proxy).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import httpx

from playground.traffic.local_constants import LOOPBACK_REQUEST_TIMEOUT_S

HEALTH_PATH = "/__playground/health"
METRICS_PATH = "/__playground/metrics"
LEDGER_PATH = "/__playground/ledger"
CALLS_PATH = "/__playground/calls"
CANARY_PATH = "/__playground/canary"
SHUTDOWN_PATH = "/__playground/shutdown"
INBOX_PATH = "/__playground/inbox"

# The SDK's own counter spellings (exact, from `signalgate._metrics`).
KEY_ENQUEUED = "log_enqueued_total"
KEY_SENT = "log_sent_total"
KEY_DROPPED_RETRY = 'log_dropped_total{reason="retry_exhausted"}'
KEY_DROPPED_QUEUE_FULL = 'log_dropped_total{reason="queue_full"}'
KEY_DROPPED_CLOSED = 'log_dropped_total{reason="closed"}'
_HTTP_ERROR_PREFIX = "log_http_error_total"


class ControlError(RuntimeError):
    """A control route answered, but not with a usable 2xx response."""

    def __init__(self, status: int) -> None:
        super().__init__(f"control route returned status {status}")
        self.status = status


@dataclass(frozen=True)
class Health:
    """``GET /__playground/health``, unpacked."""

    boot_id: str
    sdk: str
    sdk_version: str
    methods: tuple[str, ...]
    clients_created: int
    client_ready: bool
    self_test: Mapping[str, Any]
    key: str


@dataclass(frozen=True)
class LedgerPage:
    """One page of ``GET /__playground/ledger``."""

    entries: list[dict[str, Any]]
    cursor: int
    more: bool


@dataclass(frozen=True)
class CallsPage:
    """One page of ``GET /__playground/calls``."""

    v: int
    boot_id: str
    records: list[dict[str, Any]]
    cursor: int
    more: bool
    oldest_cursor: int
    truncated: bool
    record_errors: int


@dataclass(frozen=True)
class ShutdownReply:
    """``POST /__playground/shutdown``, unpacked."""

    ok: bool
    boot_id: str
    closed: bool
    metrics: dict[str, int] | None


class ControlClient:
    """Talks to one boot's control routes over loopback HTTP."""

    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._client = httpx.AsyncClient(
            base_url=base_url,
            trust_env=False,
            timeout=LOOPBACK_REQUEST_TIMEOUT_S,
            headers={"Authorization": f"Bearer {token}"},
            transport=transport,
        )

    def __repr__(self) -> str:
        # The token lives only inside the httpx client's own headers, which
        # httpx's repr never shows; nothing here echoes it either.
        return f"{type(self).__name__}(base_url={self._client.base_url!r})"

    def _read_json(self, response: httpx.Response) -> Any:
        """``response.json()``, with a bad body turned into ``ControlError``
        (a malformed 2xx body is treated exactly like a non-2xx status)."""
        try:
            return response.json()
        except ValueError as error:
            raise ControlError(response.status_code) from error

    async def health(self) -> Health:
        response = await self._client.get(HEALTH_PATH)
        if response.status_code != 200:
            raise ControlError(response.status_code)
        data = self._read_json(response)
        try:
            return Health(
                boot_id=data["boot_id"],
                sdk=data["sdk"],
                sdk_version=data["sdk_version"],
                methods=tuple(data["methods"]),
                clients_created=data["clients_created"],
                client_ready=data["client_ready"],
                self_test=data["self_test"],
                key=data["key"],
            )
        except (KeyError, TypeError) as error:
            raise ControlError(response.status_code) from error

    async def metrics(self, *, timeout_s: float | None = None) -> dict[str, int]:
        kwargs = {} if timeout_s is None else {"timeout": timeout_s}
        response = await self._client.get(METRICS_PATH, **kwargs)
        if response.status_code != 200:
            raise ControlError(response.status_code)
        data = self._read_json(response)
        if not isinstance(data, dict):
            raise ControlError(response.status_code)
        return data

    async def ledger(
        self,
        *,
        run: str | None = None,
        cursor: int = 0,
        limit: int | None = None,
    ) -> LedgerPage:
        params: dict[str, Any] = {"cursor": cursor}
        if run is not None:
            params["run"] = run
        if limit is not None:
            params["limit"] = limit
        response = await self._client.get(LEDGER_PATH, params=params)
        if response.status_code != 200:
            raise ControlError(response.status_code)
        data = self._read_json(response)
        try:
            return LedgerPage(entries=data["entries"], cursor=data["cursor"], more=data["more"])
        except (KeyError, TypeError) as error:
            raise ControlError(response.status_code) from error

    async def calls(self, *, cursor: int = 0, limit: int | None = None) -> CallsPage:
        params: dict[str, Any] = {"cursor": cursor}
        if limit is not None:
            params["limit"] = limit
        response = await self._client.get(CALLS_PATH, params=params)
        if response.status_code != 200:
            raise ControlError(response.status_code)
        data = self._read_json(response)
        try:
            return CallsPage(
                v=data["v"],
                boot_id=data["boot_id"],
                records=data["records"],
                cursor=data["cursor"],
                more=data["more"],
                oldest_cursor=data["oldest_cursor"],
                truncated=data["truncated"],
                record_errors=data["record_errors"],
            )
        except (KeyError, TypeError) as error:
            raise ControlError(response.status_code) from error

    async def inbox(self, refs: list[str]) -> dict[str, str]:
        """Take the confirmation tokens waiting for ``refs`` (each is read once)."""
        response = await self._client.get(INBOX_PATH, params={"ref": refs})
        if response.status_code != 200:
            raise ControlError(response.status_code)
        data = self._read_json(response)
        try:
            tokens = data["tokens"]
        except (KeyError, TypeError) as error:
            raise ControlError(response.status_code) from error
        if not isinstance(tokens, dict):
            raise ControlError(response.status_code)
        return tokens

    async def canary(self, items: list[Mapping[str, Any]]) -> int:
        response = await self._client.post(CANARY_PATH, json={"canaries": items})
        if response.status_code != 200:
            raise ControlError(response.status_code)
        data = self._read_json(response)
        try:
            return data["registered"]
        except (KeyError, TypeError) as error:
            raise ControlError(response.status_code) from error

    async def shutdown(self, *, timeout_s: float | None = None) -> ShutdownReply | None:
        kwargs = {} if timeout_s is None else {"timeout": timeout_s}
        try:
            response = await self._client.post(SHUTDOWN_PATH, **kwargs)
        except httpx.HTTPError:
            return None
        if response.status_code != 200:
            return None
        try:
            data = response.json()
            return ShutdownReply(
                ok=data["ok"],
                boot_id=data["boot_id"],
                closed=data["closed"],
                metrics=data.get("metrics"),
            )
        except (ValueError, KeyError, TypeError):
            # A malformed 2xx body is a failed shutdown, exactly like a
            # connect error, a timeout or a non-2xx status: the caller (the
            # launcher's stop()) falls back to a signal either way.
            return None

    async def aclose(self) -> None:
        await self._client.aclose()


def counter(snapshot: Mapping[str, int], key: str) -> int:
    """``snapshot[key]``, or 0 when the key is absent (a zero that never fired)."""
    return snapshot.get(key, 0)


def queued(snapshot: Mapping[str, int]) -> int:
    """How many ``log()`` events are still enqueued, never negative."""
    sent_and_dropped = counter(snapshot, KEY_SENT) + counter(snapshot, KEY_DROPPED_RETRY)
    return max(0, counter(snapshot, KEY_ENQUEUED) - sent_and_dropped)


def http_errors_total(snapshot: Mapping[str, int]) -> int:
    """The sum of every ``log_http_error_total{...}`` key, whatever its label."""
    return sum(
        value
        for key, value in snapshot.items()
        if key == _HTTP_ERROR_PREFIX or key.startswith(_HTTP_ERROR_PREFIX + "{")
    )


def delta(before: Mapping[str, int], after: Mapping[str, int]) -> dict[str, int]:
    """Per-key change from ``before`` to ``after``, over the union of keys.

    Never sums across keys: a 4xx that touches two counters gives two
    separate +1 entries, one per key.
    """
    keys = set(before) | set(after)
    return {key: counter(after, key) - counter(before, key) for key in keys}


__all__ = [
    "CALLS_PATH",
    "CANARY_PATH",
    "HEALTH_PATH",
    "INBOX_PATH",
    "KEY_DROPPED_CLOSED",
    "KEY_DROPPED_QUEUE_FULL",
    "KEY_DROPPED_RETRY",
    "KEY_ENQUEUED",
    "KEY_SENT",
    "LEDGER_PATH",
    "METRICS_PATH",
    "SHUTDOWN_PATH",
    "CallsPage",
    "ControlClient",
    "ControlError",
    "Health",
    "LedgerPage",
    "ShutdownReply",
    "counter",
    "delta",
    "http_errors_total",
    "queued",
]
