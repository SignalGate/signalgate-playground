"""Whitelisting decoders for the playground API's answers.

Each decoder builds a new plain value holding only the fields the console
knows, each checked for its type, and drops everything else. A field the
server adds later is never read, never stored and never shown. A field the
console needs that is missing or of the wrong type raises ``DecodeError``.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from datetime import datetime
from typing import Any


class DecodeError(ValueError):
    """An answer lacks a field the console needs, or holds one of the wrong type."""


# Workflow states and envelope roles the console knows.
WORKFLOW_STATES = ("none", "draft", "queued", "dry_running", "ready", "active", "paused")
CLASSES = ("real", "attack")
ROLES = ("action_check", "action_log", "success_log")

# The session limits: name -> whether it must be a whole number.
LIMIT_FIELDS = {
    "max_size": True,
    "lease_max": True,
    "use_within_s": False,
    "max_queue_age_s": False,
    "clock_warn_s": False,
    "clock_block_s": False,
    "readiness_retry_s": False,
    "readiness_max_attempts": True,
    "ingest_wait_s": False,
    "protect_diagnose_after_s": False,
}


def _mapping(value: Any, where: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise DecodeError(where)
    return value


def _str(value: Any, where: str) -> str:
    if not isinstance(value, str):
        raise DecodeError(where)
    return value


def _int(value: Any, where: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise DecodeError(where)
    return value


def _number(value: Any, where: str) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise DecodeError(where)
    number = float(value)
    if not math.isfinite(number):
        raise DecodeError(where)
    return number


def _bool(value: Any, where: str) -> bool:
    if not isinstance(value, bool):
        raise DecodeError(where)
    return value


def _optional(value: Any, decode: Any, where: str) -> Any:
    return None if value is None else decode(value, where)


def parse_time(value: Any, where: str = "time") -> datetime:
    """An RFC 3339 time with an offset."""
    text = _str(value, where)
    try:
        moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as error:
        raise DecodeError(where) from error
    if moment.tzinfo is None:
        raise DecodeError(where)
    return moment


def limits(raw: Any) -> dict[str, float]:
    """The session's ``limits``: every field the console uses, each a positive number."""
    data = _mapping(raw, "limits")
    out: dict[str, float] = {}
    for name, whole in LIMIT_FIELDS.items():
        value = _int(data.get(name), name) if whole else _number(data.get(name), name)
        if value <= 0:
            raise DecodeError(name)
        out[name] = value
    return out


def session(raw: Any) -> dict[str, Any]:
    """A created session: its id, its limits and the server's clock."""
    data = _mapping(raw, "session")
    methods = _mapping(data.get("methods"), "methods")
    return {
        "session_id": _str(data.get("session_id"), "session_id"),
        "methods": {
            "action": _str(methods.get("action"), "methods.action"),
            "target": _str(methods.get("target"), "methods.target"),
        },
        "workflow_name": _str(data.get("workflow_name"), "workflow_name"),
        "limits": limits(data.get("limits")),
        "server_time": parse_time(data.get("server_time"), "server_time"),
    }


def envelope(raw: Any, where: str = "envelope") -> dict[str, Any]:
    """One single-use envelope, exactly as it came, to pass on unchanged."""
    data = _mapping(raw, where)
    return {
        "encrypted": _str(data.get("encrypted"), where),
        "timestamp": _int(data.get("timestamp"), where),
        "nonce": _str(data.get("nonce"), where),
        "v": _int(data.get("v"), where),
    }


