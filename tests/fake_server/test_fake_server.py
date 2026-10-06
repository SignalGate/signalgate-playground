"""Every answer the fake server gives matches its spec and the committed contract file.

Each call goes through ``_checked``: the status must be one the route lists,
the body must pass ``assert_same_shape`` against the route's spec and
validate against the same route in ``contracts/playground-api.v1.json``,
and a status that must carry a header carries it.
"""

from __future__ import annotations

import ast
import base64
import json
from dataclasses import FrozenInstanceError
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from playground.traffic.plan import REAL_SUCCESS_SHARE, REAL_TO_TOTAL_RATIO
from tests.contract.contracts import CONTRACTS_DIR, PLAYGROUND_API_FILE
from tests.contract.json_schema import assert_valid
from tests.contract.playground_api import (
    ACTION_METHOD,
    ERROR,
    PHASES,
    PROTOCOL_HEADER,
    PROTOCOL_VERSION,
    RETRY_AFTER,
    ROUTES,
    SESSIONS_PATH,
    SUCCESS_METHOD,
)
from tests.contract.samples import INT, NULL, STR, Enum, Obj, assert_same_shape
from tests.fake_server import FakeServer, Script, default_steps, name_only, playground_flow
from tests.fake_server.server import (
    CHECK_PATH,
    ENVELOPE_BYTES,
    IDLE_REPLACE_AFTER,
    LOG_PATH,
    REJECTED,
    ROLES,
)

KEY = "fake-test-key"
OTHER_KEY = "fake-other-key"
THIRD_KEY = "fake-third-key"
API_HEADERS = {PROTOCOL_HEADER: PROTOCOL_VERSION, "Authorization": f"Bearer {KEY}"}
OTHER_HEADERS = {**API_HEADERS, "Authorization": f"Bearer {OTHER_KEY}"}
THIRD_HEADERS = {**API_HEADERS, "Authorization": f"Bearer {THIRD_KEY}"}
SIZE = 40
SESSION_BODY = {"size": SIZE, "client_version": "0.0.1", "consent_version": "c"}

CONTRACT = json.loads((CONTRACTS_DIR / PLAYGROUND_API_FILE).read_text(encoding="utf-8"))

# The public data plane's answers, as the fake gives them.
CHECK_REPLY = Obj(
    {
        "ok": Enum(True),
        "data": {
            "action": Enum("allow", "block", "dry_run_block"),
            "request_id": STR,
            "processing_time_us": INT,
        },
        "error": NULL,
    }
)
LOG_REPLY = Obj({"ok": Enum(True), "data": NULL, "error": NULL})


def _checked(route: str, response) -> Any:
    """The body of ``response``, once it has matched the route's spec and contract entry."""
    status = response.status_code
    assert status in ROUTES[route].responses, f"{route} never answers {status}: {response.text}"
    expected = ROUTES[route].response(status)
    for header in expected.headers:
        assert header in response.headers, f"{route} {status} lacks {header}"
    entry = CONTRACT["routes"][route]["responses"][str(status)]
    if expected.body is None:
        assert response.content == b""
        assert entry["body"] is None
        return None
    body = response.json()
    assert_same_shape(body, expected.body)
    assert_valid(body, entry["body"])
    return body


def _data_plane_checked(response, *, endpoint: str) -> Any:
    body = response.json()
    if response.status_code == 200:
        assert_same_shape(body, CHECK_REPLY if endpoint == "check" else LOG_REPLY)
    else:
        assert_same_shape(body, ERROR)
    return body


def _path(sid: str, suffix: str = "") -> str:
    return f"{SESSIONS_PATH}/{sid}{suffix}"


@pytest.fixture
def fake() -> FakeServer:
    return FakeServer()


@pytest.fixture
def api(fake: FakeServer) -> TestClient:
    return TestClient(fake.app, headers=API_HEADERS)


