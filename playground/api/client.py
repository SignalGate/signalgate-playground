"""The console's client for the playground API at ``https://api.signalgate.ai``.

Every request carries the API key as a bearer value and the header
``SignalGate-Playground-Protocol: 1``. A server that no longer speaks this
protocol answers 426 (``ProtocolOutdated``): the console then asks for a
``git pull``. Answers go through the whitelisting decoders in
``playground.api.decode``, so a field the console doesn't know is dropped
before anything else sees it.

The base address is fixed. A test hands the client its own ``transport``
instead; nothing else can point it anywhere.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import datetime
from typing import Any

import httpx

from playground import CLIENT_VERSION
from playground.api import decode
from playground.traffic.local_constants import API_REQUEST_TIMEOUT_S

BASE_URL = "https://api.signalgate.ai"
PROTOCOL_HEADER = "SignalGate-Playground-Protocol"
PROTOCOL_VERSION = "1"
SESSIONS_PATH = "/v0/playground/sessions"
RETRY_AFTER = "Retry-After"
# The version of the consent text the console shows before it opens a session.
CONSENT_VERSION = "2"


class ApiError(Exception):
    """The API answered with an error: ``status``, its ``code`` and any ``details``."""

    def __init__(
        self,
        status: int,
        code: str | None = None,
        details: Mapping[str, str] | None = None,
        retry_after_s: float | None = None,
    ) -> None:
        super().__init__(f"playground API answered {status} {code or ''}".strip())
        self.status = status
        self.code = code
        self.details = dict(details or {})
        self.retry_after_s = retry_after_s


class ProtocolOutdated(ApiError):
    """The API speaks another protocol version (426): this checkout is out of date."""


class ApiUnreachable(Exception):
    """No usable answer: a network error, a timeout, a 5xx or an unreadable body."""


def _retry_after(response: httpx.Response) -> float | None:
    raw = response.headers.get(RETRY_AFTER)
    try:
        value = float(raw) if raw is not None else None
    except ValueError:
        return None
    return value if value is not None and value >= 0 else None


class PlaygroundApi:
    """One API key's view of the playground API."""

    def __init__(
        self,
        key: str,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        client_version: str = CLIENT_VERSION,
    ) -> None:
        self.client_version = client_version
        self._client = httpx.AsyncClient(
            base_url=BASE_URL,
            timeout=API_REQUEST_TIMEOUT_S,
            transport=transport,
            headers={
                PROTOCOL_HEADER: PROTOCOL_VERSION,
                "Authorization": f"Bearer {key}",
                "User-Agent": f"signalgate-playground/{client_version}",
            },
        )

    def __repr__(self) -> str:
        return f"{type(self).__name__}(base_url={BASE_URL!r})"

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> PlaygroundApi:  # noqa: PYI034
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.aclose()

    async def _send(
        self,
        method: str,
        path: str,
        *,
        json: Any = None,
        params: Mapping[str, str] | None = None,
        ok: int = 200,
    ) -> httpx.Response:
        try:
            response = await self._client.request(method, path, json=json, params=params)
        except httpx.HTTPError as error:
            raise ApiUnreachable(type(error).__name__) from error
        if response.status_code == ok:
            return response
        if response.status_code >= 500:
            raise ApiUnreachable(str(response.status_code))
        try:
            body = response.json()
        except ValueError:
            body = None
        code, details = decode.error(body)
        error_class = ProtocolOutdated if response.status_code == 426 else ApiError
        raise error_class(response.status_code, code, details, _retry_after(response))

    @staticmethod
    def _json(response: httpx.Response) -> Any:
        try:
            return response.json()
        except ValueError as error:
            raise ApiUnreachable("unreadable answer") from error

    async def create_session(self, size: int, consent_version: str) -> dict[str, Any]:
        body = {
            "size": size,
            "client_version": self.client_version,
            "consent_version": consent_version,
        }
        response = await self._send("POST", SESSIONS_PATH, json=body, ok=201)
        try:
            return decode.session(self._json(response))
        except decode.DecodeError as error:
            raise ApiUnreachable("unreadable session") from error

    async def lease(
        self,
        session_id: str,
        *,
        phase: str,
        count: int,
        seq: int,
        round: int | None = None,
        probe_roles: Iterable[str] | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"phase": phase, "count": count, "seq": seq}
        if round is not None:
            body["round"] = round
        if probe_roles is not None:
            body["probe_roles"] = list(probe_roles)
        response = await self._send("POST", f"{SESSIONS_PATH}/{session_id}/leases", json=body)
        try:
            return decode.lease(self._json(response))
        except decode.DecodeError as error:
            raise ApiUnreachable("unreadable lease") from error

    async def status(self, session_id: str, since: datetime | None = None) -> dict[str, Any]:
        params = {"since": since.isoformat()} if since is not None else None
        response = await self._send("GET", f"{SESSIONS_PATH}/{session_id}", params=params)
        try:
            return decode.status(self._json(response))
        except decode.DecodeError as error:
            raise ApiUnreachable("unreadable status") from error

    async def telemetry(self, session_id: str, chapter: int, finding_ids: Iterable[str]) -> None:
        body = {"chapter": chapter, "finding_ids": list(finding_ids)}
        await self._send("POST", f"{SESSIONS_PATH}/{session_id}/telemetry", json=body, ok=204)

    async def complete(
        self,
        session_id: str,
        *,
        keep_workflow: bool,
        results: Mapping[str, float] | None = None,
    ) -> None:
        body: dict[str, Any] = {"keep_workflow": keep_workflow}
        if results is not None:
            body["results"] = dict(results)
        await self._send("POST", f"{SESSIONS_PATH}/{session_id}/complete", json=body, ok=204)

    async def end_session(self, session_id: str) -> None:
        await self._send("DELETE", f"{SESSIONS_PATH}/{session_id}", ok=204)


__all__ = [
    "BASE_URL",
    "CONSENT_VERSION",
    "PROTOCOL_HEADER",
    "PROTOCOL_VERSION",
    "SESSIONS_PATH",
    "ApiError",
    "ApiUnreachable",
    "PlaygroundApi",
    "ProtocolOutdated",
]
