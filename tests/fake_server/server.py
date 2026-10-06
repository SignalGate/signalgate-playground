"""A test-only fake of the playground API, and of the ``/v0/check`` and ``/v0/log`` behind it.

``FakeServer().app`` is a FastAPI app. Tests reach it in process: the
console side through ``fastapi.testclient.TestClient`` or
``httpx.ASGITransport``, and the sample app's SDK through the requests-layer
stub's forward mode (``tests/support/sdk_stub.forward_to``). It never opens
a socket and never reads the environment.

What it answers follows ``tests/contract/playground_api.py``: every request
body is checked against its spec (400 when it doesn't match), every answer
is built to its spec, and the tests check each one against the committed
contract file.

- **Sessions.** Every API key belongs to a workspace: its own, unless a
  test puts keys together with ``same_workspace``. A workspace has at most
  one open session. A new session from the same key replaces the open one.
  Another key's open session is replaced only once it has gone more than a
  day (``IDLE_REPLACE_AFTER``) without a lease, a telemetry report or a
  completion; a status poll is not activity. Until then the create gets 409
  ``SESSION_ACTIVE``, and its details name the open session's start and key
  prefix. A session id works for every key of its workspace. Any other key
  gets 404, and so does every call once the session is closed (completed,
  deleted or replaced), except a delete, which answers 204 again.
  ``sessions()`` reads them back for a test.
- **Leases.** Visits come from a class-independent visit plan built with the
  public constants: about 2.7 attack visitors per real one, and 74% of real
  visitors succeed (``playground.traffic.plan``). A visit's ref is random
  and new each time. Its envelopes are random bytes, base64-encoded:
  nothing is encrypted, and nothing in them depends on the class. A learning
  visit has no check envelope; a visit that doesn't succeed has no success
  envelope. A probe lease is for the console's own code checks: its visits
  are real users, each with the envelope roles ``probe_roles`` names (roles
  it doesn't know are ignored; all three when it names none), and a success
  envelope makes the visit one that succeeds.
- **Status.** The workflow comes from the ``Script``. ``window`` and
  ``stored_since`` count the logs this fake accepted for the workspace, by
  method.
- **Check and log.** Every leased envelope works once, for the workspace it
  was leased to. A check gets the script's verdict for the visit's class. An
  unknown, altered or already used envelope gets 422 ``REQUEST_REJECTED``.
- **Errors.** ``Script.fail_next(route, ...)`` makes a route's next call
  answer that error. Routes: ``create_session``, ``lease``, ``status``,
  ``telemetry``, ``complete``, ``end_session``, ``check`` and ``log``.
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from types import MappingProxyType
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

from playground.traffic.plan import Visit, build_offline_plan
from tests.contract.playground_api import (
    ACTION_METHOD,
    PHASES,
    PROTOCOL_HEADER,
    PROTOCOL_VERSION,
    RETRY_AFTER,
    ROUTES,
    SESSION_PATH,
    SESSIONS_PATH,
    SUCCESS_METHOD,
    WORKFLOW_NAME,
)
from tests.fake_server.script import Script, ScriptedError

CHECK_PATH = "/v0/check"
LOG_PATH = "/v0/log"

REJECTED = "REQUEST_REJECTED"

# Random bytes per envelope, before base64. The same for every envelope.
ENVELOPE_BYTES = 48
# The envelope's "v": a placeholder. The console and the sample app pass it
# through unchanged and never read it.
ENVELOPE_V = 0

CONSENT_TEXT_VERSION = "consent-test-1"

# Another key's open session gives way to a new one only after more than
# this long without activity (a lease, a telemetry report or a completion).
# It mirrors the API behaviour the console's copy describes: a session
# "closes by itself after a day without activity".
IDLE_REPLACE_AFTER = timedelta(days=1)

# The envelope roles a lease hands out, and which phases get a check envelope.
ROLES = ("action_check", "action_log", "success_log")
_CHECK_PHASES = frozenset({"protect", "readiness", "probe"})


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(moment: datetime) -> str:
    return moment.isoformat()


def _parse_iso(text: str) -> datetime | None:
    try:
        moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo is not None else None


def _request_id(request: Request) -> str:
    return request.headers.get("x-request-id") or f"req-{secrets.token_hex(8)}"


def _error(
    request: Request,
    status: int,
    code: str,
    message: str,
    *,
    details: dict[str, str] | None = None,
    retry_after_s: int | None = None,
) -> JSONResponse:
    """An error in the public API's envelope."""
    request_id = _request_id(request)
    error: dict[str, Any] = {"code": code, "message": message, "request_id": request_id}
    if details is not None:
        error["details"] = details
    headers = {"X-Request-Id": request_id}
    if retry_after_s is not None:
        headers[RETRY_AFTER] = str(retry_after_s)
    return JSONResponse(
        status_code=status,
        content={"ok": False, "data": None, "error": error},
        headers=headers,
    )