def _create(api: TestClient, size: int = SIZE, headers: dict[str, str] | None = None) -> dict:
    body = {"size": size, "client_version": "0.0.1", "consent_version": "consent-test-1"}
    response = api.post(SESSIONS_PATH, json=body, headers=headers)
    created = _checked("create_session", response)
    assert response.status_code == 201, response.text
    return created


def _refused(api: TestClient, headers: dict[str, str]) -> dict:
    """The details of a create that another key's open session refuses."""
    response = api.post(SESSIONS_PATH, json=SESSION_BODY, headers=headers)
    body = _checked("create_session", response)
    assert response.status_code == 409, response.text
    assert body["error"]["code"] == "SESSION_ACTIVE"
    assert set(body["error"]["details"]) == {"started_at", "key_prefix"}
    for value in (KEY, OTHER_KEY, THIRD_KEY):
        assert value not in response.text
    return body["error"]["details"]


def _lease(api: TestClient, sid: str, phase: str, count: int, seq: int = 1, **extra) -> dict:
    response = api.post(
        _path(sid, "/leases"), json={"phase": phase, "count": count, "seq": seq, **extra}
    )
    leased = _checked("lease", response)
    assert response.status_code == 200, response.text
    return leased


def _event(visit: dict, role: str, method: str) -> dict:
    return {
        "user_id": visit["user_id"],
        "ip": visit["client_ip"],
        "method": method,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "payload": visit["envelopes"][role],
    }


# --- sessions ---------------------------------------------------------------------


def test_a_new_session_matches_its_spec(api, fake):
    session = _create(api)
    assert session["limits"] == fake.script.limits
    assert session["plan_version"] == fake.script.plan_version
    assert datetime.fromisoformat(session["server_time"]).tzinfo is not None


class _Clock:
    """A clock the test moves by hand, for the fake's session times."""

    def __init__(self) -> None:
        self.now = datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.now


def _calls_on(api: TestClient, sid: str):
    """Every call on one session but the delete, as its key makes them."""
    yield "lease", api.post(_path(sid, "/leases"), json={"phase": "learn", "count": 1, "seq": 9})
    yield "status", api.get(_path(sid))
    telemetry = {"chapter": 3, "finding_ids": []}
    yield "telemetry", api.post(_path(sid, "/telemetry"), json=telemetry)
    yield "complete", api.post(_path(sid, "/complete"), json={"keep_workflow": True})


def test_a_new_session_from_the_same_key_replaces_the_open_one(api, fake):
    first = _create(api)["session_id"]
    _lease(api, first, "learn", 5)
    second = _create(api)["session_id"]
    assert second != first

    for route, response in _calls_on(api, first):
        _checked(route, response)
        assert response.status_code == 404, route
    assert _checked("end_session", api.delete(_path(first))) is None
    _lease(api, second, "learn", 1)
    assert [(s.sid, s.open, s.ended_by) for s in fake.sessions()] == [
        (first, False, "replaced"),
        (second, True, None),
    ]


def test_another_keys_active_session_is_refused_with_its_own_details(fake):
    fake.same_workspace(KEY, OTHER_KEY, THIRD_KEY)
    api = TestClient(fake.app, headers=API_HEADERS)
    first = _create(api)["session_id"]

    # The details describe the open session, whichever key asks.
    asked_by_other = _refused(api, OTHER_HEADERS)
    assert _refused(api, THIRD_HEADERS) == asked_by_other
    [opened] = fake.sessions()
    assert (opened.sid, opened.open) == (first, True)
    assert asked_by_other["started_at"] == opened.started_at.isoformat()

    # Once another key's session is the open one, they describe that one.
    _checked("end_session", api.delete(_path(first)))
    _create(api, headers=OTHER_HEADERS)
    asked_by_key = _refused(api, API_HEADERS)
    assert _refused(api, THIRD_HEADERS) == asked_by_key
    assert asked_by_key["key_prefix"] != asked_by_other["key_prefix"]
    assert [s.open for s in fake.sessions()] == [False, True]