def visit(raw: Any) -> dict[str, Any]:
    """One leased visit: who it is, how it behaves and its envelopes."""
    data = _mapping(raw, "visit")
    cls = _str(data.get("class"), "class")
    if cls not in CLASSES:
        raise DecodeError("class")
    script = _mapping(data.get("script"), "script")
    envelopes = _mapping(data.get("envelopes"), "envelopes")
    return {
        "ref": _str(data.get("ref"), "ref"),
        "class": cls,
        "user_id": _str(data.get("user_id"), "user_id"),
        "client_ip": _str(data.get("client_ip"), "client_ip"),
        "script": {
            "success": _bool(script.get("success"), "success"),
            "success_after_ms": _int(script.get("success_after_ms"), "success_after_ms"),
        },
        "envelopes": {
            role: envelope(envelopes[role], role)
            for role in ROLES
            if envelopes.get(role) is not None
        },
    }


def lease(raw: Any) -> dict[str, Any]:
    """A lease: its sequence number and its visits."""
    data = _mapping(raw, "lease")
    visits = data.get("visits")
    if not isinstance(visits, list):
        raise DecodeError("visits")
    return {"seq": _int(data.get("seq"), "seq"), "visits": [visit(item) for item in visits]}


def _prediction(raw: Any, where: str) -> dict[str, float]:
    data = _mapping(raw, where)
    return {
        "conv_now": _number(data.get("conv_now"), "conv_now"),
        "conv_pred": _number(data.get("conv_pred"), "conv_pred"),
        "blocked_share": _number(data.get("blocked_share"), "blocked_share"),
    }


def _stage(value: Any, where: str) -> int:
    stage = _int(value, where)
    if not 0 <= stage <= 3:
        raise DecodeError(where)
    return stage


def workflow(raw: Any) -> dict[str, Any]:
    """The workflow part of the status: states, booleans and numbers only."""
    data = _mapping(raw, "workflow")
    state = _str(data.get("state"), "state")
    if state not in WORKFLOW_STATES:
        raise DecodeError("state")
    return {
        "state": state,
        "stage": _optional(data.get("stage"), _int, "stage"),
        "stages_total": _optional(data.get("stages_total"), _int, "stages_total"),
        "action": _optional(data.get("action"), _str, "action"),
        "methods_match": _bool(data.get("methods_match"), "methods_match"),
        "method_ok": _optional(data.get("method_ok"), _bool, "method_ok"),
        "target_ok": _optional(data.get("target_ok"), _bool, "target_ok"),
        "prediction": _optional(data.get("prediction"), _prediction, "prediction"),
        "protecting": _optional(data.get("protecting"), _bool, "protecting"),
        "protection_stage": _optional(data.get("protection_stage"), _stage, "protection_stage"),
    }


def _counts(raw: Any, fields: tuple[str, ...], where: str) -> dict[str, int]:
    data = _mapping(raw, where)
    return {name: _int(data.get(name), name) for name in fields}


def status(raw: Any) -> dict[str, Any]:
    """The session status: the workflow, the window counts and the stored counts."""
    data = _mapping(raw, "status")
    stored = data.get("stored_since")
    return {
        "workflow": workflow(data.get("workflow")),
        "window": _counts(data.get("window"), ("action_received", "success_received"), "window"),
        "stored_since": None
        if stored is None
        else _counts(stored, ("action", "success"), "stored_since"),
    }


def error(raw: Any) -> tuple[str | None, dict[str, str]]:
    """An error answer's code and its string details; ``(None, {})`` when unreadable."""
    if not isinstance(raw, Mapping):
        return None, {}
    body = raw.get("error")
    if not isinstance(body, Mapping):
        return None, {}
    code = body.get("code") if isinstance(body.get("code"), str) else None
    details = body.get("details")
    kept: dict[str, str] = {}
    if isinstance(details, Mapping):
        for name in ("started_at", "key_prefix"):
            if isinstance(details.get(name), str):
                kept[name] = details[name]
    return code, kept


__all__ = [
    "CLASSES",
    "LIMIT_FIELDS",
    "ROLES",
    "WORKFLOW_STATES",
    "DecodeError",
    "envelope",
    "error",
    "lease",
    "limits",
    "parse_time",
    "session",
    "status",
    "visit",
    "workflow",
]
