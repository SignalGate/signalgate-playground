"""The code gate: probes 1-7 sent through your app, judged from what it recorded.

Each probe is one crafted request to the sample app, with an envelope the
console got from a ``probe`` lease (``PROBE_VISITS`` visits carrying the
roles ``probe_roles(chapter)`` names):

1. a valid action: it must log ``playground.action`` once, after the action,
   from ``signalgate_log``, and SignalGate must accept it;
2. an invalid user (the kit's reserved ``pg-bad-`` range): no log;
3. an action that fails on purpose (``X-Playground-Action-Fail: 1``): no log;
4. a confirm with a wrong token: no log;
5. a confirm with the right token: one ``playground.success`` log from its
   own ``signalgate`` envelope, accepted;
6. an action and a confirm with no envelopes: both still answer 200;
7. a malformed envelope: the app still answers (no 5xx).

When a valid request fails with a 5xx, the app's log says why: the error
and the line of ``app/routes.py`` it came from (``applog``) turn the
finding into one that names them. A confirm that reads ``signalgate_log``,
which only the action request carries, gets a finding of its own.

On top of the probes: the static checks on ``app/routes.py``, and the
client check (one shared client, never closed while the app runs; in
chapter 3, no ``check()`` call at all). In chapter 3 the probe action also
carries a dummy envelope in ``signalgate``, made on the laptop and answered
there, so a log that reads the wrong field is caught without sending it.

The gate passes only when every check passes and SignalGate accepted at
least one of the probes' real events.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import secrets
import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx

from playground.supervisor.control_client import KEY_DROPPED_CLOSED, ControlClient, ControlError
from playground.supervisor.drain import drain
from playground.traffic.driver import ACTION_PATH, CONFIRM_PATH, RUN_HEADER
from playground.traffic.local_constants import APP_LOG_SETTLE_S, EVENT_TIME_TOLERANCE_S
from playground.verify import findings as F
from playground.verify.applog import (
    APP_LOG_RELATIVE,
    HandlerError,
    log_offset,
    read_handler_errors,
)
from playground.verify.evidence import (
    ACCEPTED,
    DUPLICATE,
    REJECTED,
    AppLink,
    Evidence,
    EvidenceUnavailable,
    Sources,
    collect,
    connect,
    mark,
    new_run_id,
    rejected_status,
)
from playground.verify.model import Check, Finding, GateResult, Site, not_run
from playground.verify.static import SourceUnreadable, check_source

ACTION_METHOD = "playground.action"
SUCCESS_METHOD = "playground.success"
CHECK_FIELD = "signalgate"
LOG_FIELD = "signalgate_log"

REF_HEADER = "X-Playground-Ref"
FORWARDED_HEADER = "X-Forwarded-For"
ACTION_FAIL_HEADER = "X-Playground-Action-Fail"
# The kit's reserved user range that always fails validation.
BAD_USER_PREFIX = "pg-bad-"

# How many leased visits the probes use, and which envelope roles they need.
PROBE_VISITS = 3
_ROLES = ("action_log", "success_log")
_CHECK_ROLE = "action_check"

# Random bytes in a dummy envelope made on the laptop (never a real one).
DUMMY_BYTES = 48
# An envelope with parts missing, for probe 7.
MALFORMED = {"encrypted": "", "nonce": "malformed"}
# A log call the SDK never queued, so it has no answer to wait for.
NOT_QUEUED = "not_queued"
# The answer a dummy envelope gets on the laptop: a verdict that lets the
# request through, so a dummy never changes what the app does.
PASS_THROUGH = "admin_alert"
# What Python says when confirm() reads the envelope only an action carries.
CONFIRM_READS_LOG_FIELD_ERROR = (
    "AttributeError",
    f"'ConfirmBody' object has no attribute '{LOG_FIELD}'",
)


def probe_roles(chapter: int) -> tuple[str, ...]:
    """The envelope roles a probe lease must carry in ``chapter``."""
    return (_CHECK_ROLE, *_ROLES) if chapter >= 5 else _ROLES


def dummy_envelope() -> dict[str, Any]:
    """Random bytes in the public envelope shape, made here, never sent anywhere."""
    return {
        "encrypted": base64.b64encode(secrets.token_bytes(DUMMY_BYTES)).decode("ascii"),
        "timestamp": int(time.time()),
        "nonce": secrets.token_urlsafe(12),
    }


@dataclass(frozen=True)
class ProbeVisit:
    """What the probes use of one leased visit."""

    user: str
    ip: str
    envelopes: Mapping[str, Mapping[str, Any]]


def probe_visits(visits: Iterable[Mapping[str, Any]], chapter: int) -> list[ProbeVisit] | None:
    """The first ``PROBE_VISITS`` leased visits, or None when they lack a needed role."""
    found: list[ProbeVisit] = []
    for raw in list(visits)[:PROBE_VISITS]:
        try:
            user, ip, envelopes = raw["user_id"], raw["client_ip"], raw["envelopes"]
        except (KeyError, TypeError):
            return None
        if not isinstance(envelopes, Mapping) or not all(
            isinstance(envelopes.get(role), Mapping) for role in probe_roles(chapter)
        ):
            return None
        found.append(ProbeVisit(str(user), str(ip), envelopes))
    return found if len(found) == PROBE_VISITS else None


@dataclass
class Step:
    """One request the verifier sent, and how the app answered it."""

    name: str
    handler: str
    ref: str
    user: str
    ip: str
    sent: dict[str, str] = field(default_factory=dict)
    status: int | None = None
    sent_at: float = 0.0
    done_at: float = 0.0


def step(name: str, handler: str, run: str, user: str, ip: str, **fields: Any) -> Step:
    """A ``Step`` whose ``sent`` maps each envelope field to its nonce."""
    sent = {
        key: value["nonce"]
        for key, value in fields.items()
        if isinstance(value, Mapping) and isinstance(value.get("nonce"), str)
    }
    return Step(name, handler, f"{run}-{name}", user, ip, sent)


@dataclass(frozen=True)
class Raised:
    """What the app's log says the requests it failed with a 5xx raised.

    ``file`` is how findings name the routes file; ``errors`` maps a
    request's name to the error its handler logged.
    """

    file: str
    errors: Mapping[str, HandlerError] = field(default_factory=dict)

    def of(self, request: Step) -> HandlerError | None:
        """The error behind ``request``'s 5xx answer, if the log showed one."""
        if request.status is None or request.status < 500:
            return None
        return self.errors.get(request.name)

    def site(self, sources: Sources, error: HandlerError) -> Site:
        return sources.site(self.file, error.line)


