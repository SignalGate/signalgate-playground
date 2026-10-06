"""Type specs, and one canonical sample each, for the control routes and call records.

Every response the sample app's ``/__playground/*`` routes return, every
record ``/calls`` carries, and the headers the console sets on its requests
have a spec here. A spec maps each key to the JSON types its value may have:

- ``STR``, ``INT``, ``FLOAT``, ``BOOL``, ``NULL``: one JSON type each
  (``INT`` never matches a bool, and ``FLOAT`` matches a float only);
- ``Enum(...)``: exactly one of the listed literal values;
- ``Obj({...})`` (or a plain dict inside a union): an object with exactly
  these keys, except that a key whose spec is ``Opt(...)`` may be left out;
- ``ListOf(spec)``: a list whose items all match ``spec``;
- ``MapOf(spec)``: an object with any string keys, whose values all match;
- ``a | b``: either; ``x | NULL`` is how a key is made nullable.

``J`` is a value the recording client copied from its caller: any JSON scalar,
or ``{"type": <type name>}`` for anything else.

``assert_same_shape(actual, spec)`` checks that the key set of every object
is exactly its spec's, recursively, and that every value matches one of its
allowed types. A sample shows one example only; a sample can't show that a
key may also be null, which is why the specs exist. Tests run
``assert_same_shape`` on real responses and real records, never on these
samples alone.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

# --- the spec language -------------------------------------------------------


class Spec:
    """Base class of every spec; ``a | b`` makes a union."""

    def __or__(self, other: Any) -> Spec:
        return Union.of(self, as_spec(other))

    def __ror__(self, other: Any) -> Spec:
        return Union.of(as_spec(other), self)

    def check(self, value: Any, path: str) -> list[str]:
        """Every mismatch between ``value`` and this spec, as ``path: reason``."""
        raise NotImplementedError

    def describe(self) -> str:
        raise NotImplementedError


def as_spec(value: Any) -> Spec:
    """A spec, or a plain dict read as ``Obj(dict)``."""
    if isinstance(value, Spec):
        return value
    if isinstance(value, Mapping):
        return Obj(value)
    raise TypeError(f"not a spec: {value!r}")


@dataclass(frozen=True, eq=False)
class Kind(Spec):
    """One JSON type."""

    name: str

    def matches(self, value: Any) -> bool:
        if self.name == "str":
            return isinstance(value, str)
        if self.name == "int":
            return isinstance(value, int) and not isinstance(value, bool)
        if self.name == "float":
            return isinstance(value, float)
        if self.name == "bool":
            return isinstance(value, bool)
        if self.name == "null":
            return value is None
        raise AssertionError(f"unknown kind {self.name}")

    def check(self, value: Any, path: str) -> list[str]:
        if self.matches(value):
            return []
        return [f"{path}: expected {self.name}, got {type(value).__name__}"]

    def describe(self) -> str:
        return self.name


STR = Kind("str")
INT = Kind("int")
FLOAT = Kind("float")
BOOL = Kind("bool")
NULL = Kind("null")


@dataclass(frozen=True, eq=False)
class Enum(Spec):
    """Exactly one of ``values`` (compared with the value's own type)."""

    values: tuple[Any, ...]

    def __init__(self, *values: Any) -> None:
        if not values:
            raise ValueError("an Enum needs at least one value")
        object.__setattr__(self, "values", tuple(values))

    def check(self, value: Any, path: str) -> list[str]:
        for allowed in self.values:
            if type(value) is type(allowed) and value == allowed:
                return []
        return [f"{path}: {value!r} is not one of {list(self.values)!r}"]

    def describe(self) -> str:
        return f"enum{list(self.values)!r}"


@dataclass(frozen=True, eq=False)
class Obj(Spec):
    """An object with exactly the keys of ``fields``."""

    fields: Mapping[str, Spec]

    def __init__(self, fields: Mapping[str, Any]) -> None:
        object.__setattr__(self, "fields", {key: as_spec(spec) for key, spec in fields.items()})

    def check(self, value: Any, path: str) -> list[str]:
        if not isinstance(value, dict):
            return [f"{path}: expected an object, got {type(value).__name__}"]
        problems: list[str] = []
        required = {key for key, spec in self.fields.items() if not isinstance(spec, Opt)}
        missing = sorted(required - set(value))
        extra = sorted(set(value) - set(self.fields), key=str)
        if missing:
            problems.append(f"{path}: missing keys {missing}")
        if extra:
            problems.append(f"{path}: unexpected keys {extra}")
        for key, spec in self.fields.items():
            if key in value:
                problems.extend(spec.check(value[key], f"{path}.{key}"))
        return problems

    def describe(self) -> str:
        return "{" + ", ".join(self.fields) + "}"


@dataclass(frozen=True, eq=False)
class Opt(Spec):
    """The spec of an ``Obj`` key that may be left out; when present, it matches ``spec``."""

    spec: Spec

    def __init__(self, spec: Any) -> None:
        if isinstance(spec, Opt):
            raise TypeError("Opt(Opt(...)) says nothing more than Opt(...)")
        object.__setattr__(self, "spec", as_spec(spec))

    def check(self, value: Any, path: str) -> list[str]:
        return self.spec.check(value, path)

    def describe(self) -> str:
        return f"optional {self.spec.describe()}"


@dataclass(frozen=True, eq=False)
class ListOf(Spec):
    """A list whose every item matches ``item``."""

    item: Spec

    def __init__(self, item: Any) -> None:
        object.__setattr__(self, "item", as_spec(item))

    def check(self, value: Any, path: str) -> list[str]:
        if not isinstance(value, list):
            return [f"{path}: expected a list, got {type(value).__name__}"]
        problems: list[str] = []
        for index, element in enumerate(value):
            problems.extend(self.item.check(element, f"{path}[{index}]"))
        return problems

    def describe(self) -> str:
        return f"list[{self.item.describe()}]"


@dataclass(frozen=True, eq=False)
class MapOf(Spec):
    """An object with any string keys, whose every value matches ``value``."""

    value: Spec

    def __init__(self, value: Any) -> None:
        object.__setattr__(self, "value", as_spec(value))

    def check(self, value: Any, path: str) -> list[str]:
        if not isinstance(value, dict):
            return [f"{path}: expected an object, got {type(value).__name__}"]
        problems: list[str] = []
        for key, element in value.items():
            if not isinstance(key, str):
                problems.append(f"{path}: key {key!r} is not a string")
                continue
            problems.extend(self.value.check(element, f"{path}[{key!r}]"))
        return problems

    def describe(self) -> str:
        return f"map[{self.value.describe()}]"


@dataclass(frozen=True, eq=False)
class Union(Spec):
    """Any one of ``options``."""

    options: tuple[Spec, ...]

    @classmethod
    def of(cls, *specs: Spec) -> Union:
        options: list[Spec] = []
        for spec in specs:
            if isinstance(spec, Opt):
                raise TypeError("Opt() goes around a key's whole spec, never inside a union")
            options.extend(spec.options if isinstance(spec, Union) else (spec,))
        return cls(tuple(options))

    def check(self, value: Any, path: str) -> list[str]:
        attempts = [option.check(value, path) for option in self.options]
        if any(not problems for problems in attempts):
            return []
        # Report the closest option: the one with the fewest problems.
        closest = min(attempts, key=len)
        kinds = " | ".join(option.describe() for option in self.options)
        return [f"{path}: matches none of {kinds}", *closest]

    def describe(self) -> str:
        return " | ".join(option.describe() for option in self.options)


def assert_same_shape(actual: Any, spec: Any) -> None:
    """Fail unless ``actual`` has exactly the shape ``spec`` describes."""
    problems = as_spec(spec).check(actual, "$")
    assert not problems, "shape mismatch:\n" + "\n".join(problems)


# --- shared pieces -----------------------------------------------------------

# A value the recording client copied from its caller.
J = STR | INT | FLOAT | BOOL | NULL | {"type": STR}

# Where user code made a call: a repo-relative file, or null outside the repo.
SITE = NULL | {"file": STR, "line": INT, "func": STR}

METHODS = ("playground.action", "playground.success")
SELF_TEST_CODES = (
    "sdk_version",
    "transport_import",
    "transport_signature",
    "transport_protocol",
    "facade",
    "client_hook",
    "probe_hooks",
    "exit_hook",
)
CHECK_OUTCOMES = ("pass", "fail", "skip")
KEY_STATES = ("absent", "placeholder", "invalid", "set")
CANARY_ANSWERS = ("block", "dry_run_block", "admin_alert", "422")

# SDK counters, flat, in the SDK's own spelling: {name or name{labels}: count}.
METRICS = MapOf(INT)

# --- control route responses (/__playground/*) --------------------------------

HEALTH = Obj(
    {
        "boot_id": STR,
        "sdk": Enum("signalgate"),
        "sdk_version": STR,
        "methods": ListOf(Enum(*METHODS)),
        "clients_created": INT,
        "client_ready": BOOL,
        "self_test": {
            "ok": BOOL,
            "failed": ListOf(Enum(*SELF_TEST_CODES)),
            "checks": Obj({code: Enum(*CHECK_OUTCOMES) for code in SELF_TEST_CODES}),
        },
        "key": Enum(*KEY_STATES),
    }
)

LEDGER_ACTION = Obj(
    {
        "kind": Enum("action"),
        "seq": INT,
        "ref": STR,
        "user": STR,
        "client_ip": STR,
        "run": STR,
    }
)
LEDGER_INBOX = Obj({"kind": Enum("inbox"), "seq": INT, "ref": STR, "run": STR})
LEDGER_CONFIRM = Obj({"kind": Enum("confirm"), "seq": INT, "ref": STR, "ok": BOOL, "run": STR})
LEDGER_ENTRY = LEDGER_ACTION | LEDGER_INBOX | LEDGER_CONFIRM

LEDGER = Obj({"entries": ListOf(LEDGER_ENTRY), "cursor": INT, "more": BOOL})

# --- call records (the items of /calls) ---------------------------------------

# Every t0 and t1 below is a time.perf_counter() reading, one clock for every
# record kind, so call and attempt times compare within one boot of the app.

# One record per call user code makes on the recording client, appended when
# the call returns or raises. t0 and t1 are read around a check (null for the
# other ops).
CALL_RECORD = Obj(
    {
        "kind": Enum("call"),
        "op": Enum("log", "check", "close", "enter", "exit"),
        "seq": INT,
        "ref": STR,
        "run": STR,
        "site": SITE,
        "thread": STR,
        "on_loop": BOOL,
        "arg_type": STR | NULL,
        "payload_type": STR | NULL,
        "method": J,
        "user_id": J,
        "ip": J,
        "timestamp": J,
        "nonce": STR | NULL,
        "t0": FLOAT | NULL,
        "t1": FLOAT | NULL,
        "outcome": Enum("verdict", "failed_open", "raised", "enqueued", "not_enqueued", "returned"),
        "action": STR | NULL,
        "processing_time_us": INT | NULL,
        "log_job": INT | NULL,
        "error": NULL | {"type": STR, "status": INT | NULL, "code": STR | NULL},
    }
)

# One record per KitTransport.post, appended when the post returns or raises.
# t0 and t1 are time.perf_counter() readings taken on the thread that calls
# post, as it starts and as it finishes (a canary's delay falls in between):
# the same clock as a check call's t0 and t1.
ATTEMPT_RECORD = Obj(
    {
        "kind": Enum("attempt"),
        "endpoint": Enum("check", "log", "other"),
        "call_seq": INT | NULL,
        "log_job": INT | NULL,
        "attempt": INT,
        "method": J,
        "nonce": STR | NULL,
        "idempotency_key": STR,
        "request_id": STR,
        "status": INT | NULL,
        "code": STR | NULL,
        "error_class": STR | NULL,
        "timed_out": BOOL,
        "timeout_phase": Enum("connect", "read") | NULL,
        "verdict": STR | NULL,
        "processing_time_us": INT | NULL,
        "local": NULL | Enum("guard", "canary"),
        "t0": FLOAT,
        "t1": FLOAT,
    }
)

CONSTRUCT_RECORD = Obj(
    {
        "kind": Enum("construct"),
        "seq": INT,
        "ref": STR,
        "run": STR,
        "site": SITE,
        "thread": STR,
        "singleton": BOOL,
        "kit_transport": BOOL,
    }
)

# The spec for each record kind, keyed by the record's own "kind".
RECORD_SPECS: dict[str, Spec] = {
    "call": CALL_RECORD,
    "attempt": ATTEMPT_RECORD,
    "construct": CONSTRUCT_RECORD,
}
RECORD = CALL_RECORD | ATTEMPT_RECORD | CONSTRUCT_RECORD

CALLS = Obj(
    {
        "v": Enum(1),
        "boot_id": STR,
        "records": ListOf(RECORD),
        "cursor": INT,
        "more": BOOL,
        "oldest_cursor": INT,
        "truncated": BOOL,
        "record_errors": INT,
    }
)

# The body the console sends to POST /canary. The console always sends every
# key; the app also accepts an item without delay_ms (it defaults to 0).
CANARY_REQUEST = Obj(
    {"canaries": ListOf({"nonce": STR, "answer": Enum(*CANARY_ANSWERS), "delay_ms": INT})}
)
CANARY = Obj({"registered": INT})

# POST /shutdown answers once close_client() has returned, from two reads
# taken then: closed is client_state()'s client_ready (True iff a shared
# client was created in this boot; it is now closed), and metrics is
# metrics_snapshot() when closed is true (the closed client's final counters,
# possibly {}) and null when it is false. A repeat call answers the same way.
SHUTDOWN = Obj({"ok": Enum(True), "boot_id": STR, "closed": BOOL, "metrics": NULL | METRICS})

INBOX = Obj({"tokens": MapOf(STR)})

# Refusals every route shares.
VALIDATION_ERROR = Obj({"ok": Enum(False), "error": Enum("validation_error")})
UNAUTHORIZED = Obj({"detail": Enum("unauthorized")})
FORBIDDEN = Obj({"detail": Enum("forbidden")})


# --- headers the console sets on its requests to the sample app ---------------

# Only the headers this contract defines. Transport headers (Host,
# Content-Type, User-Agent and the like) are not part of it.

# A visitor's POST /api/action and POST /api/confirm.
VISIT_HEADERS = Obj({"X-Playground-Ref": STR, "X-Forwarded-For": STR, "X-Playground-Run": Opt(STR)})
# Every control route: the per-boot bearer token, and for /inbox the run.
# A control request never carries X-Forwarded-For.
CONTROL_HEADERS = Obj({"Authorization": STR, "X-Playground-Run": Opt(STR)})


def record_spec(record: Mapping[str, Any]) -> Spec:
    """The spec for ``record``, chosen by its ``kind``."""
    return RECORD_SPECS[record["kind"]]


# --- canonical samples ---------------------------------------------------------

_BOOT_ID = "0f1e2d3c4b5a69788796a5b4c3d2e1f0"
_REF = "ref-5a6b7c8d"
_RUN = "run-1a2b3c4d"
_USER = "user-0a1b2c3d"
_IP = "198.51.100.7"
_NONCE = "nonce-sample"
_SITE = {"file": "app/routes.py", "line": 42, "func": "action"}

SAMPLE_CALL = {
    "kind": "call",
    "op": "check",
    "seq": 8,
    "ref": _REF,
    "run": _RUN,
    "site": _SITE,
    "thread": "AnyIO worker thread",
    "on_loop": False,
    "arg_type": "Event",
    "payload_type": "EncryptedPayload",
    "method": "playground.action",
    "user_id": _USER,
    "ip": _IP,
    "timestamp": "2026-01-02T03:04:05.678901+00:00",
    "nonce": _NONCE,
    "t0": 100.125,
    "t1": 100.5,
    "outcome": "verdict",
    "action": "allow",
    "processing_time_us": 0,
    "log_job": None,
    "error": None,
}

SAMPLE_ATTEMPT = {
    "kind": "attempt",
    "endpoint": "check",
    "call_seq": 8,
    "log_job": None,
    "attempt": 1,
    "method": "playground.action",
    "nonce": _NONCE,
    "idempotency_key": "idem-sample",
    "request_id": "req-sample",
    "status": 200,
    "code": None,
    "error_class": None,
    "timed_out": False,
    "timeout_phase": None,
    "verdict": "allow",
    "processing_time_us": 0,
    "local": None,
    "t0": 100.25,
    "t1": 100.375,
}

SAMPLE_CONSTRUCT = {
    "kind": "construct",
    "seq": 7,
    "ref": _REF,
    "run": _RUN,
    "site": _SITE,
    "thread": "AnyIO worker thread",
    "singleton": True,
    "kit_transport": True,
}

# More records, one per nullable branch the canonical ones leave out.
SAMPLE_LOG_CALL_RAISED = {
    **SAMPLE_CALL,
    "op": "log",
    "seq": 11,
    "site": None,
    "on_loop": True,
    "arg_type": "dict",
    "payload_type": None,
    "method": {"type": "dict"},
    "user_id": None,
    "ip": None,
    "timestamp": 1_700_000_000,
    "nonce": None,
    "t0": None,
    "t1": None,
    "outcome": "raised",
    "action": None,
    "processing_time_us": None,
    "error": {"type": "AttributeError", "status": None, "code": None},
}
SAMPLE_LOG_ATTEMPT_TIMED_OUT = {
    **SAMPLE_ATTEMPT,
    "endpoint": "log",
    "call_seq": None,
    "log_job": 1,
    "status": None,
    "error_class": "TimeoutError",
    "timed_out": True,
    "timeout_phase": "read",
    "verdict": None,
    "processing_time_us": None,
}
SAMPLE_GUARD_ATTEMPT = {
    **SAMPLE_ATTEMPT,
    "method": "login.attempt",
    "status": 400,
    "code": "PLAYGROUND_METHOD_ONLY",
    "error_class": "ServerError",
    "verdict": None,
    "processing_time_us": None,
    "local": "guard",
}

SAMPLE_HEALTH = {
    "boot_id": _BOOT_ID,
    "sdk": "signalgate",
    "sdk_version": "0.3.2",
    "methods": list(METHODS),
    "clients_created": 0,
    "client_ready": False,
    "self_test": {
        "ok": True,
        "failed": [],
        "checks": {code: "pass" for code in SELF_TEST_CODES},
    },
    "key": "placeholder",
}

SAMPLE_METRICS = {
    "log_enqueued_total": 3,
    "log_sent_total": 2,
    'log_dropped_total{reason="retry_exhausted"}': 1,
    'log_http_error_total{status="422"}': 1,
}

SAMPLE_LEDGER = {
    "entries": [
        {
            "kind": "action",
            "seq": 1,
            "ref": _REF,
            "user": _USER,
            "client_ip": _IP,
            "run": _RUN,
        },
        {"kind": "inbox", "seq": 2, "ref": _REF, "run": _RUN},
        {"kind": "confirm", "seq": 3, "ref": _REF, "ok": True, "run": _RUN},
    ],
    "cursor": 3,
    "more": False,
}

SAMPLE_CALLS = {
    "v": 1,
    "boot_id": _BOOT_ID,
    "records": [SAMPLE_CONSTRUCT, SAMPLE_ATTEMPT, SAMPLE_CALL],
    "cursor": 3,
    "more": False,
    "oldest_cursor": 0,
    "truncated": False,
    "record_errors": 0,
}

SAMPLE_CANARY_REQUEST = {
    "canaries": [
        {"nonce": "nonce-canary-block", "answer": "block", "delay_ms": 0},
        {"nonce": "nonce-canary-reject", "answer": "422", "delay_ms": 0},
    ]
}
SAMPLE_CANARY = {"registered": 2}

SAMPLE_SHUTDOWN = {
    "ok": True,
    "boot_id": _BOOT_ID,
    "closed": True,
    "metrics": SAMPLE_METRICS,
}

SAMPLE_INBOX = {"tokens": {_REF: "token-sample"}}

SAMPLE_VALIDATION_ERROR = {"ok": False, "error": "validation_error"}
SAMPLE_UNAUTHORIZED = {"detail": "unauthorized"}
SAMPLE_FORBIDDEN = {"detail": "forbidden"}

# Every spec with its samples, for the test that each sample matches its spec.
SPECS_AND_SAMPLES: dict[str, tuple[Spec, tuple[Any, ...]]] = {
    "health": (HEALTH, (SAMPLE_HEALTH,)),
    "metrics": (METRICS, (SAMPLE_METRICS, {})),
    "ledger": (LEDGER, (SAMPLE_LEDGER,)),
    "calls": (CALLS, (SAMPLE_CALLS,)),
    "call": (CALL_RECORD, (SAMPLE_CALL, SAMPLE_LOG_CALL_RAISED)),
    "attempt": (
        ATTEMPT_RECORD,
        (SAMPLE_ATTEMPT, SAMPLE_LOG_ATTEMPT_TIMED_OUT, SAMPLE_GUARD_ATTEMPT),
    ),
    "construct": (CONSTRUCT_RECORD, (SAMPLE_CONSTRUCT,)),
    "canary_request": (CANARY_REQUEST, (SAMPLE_CANARY_REQUEST,)),
    "canary": (CANARY, (SAMPLE_CANARY,)),
    "shutdown": (SHUTDOWN, (SAMPLE_SHUTDOWN, {**SAMPLE_SHUTDOWN, "metrics": None})),
    "inbox": (INBOX, (SAMPLE_INBOX,)),
    "validation_error": (VALIDATION_ERROR, (SAMPLE_VALIDATION_ERROR,)),
    "unauthorized": (UNAUTHORIZED, (SAMPLE_UNAUTHORIZED,)),
    "forbidden": (FORBIDDEN, (SAMPLE_FORBIDDEN,)),
}

__all__ = [
    "ATTEMPT_RECORD",
    "BOOL",
    "CALLS",
    "CALL_RECORD",
    "CANARY",
    "CANARY_ANSWERS",
    "CANARY_REQUEST",
    "CHECK_OUTCOMES",
    "CONSTRUCT_RECORD",
    "CONTROL_HEADERS",
    "FLOAT",
    "FORBIDDEN",
    "HEALTH",
    "INBOX",
    "INT",
    "KEY_STATES",
    "LEDGER",
    "LEDGER_ACTION",
    "LEDGER_CONFIRM",
    "LEDGER_ENTRY",
    "LEDGER_INBOX",
    "METHODS",
    "METRICS",
    "NULL",
    "RECORD",
    "RECORD_SPECS",
    "SELF_TEST_CODES",
    "SHUTDOWN",
    "SITE",
    "SPECS_AND_SAMPLES",
    "STR",
    "UNAUTHORIZED",
    "VALIDATION_ERROR",
    "VISIT_HEADERS",
    "Enum",
    "J",
    "Kind",
    "ListOf",
    "MapOf",
    "Obj",
    "Opt",
    "Spec",
    "Union",
    "as_spec",
    "assert_same_shape",
    "record_spec",
]