def test_another_keys_session_idle_for_a_day_is_replaced():
    clock = _Clock()
    fake = FakeServer(clock=clock)
    fake.same_workspace(KEY, OTHER_KEY)
    api = TestClient(fake.app, headers=API_HEADERS)
    first = _create(api)["session_id"]

    clock.now += IDLE_REPLACE_AFTER
    _refused(api, OTHER_HEADERS)
    clock.now += timedelta(seconds=1)
    created = _create(api, headers=OTHER_HEADERS)
    second = created["session_id"]

    assert [(s.sid, s.open, s.ended_by) for s in fake.sessions()] == [
        (first, False, "idle"),
        (second, True, None),
    ]
    for headers in (API_HEADERS, OTHER_HEADERS):
        response = api.get(_path(first), headers=headers)
        _checked("status", response)
        assert response.status_code == 404
    # The clock ages sessions only: the server time and lease expiry stay real.
    real_now = datetime.now(timezone.utc)
    assert abs(datetime.fromisoformat(created["server_time"]) - real_now) < timedelta(minutes=1)
    leased = _lease(api, second, "learn", 1)
    assert abs(datetime.fromisoformat(leased["expires_at"]) - real_now) < timedelta(minutes=5)


@pytest.mark.parametrize("activity", ["lease", "telemetry"])
def test_a_lease_or_a_telemetry_report_keeps_a_session_from_going_idle(activity):
    clock = _Clock()
    fake = FakeServer(clock=clock)
    fake.same_workspace(KEY, OTHER_KEY)
    api = TestClient(fake.app, headers=API_HEADERS)
    sid = _create(api)["session_id"]

    clock.now += timedelta(hours=12)
    if activity == "lease":
        _lease(api, sid, "learn", 1)
    else:
        telemetry = {"chapter": 3, "finding_ids": []}
        _checked("telemetry", api.post(_path(sid, "/telemetry"), json=telemetry))
    active_at = clock.now
    clock.now += IDLE_REPLACE_AFTER
    _refused(api, OTHER_HEADERS)
    clock.now += timedelta(seconds=1)
    _create(api, headers=OTHER_HEADERS)
    assert fake.sessions()[0].last_activity == active_at
    assert fake.sessions()[0].ended_by == "idle"


def test_a_status_poll_is_not_activity():
    clock = _Clock()
    fake = FakeServer(clock=clock)
    fake.same_workspace(KEY, OTHER_KEY)
    api = TestClient(fake.app, headers=API_HEADERS)
    sid = _create(api)["session_id"]
    started = clock.now

    clock.now += timedelta(hours=12)
    _checked("status", api.get(_path(sid)))
    assert fake.sessions()[0].last_activity == started
    clock.now = started + IDLE_REPLACE_AFTER + timedelta(seconds=1)
    _create(api, headers=OTHER_HEADERS)
    assert fake.sessions()[0].ended_by == "idle"


def test_keys_in_different_workspaces_never_meet(api, fake):
    other = TestClient(fake.app, headers=OTHER_HEADERS)
    first = _create(api)["session_id"]
    second = _create(other)["session_id"]
    views = fake.sessions()
    assert [s.open for s in views] == [True, True]
    assert views[0].workspace != views[1].workspace

    for client, sid in ((other, first), (api, second)):
        response = client.get(_path(sid))
        _checked("status", response)
        assert response.status_code == 404
    visits = _lease(api, first, "learn", 5)["visits"]
    for visit in visits:
        log = _event(visit, "action_log", ACTION_METHOD)
        assert other.post(LOG_PATH, json=log).status_code == 422
        assert api.post(LOG_PATH, json=log).status_code == 200
    assert _checked("status", api.get(_path(first)))["window"]["action_received"] == len(visits)
    assert _checked("status", other.get(_path(second)))["window"]["action_received"] == 0