# Nothing read from the log: every request's error is unknown.
NOTHING_RAISED = Raised("")


def match_errors(
    steps: Mapping[str, Step | None], errors: Sequence[HandlerError]
) -> tuple[dict[str, HandlerError], bool]:
    """Pair each request answered with a 5xx with the error its handler logged.

    ``steps`` are in the order they were sent and ``errors`` in the order
    they were logged, so each request takes the first error left that went
    through its handler: the outermost function of the routes file in its
    traceback is named after it, wherever in that file it was raised. The
    second value is False when a request found none.
    """
    left = list(errors)
    matched: dict[str, HandlerError] = {}
    complete = True
    for request in steps.values():
        if request is None or request.status is None or request.status < 500:
            continue
        found = next((error for error in left if error.function == request.handler), None)
        if found is None:
            complete = False
            continue
        left.remove(found)
        matched[request.name] = found
    return matched, complete


def error_text(error: HandlerError) -> str:
    """The error as Python names it: ``<Type>: <message>``, or the bare type."""
    return f"{error.error}: {error.message}" if error.message else error.error


def reads_log_field_in_confirm(error: HandlerError) -> bool:
    """True for the error a confirm() that reads ``signalgate_log`` raises."""
    name, message = CONFIRM_READS_LOG_FIELD_ERROR
    return error.error == name and error.message.startswith(message)


def _raised_finding(
    request: Step, error: HandlerError, raised: Raised, sources: Sources
) -> Finding:
    site = raised.site(sources, error)
    if reads_log_field_in_confirm(error):
        return F.finding(
            F.CONFIRM_READS_LOG_FIELD,
            site,
            user=request.user,
            status=request.status,
            error=error_text(error),
        )
    return F.finding(
        F.HANDLER_ERROR,
        site,
        handler=request.handler,
        error=error_text(error),
        location=f"{raised.file}:{error.line}",
    )


async def send(
    visitor: httpx.AsyncClient,
    request: Step,
    body: Mapping[str, Any],
    run: str,
    *,
    fail: bool = False,
) -> Step:
    """Send one visitor request for ``request`` and record the answer's status."""
    path = ACTION_PATH if request.handler == "action" else CONFIRM_PATH
    headers = {REF_HEADER: request.ref, FORWARDED_HEADER: request.ip, RUN_HEADER: run}
    if fail:
        headers[ACTION_FAIL_HEADER] = "1"
    request.sent_at = time.time()
    try:
        response = await visitor.post(path, json=dict(body), headers=headers)
        request.status = response.status_code
    except httpx.HTTPError:
        request.status = None
    request.done_at = time.time()
    return request