def _scripted(request: Request, error: ScriptedError) -> JSONResponse:
    return _error(
        request,
        error.status,
        error.code,
        "scripted error",
        details=dict(error.details) if error.details else None,
        retry_after_s=error.retry_after_s,
    )


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _key(request: Request) -> str | None:
    """Who is calling: a digest of the bearer value, or None without one.

    The value itself is never kept.
    """
    header = request.headers.get("authorization", "")
    scheme, _, value = header.partition(" ")
    if scheme != "Bearer" or not value.strip():
        return None
    return _digest(value)


def _envelope() -> dict[str, Any]:
    return {
        "encrypted": base64.b64encode(secrets.token_bytes(ENVELOPE_BYTES)).decode("ascii"),
        "timestamp": int(time.time()),
        "nonce": secrets.token_urlsafe(12),
        "v": ENVELOPE_V,
    }


@dataclass
class _Session:
    sid: str
    key: str
    workspace: str
    size: int
    started_at: datetime
    last_activity: datetime
    open: bool = True
    # Why it closed: "replaced", "idle", "completed" or "deleted".
    ended_by: str | None = None
    refs: set[str] = field(default_factory=set)
    cursors: dict[tuple[str, int], int] = field(default_factory=dict)

    def close(self, why: str) -> None:
        self.open = False
        self.ended_by = why

    def leased(self) -> dict[str, int]:
        """Visits leased so far, per phase, over every round."""
        totals = dict.fromkeys(PHASES, 0)
        for (phase, _round), count in self.cursors.items():
            totals[phase] += count
        return totals


@dataclass(frozen=True)
class SessionView:
    """One session as a test reads it back (``FakeServer.sessions()``).

    ``ended_by`` says why a closed session closed: ``"replaced"`` (a new
    session from the same key), ``"idle"`` (another key's new session, after
    a day without activity), ``"completed"`` or ``"deleted"``; it is None
    while the session is open. ``leased`` counts the visits leased per phase,
    over every round.
    """

    sid: str
    workspace: str
    open: bool
    ended_by: str | None
    started_at: datetime
    last_activity: datetime
    leased: Mapping[str, int]


@dataclass
class _Minted:
    workspace: str
    sid: str
    ref: str
    cls: str
    role: str
    envelope: dict[str, Any]
    used: bool = False


@dataclass(frozen=True)
class Delivery:
    """One check or log the fake received, and what it answered.

    ``tenant`` is the caller's workspace, as a digest.
    """

    endpoint: str
    tenant: str
    method: Any
    nonce: Any
    ref: str | None
    cls: str | None
    role: str | None
    status: int
    verdict: str | None
    at: datetime