def test_sessions_reads_every_session_back_in_the_order_they_opened():
    clock = _Clock()
    fake = FakeServer(clock=clock)
    api = TestClient(fake.app, headers=API_HEADERS)
    started = clock.now
    first = _create(api)["session_id"]
    _lease(api, first, "learn", 5)
    _lease(api, first, "learn", 3, seq=2)
    _lease(api, first, "protect", 4, seq=3, round=1)
    _lease(api, first, "protect", 6, seq=4, round=2)
    clock.now += timedelta(minutes=5)
    _checked("complete", api.post(_path(first, "/complete"), json={"keep_workflow": True}))
    second = _create(api)["session_id"]
    _checked("end_session", api.delete(_path(second)))

    views = fake.sessions()
    assert [(s.sid, s.open, s.ended_by) for s in views] == [
        (first, False, "completed"),
        (second, False, "deleted"),
    ]
    assert dict(views[0].leased) == {**dict.fromkeys(PHASES, 0), "learn": 8, "protect": 10}
    assert dict(views[1].leased) == dict.fromkeys(PHASES, 0)
    assert (views[0].started_at, views[0].last_activity) == (started, clock.now)
    assert views[0].workspace == views[1].workspace
    with pytest.raises(FrozenInstanceError):
        views[0].open = True  # type: ignore[misc]
    with pytest.raises(TypeError):
        views[0].leased["learn"] = 0  # type: ignore[index]


def test_a_session_id_belongs_to_its_workspace(api):
    sid = _create(api)["session_id"]
    for response, route in (
        (api.get(_path(sid), headers=OTHER_HEADERS), "status"),
        (
            api.post(
                _path(sid, "/leases"),
                json={"phase": "learn", "count": 1, "seq": 1},
                headers=OTHER_HEADERS,
            ),
            "lease",
        ),
        (api.delete(_path(sid), headers=OTHER_HEADERS), "end_session"),
        (api.get(_path("0" * 32)), "status"),
    ):
        _checked(route, response)
        assert response.status_code == 404

    # Every key of a workspace can use its session, its envelopes and its counts.
    grouped = FakeServer()
    grouped.same_workspace(KEY, OTHER_KEY)
    mine = TestClient(grouped.app, headers=API_HEADERS)
    theirs = TestClient(grouped.app, headers=OTHER_HEADERS)
    shared = _create(mine)["session_id"]
    visits = _lease(theirs, shared, "learn", 3)["visits"]
    for visit in visits:
        log = _event(visit, "action_log", ACTION_METHOD)
        assert mine.post(LOG_PATH, json=log).status_code == 200
    assert _checked("status", theirs.get(_path(shared)))["window"]["action_received"] == len(visits)


def _every_route_call(api: TestClient, sid: str, headers: dict[str, str]):
    session_body = {"size": SIZE, "client_version": "0.0.1", "consent_version": "c"}
    yield "create_session", api.post(SESSIONS_PATH, json=session_body, headers=headers)
    lease_body = {"phase": "learn", "count": 1, "seq": 1}
    yield "lease", api.post(_path(sid, "/leases"), json=lease_body, headers=headers)
    yield "status", api.get(_path(sid), headers=headers)
    telemetry = {"chapter": 3, "finding_ids": []}
    yield "telemetry", api.post(_path(sid, "/telemetry"), json=telemetry, headers=headers)
    complete = {"keep_workflow": True}
    yield "complete", api.post(_path(sid, "/complete"), json=complete, headers=headers)
    yield "end_session", api.delete(_path(sid), headers=headers)


