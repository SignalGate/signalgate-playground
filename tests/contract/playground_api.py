"""Specs for the playground API: every request the console sends and every answer it may get.

The API lives under ``/v0/playground`` and takes the same API key as the
rest of the public API. Every request carries the protocol header
``SignalGate-Playground-Protocol: 1``; a server that speaks another protocol
answers 426. Successful answers are the bodies below; errors use the public
API's error envelope, ``{ok: false, data: null, error: {code, message,
request_id}}``, and an error that carries fields puts them in
``error.details``. Every object is closed: a key not listed here is a
contract break.

``ROUTES`` lists every route with its request and its answers by status.
Besides the statuses a route lists for itself, any route may answer 400 (a
request that doesn't match its spec), 401 (a missing or unknown API key)
and 426 (another protocol), and every route under a session id may answer
404 (no such session for this key).

Numbers the server sets for the client, such as how long a leased envelope
stays usable, come in the session's ``limits`` at run time. Nothing here
pins their values.
"""

from __future__ import annotations

from typing import Any

from tests.contract.json_schema import HeaderSet, Response, Route
from tests.contract.samples import BOOL, FLOAT, INT, NULL, STR, Enum, ListOf, Obj, Opt, Spec

PROTOCOL_HEADER = "SignalGate-Playground-Protocol"
PROTOCOL_VERSION = "1"
RETRY_AFTER = "Retry-After"

ACTION_METHOD = "playground.action"
SUCCESS_METHOD = "playground.success"
WORKFLOW_NAME = "playground"

# A JSON number: the server may send a whole number without a fraction.
NUMBER = INT | FLOAT

# The headers this contract defines. Transport headers (Content-Type,
# User-Agent and the like) are not part of it.
HEADERS = Obj({PROTOCOL_HEADER: Enum(PROTOCOL_VERSION), "Authorization": STR})

# --- the error envelope ------------------------------------------------------


def error_envelope(code: Spec = STR, details: Spec | None = None) -> Obj:
    """The public API's error envelope, with ``code`` and, if given, ``details``."""
    fields: dict[str, Any] = {"code": code, "message": STR, "request_id": STR}
    if details is not None:
        fields["details"] = details
    return Obj({"ok": Enum(False), "data": NULL, "error": fields})


ERROR = error_envelope()
SESSION_ACTIVE = error_envelope(Enum("SESSION_ACTIVE"), Obj({"started_at": STR, "key_prefix": STR}))
NO_ACTIVE_PUBLIC_KEY = error_envelope(Enum("NO_ACTIVE_PUBLIC_KEY"))
LEASE_CONFLICT = error_envelope(Enum("KEY_ROTATED", "PLAN_CHANGED"))
SLOW_DOWN = error_envelope(Enum("SLOW_DOWN"))

# --- sessions ------------------------------------------------------------------

SESSION_REQUEST = Obj({"size": INT, "client_version": STR, "consent_version": STR})

LIMITS = Obj(
    {
        "max_size": INT,
        "lease_max": INT,
        "use_within_s": NUMBER,
        "max_queue_age_s": NUMBER,
        "clock_warn_s": NUMBER,
        "clock_block_s": NUMBER,
        "readiness_retry_s": NUMBER,
        "readiness_max_attempts": INT,
        "ingest_wait_s": NUMBER,
        "protect_diagnose_after_s": NUMBER,
    }
)

SESSION = Obj(
    {
        "session_id": STR,
        "methods": {"action": Enum(ACTION_METHOD), "target": Enum(SUCCESS_METHOD)},
        "workflow_name": Enum(WORKFLOW_NAME),
        "plan_version": STR,
        "consent_text_version": STR,
        "limits": LIMITS,
        # RFC 3339, with an offset.
        "server_time": STR,
    }
)

# --- leases ----------------------------------------------------------------------

PHASES = ("learn", "protect", "readiness", "probe")
CLASSES = ("real", "attack")

# count is at most the session's limits.lease_max.
LEASE_REQUEST = Obj(
    {
        "phase": Enum(*PHASES),
        "round": Opt(Enum(1, 2)),
        "count": INT,
        "seq": INT,
        "probe_roles": Opt(ListOf(STR)),
    }
)

# A single-use envelope: pass it through unchanged, once.
ENVELOPE = Obj({"encrypted": STR, "timestamp": INT, "nonce": STR, "v": INT})

VISIT = Obj(
    {
        "ref": STR,
        "class": Enum(*CLASSES),
        "user_id": STR,
        "client_ip": STR,
        "script": {"success": BOOL, "success_after_ms": INT},
        "envelopes": {
            "action_check": Opt(ENVELOPE),
            "action_log": ENVELOPE,
            "success_log": Opt(ENVELOPE),
        },
    }
)