def not_ready(health: Any) -> str | None:
    """Why the app can't be checked right now, or None."""
    if not health.self_test.get("ok", False):
        return "self_test"
    if health.key != "set":
        return "key"
    return None


# --- sending the probes ------------------------------------------------------------


async def _send_probes(
    visitor: httpx.AsyncClient,
    control: ControlClient,
    visits: list[ProbeVisit],
    chapter: int,
    run: str,
) -> dict[str, Step | None]:
    a, b, c = visits
    if chapter >= 5:
        a_check, b_check, c_check = (v.envelopes[_CHECK_ROLE] for v in visits)
    else:
        a_check = dummy_envelope()
        await control.canary([{"nonce": a_check["nonce"], "answer": PASS_THROUGH}])
        b_check = c_check = None

    def action_body(user: str, check: Any, log: Any) -> dict[str, Any]:
        body: dict[str, Any] = {"user": user, LOG_FIELD: log}
        if check is not None:
            body[CHECK_FIELD] = check
        return body

    steps: dict[str, Step | None] = {}
    p1 = step(
        "p1",
        "action",
        run,
        a.user,
        a.ip,
        signalgate=a_check,
        signalgate_log=a.envelopes["action_log"],
    )
    steps["p1"] = await send(
        visitor, p1, action_body(a.user, a_check, a.envelopes["action_log"]), run
    )
    tokens = await control.inbox([p1.ref])

    wrong = step("p4", "confirm", run, b.user, b.ip, signalgate=b.envelopes["success_log"])
    body = {
        "user": b.user,
        "token": f"wrong-{secrets.token_hex(8)}",
        CHECK_FIELD: b.envelopes["success_log"],
    }
    steps["p4"] = await send(visitor, wrong, body, run)

    steps["p5"] = None
    if p1.ref in tokens:
        right = step("p5", "confirm", run, a.user, a.ip, signalgate=a.envelopes["success_log"])
        body = {"user": a.user, "token": tokens[p1.ref], CHECK_FIELD: a.envelopes["success_log"]}
        steps["p5"] = await send(visitor, right, body, run)

    bad_user = f"{BAD_USER_PREFIX}{secrets.token_hex(4)}"
    p2 = step(
        "p2",
        "action",
        run,
        bad_user,
        b.ip,
        signalgate=b_check,
        signalgate_log=b.envelopes["action_log"],
    )
    steps["p2"] = await send(
        visitor, p2, action_body(bad_user, b_check, b.envelopes["action_log"]), run
    )

    p3 = step(
        "p3",
        "action",
        run,
        c.user,
        c.ip,
        signalgate=c_check,
        signalgate_log=c.envelopes["action_log"],
    )
    body = action_body(c.user, c_check, c.envelopes["action_log"])
    steps["p3"] = await send(visitor, p3, body, run, fail=True)

    p6 = step("p6", "action", run, a.user, a.ip)
    steps["p6"] = await send(visitor, p6, {"user": a.user}, run)
    tokens = await control.inbox([p6.ref])
    steps["p6_confirm"] = None
    if p6.ref in tokens:
        p6c = step("p6_confirm", "confirm", run, a.user, a.ip)
        steps["p6_confirm"] = await send(
            visitor, p6c, {"user": a.user, "token": tokens[p6.ref]}, run
        )

    p7 = step("p7", "action", run, a.user, a.ip)
    body = {"user": a.user, CHECK_FIELD: MALFORMED, LOG_FIELD: MALFORMED}
    steps["p7"] = await send(visitor, p7, body, run)
    return steps


# --- judging -------------------------------------------------------------------------


def _time_ok(value: Any, request: Step) -> bool:
    """An RFC 3339 time with an offset, taken while the request was being handled."""
    if not isinstance(value, str):
        return False
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    if moment.tzinfo is None:
        return False
    at = moment.timestamp()
    return (
        request.sent_at - EVENT_TIME_TOLERANCE_S <= at <= request.done_at + EVENT_TIME_TOLERANCE_S
    )


def raised_site(ev: Evidence, ref: str, sources: Sources) -> Any:
    """Where an SDK call of this request raised, if one did."""
    raised = [c for c in ev.calls(ref=ref) if c.get("outcome") == "raised"]
    return sources.of(raised[0]) if raised else None