@pytest.mark.parametrize(
    ("headers", "status"),
    [
        ({PROTOCOL_HEADER: "", "Authorization": f"Bearer {KEY}"}, 426),
        ({PROTOCOL_HEADER: "2", "Authorization": f"Bearer {KEY}"}, 426),
        ({PROTOCOL_HEADER: PROTOCOL_VERSION, "Authorization": ""}, 401),
        ({PROTOCOL_HEADER: PROTOCOL_VERSION, "Authorization": "Bearer "}, 401),
    ],
)
def test_every_route_needs_the_protocol_header_and_a_key(fake, headers, status):
    sid = _create(TestClient(fake.app, headers=API_HEADERS))["session_id"]
    bare = TestClient(fake.app)
    for route, response in _every_route_call(bare, sid, headers):
        body = _checked(route, response)
        assert response.status_code == status, route
        assert body["error"]["code"] == REJECTED


def test_every_request_body_is_checked_against_its_spec(api, fake):
    fake.script.limits["lease_max"] = 5
    for bad in (
        {"size": SIZE, "client_version": "0.0.1"},
        {"size": SIZE, "client_version": "0.0.1", "consent_version": "c", "extra": 1},
        {"size": "40", "client_version": "0.0.1", "consent_version": "c"},
        {"size": 0, "client_version": "0.0.1", "consent_version": "c"},
        {"size": fake.script.limits["max_size"] + 1, "client_version": "x", "consent_version": "c"},
    ):
        response = api.post(SESSIONS_PATH, json=bad)
        _checked("create_session", response)
        assert response.status_code == 400, bad
    sid = _create(api)["session_id"]
    for bad in (
        {"phase": "later", "count": 1, "seq": 1},
        {"phase": "learn", "count": 6, "seq": 1},
        {"phase": "learn", "count": 0, "seq": 1},
        {"phase": "protect", "round": 3, "count": 1, "seq": 1},
        {"phase": "probe", "count": 1, "seq": 1, "probe_roles": "all"},
    ):
        response = api.post(_path(sid, "/leases"), json=bad)
        _checked("lease", response)
        assert response.status_code == 400, bad
    for route, response in (
        ("telemetry", api.post(_path(sid, "/telemetry"), json={"chapter": "3", "finding_ids": []})),
        ("complete", api.post(_path(sid, "/complete"), json={"results": None})),
        ("complete", api.post(_path(sid, "/complete"), content=b"not json")),
        ("status", api.get(_path(sid), params={"since": "yesterday"})),
        ("status", api.get(_path(sid), params={"since": "2026-01-02T03:04:05"})),
    ):
        _checked(route, response)
        assert response.status_code == 400, route


def test_scripted_errors_reach_their_route_once(api, fake):
    script = fake.script
    script.fail_next("create_session", 412, "NO_ACTIVE_PUBLIC_KEY")
    script.fail_next("create_session", 429, "SLOW_DOWN", retry_after_s=7)
    first = api.post(
        SESSIONS_PATH, json={"size": SIZE, "client_version": "x", "consent_version": "c"}
    )
    assert _checked("create_session", first)["error"]["code"] == "NO_ACTIVE_PUBLIC_KEY"
    second = api.post(
        SESSIONS_PATH, json={"size": SIZE, "client_version": "x", "consent_version": "c"}
    )
    _checked("create_session", second)
    assert (second.status_code, second.headers[RETRY_AFTER]) == (429, "7")
    sid = _create(api)["session_id"]

    script.fail_next("lease", 409, "KEY_ROTATED")
    script.fail_next("lease", 409, "PLAN_CHANGED")
    script.fail_next("lease", 429, "SLOW_DOWN", retry_after_s=2)
    body = {"phase": "learn", "count": 1, "seq": 1}
    codes = []
    for _ in range(3):
        response = api.post(_path(sid, "/leases"), json=body)
        codes.append((response.status_code, _checked("lease", response)["error"]["code"]))
    assert codes == [(409, "KEY_ROTATED"), (409, "PLAN_CHANGED"), (429, "SLOW_DOWN")]
    _lease(api, sid, "learn", 1)


# --- leases -------------------------------------------------------------------------