# expires_at (RFC 3339) is a budget for the client: use the visits before then.
LEASE = Obj({"seq": INT, "expires_at": STR, "visits": ListOf(VISIT)})

# --- status ------------------------------------------------------------------------

WORKFLOW_STATES = ("none", "draft", "queued", "dry_running", "ready", "active", "paused")

PREDICTION = Obj({"conv_now": NUMBER, "conv_pred": NUMBER, "blocked_share": NUMBER})

# A workflow named "playground" whose methods aren't the playground's own
# comes back as {state, methods_match: false} with every other field null,
# and so does "no such workflow" (state "none").
WORKFLOW = Obj(
    {
        "state": Enum(*WORKFLOW_STATES),
        "stage": INT | NULL,
        "stages_total": INT | NULL,
        "action": STR | NULL,
        "methods_match": BOOL,
        "method_ok": BOOL | NULL,
        "target_ok": BOOL | NULL,
        "prediction": PREDICTION | NULL,
        "protecting": BOOL | NULL,
        "protection_stage": Enum(0, 1, 2, 3) | NULL,
    }
)

# stored_since is null unless the request passed ?since=.
STATUS = Obj(
    {
        "workflow": WORKFLOW,
        "window": {"action_received": INT, "success_received": INT},
        "stored_since": NULL | {"action": INT, "success": INT},
    }
)

# --- the end of a session ------------------------------------------------------------

TELEMETRY_REQUEST = Obj({"chapter": INT, "finding_ids": ListOf(STR)})

# results: conversion without and with SignalGate, as the console measured it.
RESULTS = Obj({"conv_before": NUMBER, "conv_after": NUMBER})
COMPLETE_REQUEST = Obj({"keep_workflow": BOOL, "results": Opt(RESULTS)})

# --- routes ----------------------------------------------------------------------------

SESSIONS_PATH = "/v0/playground/sessions"
SESSION_PATH = SESSIONS_PATH + "/{sid}"

_ANY_ROUTE = {400: ERROR, 401: ERROR, 426: ERROR}
_SESSION_ROUTE = {**_ANY_ROUTE, 404: ERROR}

ROUTES: dict[str, Route] = {
    "create_session": Route(
        "POST",
        SESSIONS_PATH,
        request=SESSION_REQUEST,
        responses={
            201: SESSION,
            **_ANY_ROUTE,
            409: SESSION_ACTIVE,
            412: NO_ACTIVE_PUBLIC_KEY,
            429: Response(ERROR, headers=(RETRY_AFTER,)),
        },
    ),
    "lease": Route(
        "POST",
        SESSION_PATH + "/leases",
        request=LEASE_REQUEST,
        responses={
            200: LEASE,
            **_SESSION_ROUTE,
            409: LEASE_CONFLICT,
            429: Response(SLOW_DOWN, headers=(RETRY_AFTER,)),
        },
    ),
    "status": Route(
        "GET", SESSION_PATH, query=("since",), responses={200: STATUS, **_SESSION_ROUTE}
    ),
    "telemetry": Route(
        "POST",
        SESSION_PATH + "/telemetry",
        request=TELEMETRY_REQUEST,
        responses={204: None, **_SESSION_ROUTE},
    ),
    "complete": Route(
        "POST",
        SESSION_PATH + "/complete",
        request=COMPLETE_REQUEST,
        responses={204: None, **_SESSION_ROUTE},
    ),
    "end_session": Route("DELETE", SESSION_PATH, responses={204: None, **_SESSION_ROUTE}),
}

HEADER_SETS = {"api": HeaderSet("every /v0/playground request", HEADERS)}

TITLE = "SignalGate playground API"

__all__ = [
    "ACTION_METHOD",
    "CLASSES",
    "COMPLETE_REQUEST",
    "ENVELOPE",
    "ERROR",
    "HEADERS",
    "HEADER_SETS",
    "LEASE",
    "LEASE_CONFLICT",
    "LEASE_REQUEST",
    "LIMITS",
    "NO_ACTIVE_PUBLIC_KEY",
    "NUMBER",
    "PHASES",
    "PREDICTION",
    "PROTOCOL_HEADER",
    "PROTOCOL_VERSION",
    "RESULTS",
    "RETRY_AFTER",
    "ROUTES",
    "SESSION",
    "SESSIONS_PATH",
    "SESSION_ACTIVE",
    "SESSION_PATH",
    "SESSION_REQUEST",
    "SLOW_DOWN",
    "STATUS",
    "SUCCESS_METHOD",
    "TELEMETRY_REQUEST",
    "TITLE",
    "VISIT",
    "WORKFLOW",
    "WORKFLOW_NAME",
    "WORKFLOW_STATES",
    "error_envelope",
]