def judge_check_field(request: Step, ev: Evidence, sources: Sources) -> list[Finding]:
    """A check() call that didn't read the envelope in ``signalgate``."""
    for call in ev.calls("check", ref=request.ref):
        nonce = call.get("nonce")
        if nonce == request.sent.get(CHECK_FIELD):
            continue
        site = sources.of(call)
        if nonce is not None and nonce == request.sent.get(LOG_FIELD):
            return [
                F.finding(
                    F.WRONG_ENVELOPE_FIELD,
                    site,
                    "check",
                    field=LOG_FIELD,
                    expected_field=CHECK_FIELD,
                )
            ]
        return [F.finding(F.ENVELOPE_FROM_OTHER_REQUEST, site, "check", expected_field=CHECK_FIELD)]
    return []


def judge_log(
    request: Step, ev: Evidence, sources: Sources, *, method: str, field_name: str
) -> tuple[list[Finding], str | None]:
    """Findings for the one log a handled request should make, and how that log ended."""
    logs = ev.calls("log", ref=request.ref)
    params = {"user": request.user, "status": request.status, "handler": request.handler}
    if not logs:
        missing = F.LOG_MISSING if method == ACTION_METHOD else F.SUCCESS_LOG_MISSING
        return [F.finding(missing, None, **params)], None
    wrong = [c for c in logs if c.get("method") != method]
    if wrong:
        site = sources.of(wrong[0])
        got = wrong[0].get("method")
        return [F.finding(F.WRONG_METHOD, site, request.handler, method=got, expected=method)], None
    if len(logs) > 1:
        site = sources.of(logs[1])
        return [F.finding(F.LOGGED_TWICE, site, method=method, count=len(logs), **params)], None
    log = logs[0]
    site = sources.of(log)
    actions = ev.entries("action", ref=request.ref)
    if method == ACTION_METHOD and actions and log.get("seq", 0) < actions[0].get("seq", 0):
        return [F.finding(F.LOG_BEFORE_ACTION, site, **params)], None
    if log.get("outcome") == "raised":
        # log() raised and the app carried on: nothing was ever sent.
        missing = F.LOG_MISSING if method == ACTION_METHOD else F.SUCCESS_LOG_MISSING
        return [F.finding(missing, site, **params)], None
    nonce = log.get("nonce")
    if nonce is not None and any(
        c.get("nonce") == nonce for c in ev.calls("check", ref=request.ref)
    ):
        return [F.finding(F.ENVELOPE_REUSED, site, field=field_name)], None
    if nonce is None or nonce != request.sent.get(field_name):
        other = next((k for k, v in request.sent.items() if nonce is not None and v == nonce), None)
        if other is not None:
            found = F.finding(
                F.WRONG_ENVELOPE_FIELD,
                site,
                "log",
                method=method,
                field=other,
                expected_field=field_name,
            )
        else:
            found = F.finding(
                F.ENVELOPE_FROM_OTHER_REQUEST, site, "log", method=method, expected_field=field_name
            )
        return [found], None
    if log.get("user_id") != request.user:
        return [
            F.finding(F.USER_ID_MISMATCH, site, user=request.user, got=log.get("user_id"))
        ], None
    if log.get("ip") != request.ip:
        return [F.finding(F.IP_NOT_FORWARDED, site, ip=request.ip, got=log.get("ip"))], None
    if not _time_ok(log.get("timestamp"), request):
        return [F.finding(F.EVENT_TIME_INVALID, site, got=log.get("timestamp"))], None
    if log.get("outcome") != "enqueued":
        # Never queued (a closed client drops it): the client check says why.
        return [], NOT_QUEUED
    outcome = ev.outcome(log)
    if outcome == REJECTED:
        status = rejected_status(ev.attempts(log))
        return [
            F.finding(F.LOG_REJECTED, site, method=method, user=request.user, code=status)
        ], outcome
    return [], outcome