def test_leased_visits_carry_the_envelopes_their_phase_needs(api):
    sid = _create(api)["session_id"]
    learn = _lease(api, sid, "learn", 20, seq=4)
    protect = _lease(api, sid, "protect", 20, seq=5, round=1)
    readiness = _lease(api, sid, "readiness", 5, seq=6)
    probe = _lease(api, sid, "probe", 5, seq=7, probe_roles=["any"])
    assert (learn["seq"], protect["seq"]) == (4, 5)
    assert datetime.fromisoformat(learn["expires_at"]) > datetime.now(timezone.utc)

    visits = learn["visits"] + protect["visits"] + readiness["visits"] + probe["visits"]
    for visit in learn["visits"]:
        assert "action_check" not in visit["envelopes"]
    for visit in protect["visits"] + readiness["visits"] + probe["visits"]:
        assert "action_check" in visit["envelopes"]
    for visit in visits:
        assert ("success_log" in visit["envelopes"]) is visit["script"]["success"]
        if visit["class"] == "attack":
            assert visit["script"]["success"] is False

    envelopes = [e for visit in visits for e in visit["envelopes"].values()]
    assert len({e["nonce"] for e in envelopes}) == len(envelopes)
    assert len({visit["ref"] for visit in visits}) == len(visits)
    assert {len(base64.b64decode(e["encrypted"])) for e in envelopes} == {ENVELOPE_BYTES}


def test_a_probe_lease_gives_real_visits_with_exactly_the_roles_it_names(api):
    sid = _create(api)["session_id"]
    logs_only = _lease(api, sid, "probe", 3, seq=1, probe_roles=["action_log", "success_log"])
    only_action = _lease(api, sid, "probe", 3, seq=2, probe_roles=["action_log"])
    every_role = _lease(api, sid, "probe", 3, seq=3)
    for visit in logs_only["visits"] + only_action["visits"] + every_role["visits"]:
        assert visit["class"] == "real"
        assert ("success_log" in visit["envelopes"]) is visit["script"]["success"]
    assert {tuple(v["envelopes"]) for v in logs_only["visits"]} == {("action_log", "success_log")}
    assert {tuple(v["envelopes"]) for v in only_action["visits"]} == {("action_log",)}
    assert {tuple(v["envelopes"]) for v in every_role["visits"]} == {ROLES}


def test_the_visit_plan_follows_the_public_mix():
    size = 1000
    fake = FakeServer(Script(limits={"lease_max": size}))
    api = TestClient(fake.app, headers=API_HEADERS)
    sid = _create(api, size=size)["session_id"]
    visits = _lease(api, sid, "learn", size)["visits"]
    real = [v for v in visits if v["class"] == "real"]
    real_total = round(size / REAL_TO_TOTAL_RATIO)
    assert len(real) == real_total
    assert sum(v["script"]["success"] for v in real) == round(REAL_SUCCESS_SHARE * real_total)
    assert not any(v["script"]["success"] for v in visits if v["class"] == "attack")


def test_the_plan_is_the_same_for_every_session_and_refs_are_new_each_time(api, fake):
    other = TestClient(fake.app, headers=OTHER_HEADERS)
    first = _lease(api, _create(api)["session_id"], "learn", 20)["visits"]
    second = _lease(other, _create(other)["session_id"], "learn", 20)["visits"]

    def visible(visit: dict) -> tuple:
        return visit["class"], visit["user_id"], visit["client_ip"], visit["script"]["success"]

    assert [visible(v) for v in first] == [visible(v) for v in second]
    assert not {v["ref"] for v in first} & {v["ref"] for v in second}


# --- status --------------------------------------------------------------------------


def test_status_walks_the_scripted_workflow(fake, api):
    fake.script.advance_on_poll = True
    sid = _create(api)["session_id"]
    steps = default_steps()
    seen = [_checked("status", api.get(_path(sid)))["workflow"] for _ in range(len(steps) + 1)]
    assert seen[:-1] == steps
    assert seen[-1] == steps[-1]
    assert [step["state"] for step in steps] == [
        "none",
        "draft",
        "queued",
        "dry_running",
        "dry_running",
        "dry_running",
        "ready",
        "active",
        "active",
        "active",
        "active",
    ]
    assert [step["protection_stage"] for step in steps[-4:]] == [0, 1, 2, 3]