class FakeServer:
    """The fake's state, and the FastAPI app that serves it (``app``)."""

    def __init__(
        self,
        script: Script | None = None,
        *,
        seed: int = 0,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.script = script if script is not None else Script()
        self.seed = seed
        # When a session starts and when it was last active, as an aware
        # datetime. A test moves it to age a session; the server time a new
        # session reports, lease expiry and delivery times keep the real clock.
        self.clock = clock if clock is not None else _now
        self._lock = threading.Lock()
        self._workspaces: dict[str, str] = {}
        self._sessions: dict[str, _Session] = {}
        self._minted: dict[str, _Minted] = {}
        self._plans: dict[tuple[str, int], list[Visit]] = {}
        self.deliveries: list[Delivery] = []
        self.telemetry: list[dict[str, Any]] = []
        self.completions: list[dict[str, Any]] = []
        self.app = self._build_app()

    # --- what tests read back ------------------------------------------------------

    def delivered(self, endpoint: str | None = None) -> list[Delivery]:
        """The checks and logs received so far, for one endpoint or both."""
        with self._lock:
            return [d for d in self.deliveries if endpoint in (None, d.endpoint)]

    def sessions(self) -> list[SessionView]:
        """Every session opened so far, open or closed, in the order they were opened."""
        with self._lock:
            return [
                SessionView(
                    sid=session.sid,
                    workspace=session.workspace,
                    open=session.open,
                    ended_by=session.ended_by,
                    started_at=session.started_at,
                    last_activity=session.last_activity,
                    leased=MappingProxyType(session.leased()),
                )
                for session in self._sessions.values()
            ]

    # --- how a test sets it up ------------------------------------------------------

    def same_workspace(self, *keys: str) -> None:
        """Put these API keys in one workspace, the first key's.

        Every key is its own workspace until then. Only a digest of each key
        is kept. Call it before the keys open sessions: a session stays in
        the workspace it was opened in.
        """
        digests = [_digest(key) for key in keys]
        if not digests:
            return
        with self._lock:
            workspace = self._workspace_of(digests[0])
            for digest in digests:
                self._workspaces[digest] = workspace

    # --- shared checks --------------------------------------------------------------

    def _workspace_of(self, key: str) -> str:
        """The workspace a key digest belongs to. Call it holding ``self._lock``."""
        return self._workspaces.get(key, key)

    def _plan(self, phase: str, size: int) -> list[Visit]:
        # The same plan for every session of this size: which visit comes i-th
        # in a phase never depends on who asks.
        key = (phase, size)
        if key not in self._plans:
            self._plans[key] = build_offline_plan(size, seed=self.seed * 10 + PHASES.index(phase))
        return self._plans[key]

    async def _body(self, request: Request, route: str) -> Any:
        """The request's JSON body if it matches the route's spec; else a 400 response."""
        try:
            body = json.loads(await request.body() or b"null")
        except ValueError:
            return _error(request, 400, REJECTED, "the body is not JSON")
        problems = ROUTES[route].request.check(body, "$")
        if problems:
            return _error(request, 400, REJECTED, "the body does not match the contract")
        return body

    def _guard(
        self, request: Request, route: str, sid: str | None = None, *, allow_closed: bool = False
    ) -> tuple[str, str, _Session | None] | JSONResponse:
        """Protocol, key, session and scripted errors, in that order.

        Passed, it gives the caller's key digest, its workspace and the session.
        """
        if request.headers.get(PROTOCOL_HEADER) != PROTOCOL_VERSION:
            return _error(request, 426, REJECTED, "this protocol version is not supported")
        key = _key(request)
        if key is None:
            return _error(request, 401, REJECTED, "an API key is required")
        session = None
        with self._lock:
            workspace = self._workspace_of(key)
            if sid is not None:
                session = self._sessions.get(sid)
        if sid is not None and (
            session is None or session.workspace != workspace or not (session.open or allow_closed)
        ):
            return _error(request, 404, REJECTED, "no such session")
        scripted = self.script.take_error(route)
        if scripted is not None:
            return _scripted(request, scripted)
        return key, workspace, session

    def _gives_way(self, session: _Session, key: str, now: datetime) -> bool:
        """Whether a new session from ``key`` replaces this open one."""
        return session.key == key or now - session.last_activity > IDLE_REPLACE_AFTER

    # --- the playground API ----------------------------------------------------------

    async def create_session(self, request: Request) -> Response:
        guarded = self._guard(request, "create_session")
        if isinstance(guarded, Response):
            return guarded
        key, workspace, _ = guarded
        body = await self._body(request, "create_session")
        if isinstance(body, Response):
            return body
        limits = dict(self.script.limits)
        if not 1 <= body["size"] <= limits["max_size"]:
            return _error(request, 400, REJECTED, "size is out of range")
        with self._lock:
            now = self.clock()
            open_ones = [s for s in self._sessions.values() if s.open and s.workspace == workspace]
            blocking = next((s for s in open_ones if not self._gives_way(s, key, now)), None)
            if blocking is None:
                for old in open_ones:
                    old.close("replaced" if old.key == key else "idle")
                session = _Session(
                    sid=secrets.token_hex(16),
                    key=key,
                    workspace=workspace,
                    size=body["size"],
                    started_at=now,
                    last_activity=now,
                )
                self._sessions[session.sid] = session
        if blocking is not None:
            return _error(
                request,
                409,
                "SESSION_ACTIVE",
                "another key's session is open in this workspace",
                details={
                    "started_at": _iso(blocking.started_at),
                    "key_prefix": f"key-{blocking.key[:6]}",
                },
            )
        return JSONResponse(
            status_code=201,
            content={
                "session_id": session.sid,
                "methods": {"action": ACTION_METHOD, "target": SUCCESS_METHOD},
                "workflow_name": WORKFLOW_NAME,
                "plan_version": self.script.plan_version,
                "consent_text_version": CONSENT_TEXT_VERSION,
                "limits": limits,
                "server_time": _iso(_now()),
            },
        )

    def _wire_visit(
        self, session: _Session, visit: Visit, phase: str, probe_roles: list[str] | None = None
    ) -> dict[str, Any]:
        ref = secrets.token_hex(8)
        while ref in session.refs:
            ref = secrets.token_hex(8)
        session.refs.add(ref)
        if phase == "probe":
            named = {role for role in probe_roles or () if role in ROLES}
            wanted = named | {"action_log"} if named else set(ROLES)
            roles = [role for role in ROLES if role in wanted]
            visit = replace(visit, cls="real", success="success_log" in wanted)
        else:
            roles = ["action_log"]
            if phase in _CHECK_PHASES:
                roles.insert(0, "action_check")
            if visit.success:
                roles.append("success_log")
        envelopes = {}
        for role in roles:
            envelope = _envelope()
            envelopes[role] = envelope
            self._minted[envelope["nonce"]] = _Minted(
                session.workspace, session.sid, ref, visit.cls, role, envelope
            )
        return {
            "ref": ref,
            "class": visit.cls,
            "user_id": visit.user_id,
            "client_ip": visit.client_ip,
            "script": {"success": visit.success, "success_after_ms": visit.success_after_ms},
            "envelopes": envelopes,
        }

    async def lease(self, request: Request) -> Response:
        sid = request.path_params["sid"]
        guarded = self._guard(request, "lease", sid)
        if isinstance(guarded, Response):
            return guarded
        _, _, session = guarded
        body = await self._body(request, "lease")
        if isinstance(body, Response):
            return body
        if not 1 <= body["count"] <= self.script.limits["lease_max"]:
            return _error(request, 400, REJECTED, "count is out of range")
        phase = body["phase"]
        with self._lock:
            # A new session may have replaced this one while the body was read.
            if not session.open:
                return _error(request, 404, REJECTED, "no such session")
            session.last_activity = self.clock()
            plan = self._plan(phase, session.size)
            cursor_key = (phase, body.get("round", 0))
            start = session.cursors.get(cursor_key, 0)
            session.cursors[cursor_key] = start + body["count"]
            visits = [
                self._wire_visit(
                    session, plan[(start + offset) % len(plan)], phase, body.get("probe_roles")
                )
                for offset in range(body["count"])
            ]
        expires_at = _now() + timedelta(seconds=self.script.limits["use_within_s"])
        return JSONResponse(
            content={"seq": body["seq"], "expires_at": _iso(expires_at), "visits": visits}
        )

    def _stored(self, workspace: str, since: datetime | None) -> dict[str, int]:
        counts = {ACTION_METHOD: 0, SUCCESS_METHOD: 0}
        with self._lock:
            for delivery in self.deliveries:
                if delivery.endpoint != "log" or delivery.status != 200:
                    continue
                if delivery.tenant != workspace or delivery.method not in counts:
                    continue
                if since is None or delivery.at >= since:
                    counts[delivery.method] += 1
        return counts

    async def status(self, request: Request) -> Response:
        sid = request.path_params["sid"]
        guarded = self._guard(request, "status", sid)
        if isinstance(guarded, Response):
            return guarded
        _, workspace, _ = guarded
        since_text = request.query_params.get("since")
        since = None
        if since_text is not None:
            since = _parse_iso(since_text)
            if since is None:
                return _error(request, 400, REJECTED, "since is not an RFC 3339 time")
        window = self._stored(workspace, None)
        stored_since = None
        if since is not None:
            counts = self._stored(workspace, since)
            stored_since = {"action": counts[ACTION_METHOD], "success": counts[SUCCESS_METHOD]}
        return JSONResponse(
            content={
                "workflow": self.script.polled(window[ACTION_METHOD]),
                "window": {
                    "action_received": window[ACTION_METHOD],
                    "success_received": window[SUCCESS_METHOD],
                },
                "stored_since": stored_since,
            }
        )

    async def telemetry_report(self, request: Request) -> Response:
        sid = request.path_params["sid"]
        guarded = self._guard(request, "telemetry", sid)
        if isinstance(guarded, Response):
            return guarded
        _, _, session = guarded
        body = await self._body(request, "telemetry")
        if isinstance(body, Response):
            return body
        with self._lock:
            if not session.open:
                return _error(request, 404, REJECTED, "no such session")
            session.last_activity = self.clock()
            self.telemetry.append({"session_id": sid, **body})
        return Response(status_code=204)

    async def complete(self, request: Request) -> Response:
        sid = request.path_params["sid"]
        guarded = self._guard(request, "complete", sid)
        if isinstance(guarded, Response):
            return guarded
        _, _, session = guarded
        body = await self._body(request, "complete")
        if isinstance(body, Response):
            return body
        if not body["keep_workflow"]:
            self.script.pause()
        with self._lock:
            if not session.open:
                return _error(request, 404, REJECTED, "no such session")
            self.completions.append({"session_id": sid, **body})
            session.last_activity = self.clock()
            session.close("completed")
        return Response(status_code=204)

    async def end_session(self, request: Request) -> Response:
        sid = request.path_params["sid"]
        guarded = self._guard(request, "end_session", sid, allow_closed=True)
        if isinstance(guarded, Response):
            return guarded
        _, _, session = guarded
        with self._lock:
            if session.open:
                session.close("deleted")
        return Response(status_code=204)

    # --- check and log ---------------------------------------------------------------

    async def _data_plane(self, request: Request, endpoint: str) -> Response:
        key = _key(request)
        if key is None:
            return _error(request, 401, REJECTED, "an API key is required")
        scripted = self.script.take_error(endpoint)
        if scripted is not None:
            return _scripted(request, scripted)
        try:
            body = json.loads(await request.body() or b"null")
        except ValueError:
            body = None
        body = body if isinstance(body, dict) else {}
        payload = body.get("payload")
        payload = payload if isinstance(payload, dict) else {}
        nonce = payload.get("nonce")
        with self._lock:
            workspace = self._workspace_of(key)
            minted = self._minted.get(nonce) if isinstance(nonce, str) else None
            usable = (
                minted is not None
                and minted.workspace == workspace
                and not minted.used
                and payload.get("encrypted") == minted.envelope["encrypted"]
                and payload.get("timestamp") == minted.envelope["timestamp"]
            )
            if usable:
                minted.used = True
        verdict = self.script.verdict(minted.cls) if usable and endpoint == "check" else None
        status = 200 if usable else 422
        with self._lock:
            self.deliveries.append(
                Delivery(
                    endpoint=endpoint,
                    tenant=workspace,
                    method=body.get("method"),
                    nonce=nonce,
                    ref=minted.ref if minted else None,
                    cls=minted.cls if minted else None,
                    role=minted.role if minted else None,
                    status=status,
                    verdict=verdict,
                    at=_now(),
                )
            )
        if not usable:
            return _error(request, 422, REJECTED, "the envelope was not accepted")
        request_id = _request_id(request)
        data = None
        if endpoint == "check":
            data = {"action": verdict, "request_id": request_id, "processing_time_us": 0}
        return JSONResponse(
            content={"ok": True, "data": data, "error": None},
            headers={"X-Request-Id": request_id},
        )

    async def check(self, request: Request) -> Response:
        return await self._data_plane(request, "check")

    async def log(self, request: Request) -> Response:
        return await self._data_plane(request, "log")

    # --- wiring ---------------------------------------------------------------------------

    def _build_app(self) -> FastAPI:
        app = FastAPI(openapi_url=None, docs_url=None, redoc_url=None)
        app.add_api_route(SESSIONS_PATH, self.create_session, methods=["POST"])
        app.add_api_route(SESSION_PATH + "/leases", self.lease, methods=["POST"])
        app.add_api_route(SESSION_PATH, self.status, methods=["GET"])
        app.add_api_route(SESSION_PATH + "/telemetry", self.telemetry_report, methods=["POST"])
        app.add_api_route(SESSION_PATH + "/complete", self.complete, methods=["POST"])
        app.add_api_route(SESSION_PATH, self.end_session, methods=["DELETE"])
        app.add_api_route(CHECK_PATH, self.check, methods=["POST"])
        app.add_api_route(LOG_PATH, self.log, methods=["POST"])
        return app


__all__ = [
    "CHECK_PATH",
    "ENVELOPE_BYTES",
    "IDLE_REPLACE_AFTER",
    "LOG_PATH",
    "REJECTED",
    "ROLES",
    "Delivery",
    "FakeServer",
    "SessionView",
]