def _handled(
    name: str,
    request: Step | None,
    ev: Evidence,
    sources: Sources,
    *,
    method: str,
    field_name: str,
    chapter: int,
    raised: Raised = NOTHING_RAISED,
) -> Check:
    """Probes 1 and 5: a valid request, handled and logged once, accepted."""
    if request is None:
        return Check.skipped(name, "depends")
    if request.status is None:
        return Check.skipped(name, "app")
    if chapter >= 5 and any(ev.verdict(c) == "block" for c in ev.calls("check", ref=request.ref)):
        return Check.skipped(name, "verdict")
    if request.status != 200:
        error = raised.of(request)
        if error is not None:
            return Check.judged(name, [_raised_finding(request, error, raised, sources)])
        site = raised_site(ev, request.ref, sources)
        params = {"user": request.user, "status": request.status}
        return Check.judged(name, [F.finding(F.REQUEST_FAILED, site, request.handler, **params)])
    found, outcome = judge_log(request, ev, sources, method=method, field_name=field_name)
    if not found and chapter >= 5 and request.handler == "action":
        found = judge_check_field(request, ev, sources)
    if found:
        return Check.judged(name, found)
    if outcome in (ACCEPTED, DUPLICATE):
        return Check.judged(name, [])
    return Check.skipped(name, "depends" if outcome == NOT_QUEUED else "signalgate")


def _not_logged(
    name: str,
    request: Step | None,
    ev: Evidence,
    sources: Sources,
    finding_id: str,
    variant: str | None,
) -> Check:
    """Probes 2, 3 and 4: a request that failed must leave no log."""
    if request is None or request.status is None:
        return Check.skipped(name, "app")
    logs = ev.calls("log", ref=request.ref)
    if logs:
        params = {"user": request.user, "status": request.status}
        return Check.judged(name, [F.finding(finding_id, sources.of(logs[0]), variant, **params)])
    return Check.judged(name, [])


def _still_answers(
    name: str,
    requests: list[Step | None],
    ev: Evidence,
    sources: Sources,
    finding_id: str,
    *,
    ok: set[int] | None,
    raised: Raised = NOTHING_RAISED,
) -> Check:
    """Probes 6 and 7: the app keeps answering (``ok`` statuses, or anything below 500)."""
    for request in requests:
        if request is None:
            return Check.skipped(name, "depends")
        if request.status is None:
            return Check.skipped(name, "app")
        broken = request.status not in ok if ok is not None else request.status >= 500
        if broken:
            error = raised.of(request)
            if error is not None and reads_log_field_in_confirm(error):
                return Check.judged(name, [_raised_finding(request, error, raised, sources)])
            site = raised_site(ev, request.ref, sources)
            if site is None and error is not None:
                site = raised.site(sources, error)
            params = {"status": request.status}
            return Check.judged(name, [F.finding(finding_id, site, request.handler, **params)])
    return Check.judged(name, [])


def judge_client(ev: Evidence, sources: Sources, *, chapter: int) -> Check:
    """One shared client, never closed while the app runs; no check() in chapter 3."""
    found: list[Finding] = []
    extra = [r for r in ev.constructs() if not r.get("singleton")]
    if extra:
        found.append(F.finding(F.CLIENT_PER_REQUEST, sources.of(extra[0])))
    closes = [c for c in ev.calls() if c.get("op") in ("close", "exit")]
    if closes or ev.metrics.get(KEY_DROPPED_CLOSED, 0) > 0:
        found.append(F.finding(F.CLIENT_CLOSED, sources.of(closes[0]) if closes else None))
    checks = ev.calls("check")
    if chapter < 5 and checks:
        found.append(F.finding(F.CHECK_TOO_EARLY, sources.of(checks[0])))
    return Check.judged("client", found)


def accepted_count(ev: Evidence, refs: Iterable[str]) -> int:
    """How many of these requests' log and check calls SignalGate accepted."""
    wanted = set(refs)
    return sum(
        1
        for call in ev.calls()
        if call.get("ref") in wanted
        and call.get("op") in ("log", "check")
        and ev.outcome(call) == ACCEPTED
    )


def judge_probes(
    steps: Mapping[str, Step | None],
    ev: Evidence,
    sources: Sources,
    *,
    chapter: int,
    raised: Raised = NOTHING_RAISED,
) -> list[Check]:
    """Every probe's check, in probe order.

    ``raised`` says what the app logged for the requests it failed with a
    5xx (nothing, unless the log was read).
    """
    p = steps.get
    return [
        _handled(
            "probe_1",
            p("p1"),
            ev,
            sources,
            method=ACTION_METHOD,
            field_name=LOG_FIELD,
            chapter=chapter,
            raised=raised,
        ),
        _not_logged("probe_2", p("p2"), ev, sources, F.LOG_WITHOUT_ACTION, "invalid_user"),
        _not_logged("probe_3", p("p3"), ev, sources, F.LOG_WITHOUT_ACTION, "action_failed"),
        _not_logged("probe_4", p("p4"), ev, sources, F.SUCCESS_LOG_ON_FAILURE, None),
        _handled(
            "probe_5",
            p("p5"),
            ev,
            sources,
            method=SUCCESS_METHOD,
            field_name=CHECK_FIELD,
            chapter=chapter,
            raised=raised,
        ),
        _still_answers(
            "probe_6",
            [p("p6"), p("p6_confirm")],
            ev,
            sources,
            F.MISSING_ENVELOPE_BREAKS_REQUEST,
            ok={200},
            raised=raised,
        ),
        _still_answers(
            "probe_7",
            [p("p7")],
            ev,
            sources,
            F.SDK_ERROR_BREAKS_REQUEST,
            ok=None,
            raised=raised,
        ),
    ]