def test_a_name_only_match_reports_state_and_nothing_else():
    fake = FakeServer(Script([name_only("active")]))
    api = TestClient(fake.app, headers=API_HEADERS)
    workflow = _checked("status", api.get(_path(_create(api)["session_id"])))["workflow"]
    assert workflow["state"] == "active"
    assert workflow["methods_match"] is False
    others = set(workflow) - {"state", "methods_match"}
    assert all(workflow[key] is None for key in others)


def test_window_and_stored_since_count_the_logs_this_fake_accepted(api):
    started = datetime.now(timezone.utc) - timedelta(seconds=1)
    sid = _create(api)["session_id"]
    status = _checked("status", api.get(_path(sid)))
    assert status["window"] == {"action_received": 0, "success_received": 0}
    assert status["stored_since"] is None

    visits = _lease(api, sid, "learn", 20)["visits"]
    successes = [v for v in visits if "success_log" in v["envelopes"]]
    for visit in visits:
        response = api.post(LOG_PATH, json=_event(visit, "action_log", ACTION_METHOD))
        _data_plane_checked(response, endpoint="log")
    for visit in successes:
        api.post(LOG_PATH, json=_event(visit, "success_log", SUCCESS_METHOD))

    status = _checked("status", api.get(_path(sid), params={"since": started.isoformat()}))
    counts = {"action_received": len(visits), "success_received": len(successes)}
    assert status["window"] == counts
    assert status["stored_since"] == {"action": len(visits), "success": len(successes)}
    later = (datetime.now(timezone.utc) + timedelta(minutes=1)).isoformat()
    status = _checked("status", api.get(_path(sid), params={"since": later}))
    assert status["stored_since"] == {"action": 0, "success": 0}


# --- check and log ------------------------------------------------------------------


def _one_of_each(visits: list[dict]) -> tuple[dict, dict]:
    real = next(v for v in visits if v["class"] == "real")
    attack = next(v for v in visits if v["class"] == "attack")
    return real, attack


def test_check_verdicts_follow_the_script(api, fake):
    sid = _create(api)["session_id"]
    visits = _lease(api, sid, "protect", 20, round=1)["visits"]
    real, attack = _one_of_each(visits)

    def verdict(visit: dict) -> str:
        response = api.post(CHECK_PATH, json=_event(visit, "action_check", ACTION_METHOD))
        return _data_plane_checked(response, endpoint="check")["data"]["action"]

    assert verdict(attack) == "allow"
    fake.script.go_to("active", protection_stage=1)
    real, attack = _one_of_each(_lease(api, sid, "protect", 20, seq=2, round=1)["visits"])
    assert (verdict(real), verdict(attack)) == ("allow", "block")
    fake.script.set_workflow(
        playground_flow("active", protecting=True, protection_stage=3, action="dry_run_block")
    )
    _, attack = _one_of_each(_lease(api, sid, "protect", 20, seq=3, round=1)["visits"])
    assert verdict(attack) == "dry_run_block"
    assert [(d.cls, d.role, d.verdict) for d in fake.delivered("check")] == [
        ("attack", "action_check", "allow"),
        ("real", "action_check", "allow"),
        ("attack", "action_check", "block"),
        ("attack", "action_check", "dry_run_block"),
    ]


def test_every_envelope_works_once_and_only_for_its_own_key(api, fake):
    sid = _create(api)["session_id"]
    visit = _lease(api, sid, "protect", 1, round=1)["visits"][0]
    check = _event(visit, "action_check", ACTION_METHOD)
    assert api.post(CHECK_PATH, json=check).status_code == 200
    again = api.post(CHECK_PATH, json=check)
    assert again.status_code == 422
    assert _data_plane_checked(again, endpoint="check")["error"]["code"] == REJECTED

    log = _event(visit, "action_log", ACTION_METHOD)
    other = TestClient(fake.app, headers=OTHER_HEADERS)
    assert other.post(LOG_PATH, json=log).status_code == 422
    altered = {**log, "payload": {**log["payload"], "encrypted": "AAAA"}}
    assert api.post(LOG_PATH, json=altered).status_code == 422
    unknown = {**log, "payload": {**log["payload"], "nonce": "not-leased"}}
    assert api.post(LOG_PATH, json=unknown).status_code == 422
    assert api.post(LOG_PATH, content=b"not json").status_code == 422
    accepted = api.post(LOG_PATH, json=log)
    assert _data_plane_checked(accepted, endpoint="log") == {
        "ok": True,
        "data": None,
        "error": None,
    }
    assert [d.status for d in fake.delivered()] == [200, 422, 422, 422, 422, 422, 200]


def test_check_and_log_need_a_key_and_honour_scripted_errors(fake):
    bare = TestClient(fake.app)
    response = bare.post(CHECK_PATH, json={})
    assert response.status_code == 401
    _data_plane_checked(response, endpoint="check")
    fake.script.fail_next("log", 503, "TEST_CODE")
    response = TestClient(fake.app, headers=API_HEADERS).post(LOG_PATH, json={})
    assert response.status_code == 503
    assert _data_plane_checked(response, endpoint="log")["error"]["code"] == "TEST_CODE"


# --- the end of a session ---------------------------------------------------------------


def test_telemetry_complete_and_delete(api, fake):
    fake.script.go_to("active", protection_stage=3)
    sid = _create(api)["session_id"]
    telemetry = {"chapter": 4, "finding_ids": ["WRONG_METHOD"]}
    assert _checked("telemetry", api.post(_path(sid, "/telemetry"), json=telemetry)) is None
    assert fake.telemetry == [{"session_id": sid, **telemetry}]

    complete = {"keep_workflow": False, "results": {"conv_before": 0.2, "conv_after": 0.6}}
    assert _checked("complete", api.post(_path(sid, "/complete"), json=complete)) is None
    assert fake.script.workflow()["state"] == "paused"
    assert fake.script.workflow()["protecting"] is False
    closed = api.get(_path(sid))
    _checked("status", closed)
    assert closed.status_code == 404
    assert _checked("end_session", api.delete(_path(sid))) is None

    kept = FakeServer()
    kept.script.go_to("active", protection_stage=3)
    kept_api = TestClient(kept.app, headers=API_HEADERS)
    kept_sid = _create(kept_api)["session_id"]
    _checked("complete", kept_api.post(_path(kept_sid, "/complete"), json={"keep_workflow": True}))
    assert kept.script.workflow()["state"] == "active"


def test_ending_a_session_never_touches_the_workflow(api, fake):
    fake.script.go_to("active", protection_stage=2)
    sid = _create(api)["session_id"]
    before = fake.script.workflow()
    _checked("end_session", api.delete(_path(sid)))
    assert fake.script.workflow() == before


def test_keep_false_leaves_a_name_only_workflow_alone():
    fake = FakeServer(Script([name_only("active")]))
    api = TestClient(fake.app, headers=API_HEADERS)
    sid = _create(api)["session_id"]
    _checked("complete", api.post(_path(sid, "/complete"), json={"keep_workflow": False}))
    assert fake.script.workflow() == name_only("active")


# --- the fake stays a test fixture --------------------------------------------------------


def test_the_fake_never_reads_the_environment():
    package = Path(__file__).resolve().parent
    for path in sorted(package.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute):
                assert node.attr not in ("environ", "getenv", "environb"), path.name
            if isinstance(node, ast.Name):
                assert node.id not in ("environ", "getenv"), path.name
            if isinstance(node, ast.ImportFrom) and node.module == "os":
                assert not {a.name for a in node.names} & {"environ", "getenv"}, path.name