def routes_digest(routes: Path) -> str:
    return hashlib.sha256(routes.read_bytes()).hexdigest()


async def read_raised(
    app_log: Path, offset: int, routes: Path, steps: Mapping[str, Step | None], file: str
) -> Raised:
    """What the log says the requests answered with a 5xx raised.

    A traceback may reach the log just after the answer did, so when a 5xx
    has no error yet the log is read once more, a moment later.
    """
    failed = [s for s in steps.values() if s is not None and (s.status or 0) >= 500]
    if not failed:
        return Raised(file)
    matched, complete = match_errors(steps, read_handler_errors(app_log, offset, routes))
    if not complete:
        await asyncio.sleep(APP_LOG_SETTLE_S)
        matched, _ = match_errors(steps, read_handler_errors(app_log, offset, routes))
    return Raised(file, matched)


async def run_code_gate(
    link: AppLink,
    visits: Iterable[Mapping[str, Any]],
    *,
    chapter: int,
    routes: Path,
    root: Path,
    app_log: Path | None = None,
) -> GateResult:
    """Run the code gate for ``chapter`` against the app at ``link``.

    ``visits`` come from a ``probe`` lease of ``PROBE_VISITS`` visits with
    ``probe_roles(chapter)``; ``routes`` is the user's ``app/routes.py``
    and ``root`` the repo root the app's recorded call sites are relative to.
    ``app_log`` is the file the app's output goes to (by default
    ``.playground/app.log`` under ``root``); what the probes add to it says
    why a request failed. Never raises for a problem with the app or the
    network: those make the gate NOT RUN.
    """
    sources = Sources(root)
    log_path = app_log if app_log is not None else root / APP_LOG_RELATIVE
    try:
        digest = routes_digest(routes)
        static = check_source(routes, chapter=chapter, sources=sources)
    except (OSError, SourceUnreadable):
        return not_run("code", "routes", needs_proof=True)
    probes = probe_visits(visits, chapter)
    if probes is None:
        return not_run("code", "lease", needs_proof=True)
    try:
        async with connect(link) as (visitor, control):
            health = await control.health()
            reason = not_ready(health)
            if reason is not None:
                return not_run("code", reason, needs_proof=True)
            start = await mark(control, health)
            run = new_run_id("probe")
            offset = log_offset(log_path)
            steps = await _send_probes(visitor, control, probes, chapter, run)
            await drain(control)
            ev = await collect(control, start, run=run)
    except EvidenceUnavailable as unavailable:
        return not_run("code", unavailable.reason, needs_proof=True)
    except (httpx.HTTPError, ControlError):
        return not_run("code", "app", needs_proof=True)
    raised = await read_raised(log_path, offset, routes, steps, sources.relative(routes))
    checks = [
        Check.judged("static", static),
        *judge_probes(steps, ev, sources, chapter=chapter, raised=raised),
        judge_client(ev, sources, chapter=chapter),
    ]
    refs = [s.ref for s in steps.values() if s is not None]
    return GateResult(
        "code",
        tuple(checks),
        accepted=accepted_count(ev, refs),
        needs_proof=True,
        routes_sha256=digest,
    )


__all__ = [
    "ACTION_METHOD",
    "BAD_USER_PREFIX",
    "CHECK_FIELD",
    "CONFIRM_READS_LOG_FIELD_ERROR",
    "LOG_FIELD",
    "NOTHING_RAISED",
    "PROBE_VISITS",
    "SUCCESS_METHOD",
    "ProbeVisit",
    "Raised",
    "Step",
    "dummy_envelope",
    "error_text",
    "judge_check_field",
    "judge_client",
    "judge_log",
    "judge_probes",
    "match_errors",
    "not_ready",
    "probe_roles",
    "probe_visits",
    "raised_site",
    "read_raised",
    "reads_log_field_in_confirm",
    "routes_digest",
    "run_code_gate",
    "send",
    "step",
]
