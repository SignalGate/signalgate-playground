"""The runner's online steps (``playground.online``) against the fake, in process.

A stub stands in for the runner's session: it holds the machine's session
and records what would be shown. The playground API client reaches the fake
through its transport. The steps that drive the sample app are covered end
to end in ``tests/e2e``; here: the key check, opening a session (every
error it maps, the clock check), a session that was closed under the
console, the workflow waits, reading the stage, completing, telemetry, the
support bundle, and a protected round's numbers from recorded evidence.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from playground import CLIENT_VERSION
from playground import online as online_module
from playground.api import key as api_key
from playground.api.client import CONSENT_VERSION, PROTOCOL_HEADER, PROTOCOL_VERSION
from playground.copy.en import text
from playground.machine.events import (
    Complete,
    Gate,
    Phase,
    Prediction,
    ReadStage,
    RunGate,
    Say,
    StepDone,
    WaitWorkflow,
)
from playground.online import Online, round_stats
from playground.traffic.driver import RunResult
from playground.verify import GateResult, posted_counts
from playground.verify.evidence import Mark
from playground.verify.model import Check, Finding, Verdict
from tests.contract.playground_api import SESSIONS_PATH
from tests.fake_server import FakeServer, Script, name_only, no_workflow, playground_flow
from tests.fake_server.script import DEFAULT_LIMITS, PREDICTION
from tests.verify.recordings import (
    accepted_logs,
    action_entry,
    check_attempt,
    check_call,
    evidence,
)

KEY = api_key.KEY_PREFIX + "Onl1" * 8


class _Machine:
    def __init__(self) -> None:
        self.session: dict[str, Any] | None = None
        self.chapter = 5

    def to_dict(self) -> dict[str, Any]:
        return {
            "step": "CODE5",
            "running": False,
            "session": self.session,
            "stage": 1,
            "learn": None,
            "prediction": None,
            "rounds": [],
        }


class _App:
    alive = False
    control = None
    number = 2
    boot = None


class _Session:
    """Just enough of the runner's session for the steps that don't drive the app."""

    def __init__(self) -> None:
        self.machine = _Machine()
        self.app = _App()
        self.shown: list[Say] = []
        self.waits: list[Say] = []
        self.restarted = 0

    def show(self, event: Say) -> None:
        self.shown.append(event)

    def wait_show(self, event: Say) -> None:
        self.waits.append(event)

    def wait_end(self) -> None:
        pass

    async def restart_with_key(self) -> None:
        self.restarted += 1


@pytest.fixture
def root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.delenv(api_key.KEY_NAME, raising=False)
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / ".env").write_text(f"SIGNALGATE_API_KEY={KEY}\n")
    return tmp_path


def _online(root: Path, fake: FakeServer, *, telemetry: bool = True) -> tuple[Online, _Session]:
    session = _Session()
    online = Online(
        session,  # type: ignore[arg-type]
        root=root,
        api_transport=httpx.ASGITransport(app=fake.app),
        telemetry=telemetry,
    )
    return online, session


def _keys(done: StepDone) -> list[str]:
    return [say.key for say in done.says]


def _opened(root: Path, fake: FakeServer, **kwargs) -> tuple[Online, _Session]:
    online, session = _online(root, fake, **kwargs)
    done = asyncio.run(online.open_session(1100))
    assert done.passed, done
    session.machine.session = dict(done.session)
    return online, session


# -- chapter 2 ---------------------------------------------------------------------------


def test_the_key_check_names_the_problem_and_skips_git_outside_a_checkout(root):
    online, _ = _online(root, FakeServer())
    done = asyncio.run(online.check_key())
    assert done.passed and _keys(done) == ["ch2.key.ok", "ch2.git.no_git"]

    (root / "app" / ".env").write_text("SIGNALGATE_API_KEY=" + "x" * 43 + "\n")
    done = asyncio.run(online.check_key())
    assert not done.passed and _keys(done) == ["ch2.key.public_key"] and done.exit_code == 3


def test_opening_a_session_restarts_the_app_and_hands_the_session_over(root):
    fake = FakeServer()
    online, session = _online(root, fake)
    done = asyncio.run(online.open_session(1100))
    assert done.passed
    assert set(done.session) == {"session_id", "size", "limits"}
    assert done.session["size"] == 1100
    assert [say.key for say in session.shown] == ["ch2.session.ok"]
    assert session.restarted == 1


@pytest.mark.parametrize(
    ("status", "code", "details", "key", "exit_code"),
    [
        (401, "REQUEST_REJECTED", None, "ch2.session.bad_key", 3),
        (409, "SESSION_ACTIVE", {"started_at": "t", "key_prefix": "k1"}, "ch2.session.active", 3),
        (412, "NO_ACTIVE_PUBLIC_KEY", None, "ch2.session.no_public_key", 3),
        (429, "REQUEST_REJECTED", None, "ch2.session.busy", 4),
        (400, "REQUEST_REJECTED", None, "ch2.session.rejected", 2),
    ],
)
def test_every_session_error_has_its_words(root, status, code, details, key, exit_code):
    fake = FakeServer()
    fake.script.fail_next("create_session", status, code, details=details, retry_after_s=1)
    online, session = _online(root, fake)
    done = asyncio.run(online.open_session(1100))
    assert not done.passed and _keys(done) == [key] and done.exit_code == exit_code
    assert session.restarted == 0
    if key == "ch2.session.active":
        assert done.says[0].params == {"started_at": "t", "key_prefix": "k1"}


def test_a_426_says_git_pull_and_exits_3(root):
    fake = FakeServer()
    fake.script.fail_next("create_session", 426, "REQUEST_REJECTED")
    online, _ = _online(root, fake)
    done = asyncio.run(online.open_session(1100))
    assert (done.fatal, done.exit_code, _keys(done)) == (True, 3, ["online.outdated"])


def test_a_clock_far_off_ends_the_session_and_says_so(root, monkeypatch):
    fake = FakeServer()
    online, _ = _online(root, fake)
    real_time = __import__("time").time
    off = DEFAULT_LIMITS["clock_block_s"] + 5
    monkeypatch.setattr("playground.online.time.time", lambda: real_time() + off)
    done = asyncio.run(online.open_session(1100))
    assert not done.passed and _keys(done) == ["ch2.clock.block"]
    assert done.says[0].params["skew"] == pytest.approx(off, abs=2)
    monkeypatch.undo()
    # The session it opened was ended, so a new one opens.
    assert asyncio.run(_online(root, fake)[0].open_session(1100)).passed


def test_a_clock_somewhat_off_warns_and_goes_on(root, monkeypatch):
    fake = FakeServer()
    online, session = _online(root, fake)
    real_time = __import__("time").time
    off = (DEFAULT_LIMITS["clock_warn_s"] + DEFAULT_LIMITS["clock_block_s"]) / 2
    monkeypatch.setattr("playground.online.time.time", lambda: real_time() + off)
    assert asyncio.run(online.open_session(1100)).passed
    assert [say.key for say in session.shown] == ["ch2.session.ok", "ch2.clock.warn"]


# -- the 409 for another key's session ---------------------------------------------------------


@pytest.fixture
def away_from_utc(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """This computer's clock in a zone well away from UTC, where the system can set one."""
    if not hasattr(time, "tzset"):
        yield
        return
    monkeypatch.setenv("TZ", "Asia/Kathmandu")
    time.tzset()
    try:
        yield
    finally:
        monkeypatch.undo()
        time.tzset()


def _refused_with(root: Path, started_at: str) -> str:
    fake = FakeServer()
    details = {"started_at": started_at, "key_prefix": "pk_live_ab12"}
    fake.script.fail_next("create_session", 409, "SESSION_ACTIVE", details=details)
    online, session = _online(root, fake)
    done = asyncio.run(online.open_session(1100))
    assert not done.passed and _keys(done) == ["ch2.session.active"] and done.exit_code == 3
    assert session.restarted == 0
    (say,) = done.says
    assert all(isinstance(value, str) for value in say.params.values())
    return text(say.key, **dict(say.params))


def test_the_409_names_the_other_session_and_how_it_closes(root, away_from_utc):
    instant = "2026-10-02T12:05:00.123456+00:00"
    local = datetime.fromisoformat(instant).astimezone()
    words = f"at {local:%H:%M} on {local:%Y-%m-%d}"
    shown = _refused_with(root, instant)
    assert f"It started {words} with the key that starts pk_live_ab12." in shown
    assert "If that's you on another computer, finish it there." in shown
    assert "Otherwise it closes by itself after a day without activity." in shown
    assert "/quit" not in shown
    if hasattr(time, "tzset"):
        assert "12:05" not in shown, "the start is shown in this computer's local time"


def test_the_409_keeps_a_start_it_cant_read_and_reads_a_z_suffix(root, away_from_utc):
    assert "It started t with the key" in _refused_with(root, "t")
    assert "It started 2026-10-02T12:05:00 with the key" in _refused_with(
        root, "2026-10-02T12:05:00"
    )
    local = datetime.fromisoformat("2026-10-02T12:05:00+00:00").astimezone()
    shown = _refused_with(root, "2026-10-02T12:05:00Z")
    assert f"It started at {local:%H:%M} on {local:%Y-%m-%d} with the key" in shown


# -- a session closed under the console ----------------------------------------------------------


class _Recording(httpx.AsyncBaseTransport):
    """The fake's transport, noting the body of every session the console asks for."""

    def __init__(self, fake: FakeServer) -> None:
        self._inner = httpx.ASGITransport(app=fake.app)
        self.created: list[dict[str, Any]] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path == SESSIONS_PATH:
            self.created.append(json.loads(request.content))
        return await self._inner.handle_async_request(request)


def _new_session_from_the_same_key(fake: FakeServer) -> str:
    """Open a session with the console's key, as a second console would; its id."""
    client = TestClient(
        fake.app, headers={PROTOCOL_HEADER: PROTOCOL_VERSION, "Authorization": f"Bearer {KEY}"}
    )
    body = {"size": 1100, "client_version": CLIENT_VERSION, "consent_version": CONSENT_VERSION}
    response = client.post(SESSIONS_PATH, json=body)
    assert response.status_code == 201, response.text
    return response.json()["session_id"]


def test_a_closed_session_is_reported_as_closed_not_as_words(root):
    fake = FakeServer(Script([playground_flow("active", prediction=PREDICTION)]))
    online, session = _opened(root, fake)
    _new_session_from_the_same_key(fake)
    closed = StepDone(passed=False, says=(), session_closed=True)
    assert asyncio.run(online.handle(WaitWorkflow(Gate.G2))) == closed
    assert asyncio.run(online.handle(ReadStage())) == closed
    assert [view.ended_by for view in fake.sessions()] == ["replaced", None]
    assert fake.sessions()[0].sid == session.machine.session["session_id"]


class _Boot:
    base_url = "http://127.0.0.1:9"
    token = "t"


class _GateApp(_App):
    boot = _Boot()

    @contextlib.asynccontextmanager
    async def hold(self):
        yield None


@pytest.mark.parametrize("chapter", [3, 5])
def test_the_code_gates_probe_lease_passes_a_closed_session_on(root, chapter):
    fake = FakeServer()
    online, session = _opened(root, fake)
    session.app = _GateApp()
    fake.script.fail_next("lease", 404, "SESSION_NOT_FOUND")
    assert asyncio.run(online.handle(RunGate(chapter))) == StepDone(False, session_closed=True)


@pytest.mark.parametrize(("status", "code"), [(409, "PLAN_CHANGED"), (400, "REQUEST_REJECTED")])
def test_any_other_probe_lease_error_is_a_check_that_didnt_run(root, status, code):
    fake = FakeServer()
    online, session = _opened(root, fake)
    session.app = _GateApp()
    fake.script.fail_next("lease", status, code)
    done = asyncio.run(online.handle(RunGate(3)))
    assert not done.passed and not done.session_closed
    lines = [say.params["line"] for say in done.says if say.key == "verify.line"]
    assert lines and lines[0].startswith(text("mark.not_run")), lines


def test_a_replaced_session_reuses_the_workflow_already_there(root):
    fake = FakeServer(Script([playground_flow("active", prediction=PREDICTION)]))
    transport = _Recording(fake)
    session = _Session()
    online = Online(
        session,  # type: ignore[arg-type]
        root=root,
        api_transport=transport,
        telemetry=True,
    )
    first = asyncio.run(online.open_session(1100))
    assert first.passed
    session.machine.session = dict(first.session)
    _new_session_from_the_same_key(fake)
    assert asyncio.run(online.handle(WaitWorkflow(Gate.G2))).session_closed

    # The carry-on: a new session, with the same consent version, and the
    # workflow the first session made is still there for chapter 4.
    again = asyncio.run(online.open_session(1100))
    assert again.passed
    session.machine.session = dict(again.session)
    dry_run = asyncio.run(online.handle(WaitWorkflow(Gate.G2)))
    assert dry_run.passed and dry_run.prediction == Prediction(**PREDICTION)
    assert asyncio.run(online.handle(WaitWorkflow(Gate.G3))).passed
    assert [body["consent_version"] for body in transport.created] == [CONSENT_VERSION] * 2
    views = fake.sessions()
    assert [view.ended_by for view in views] == ["replaced", "replaced", None]
    assert views[2].sid == again.session["session_id"]


# -- the workflow ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("steps", "gate", "keys"),
    [
        ([name_only("draft")], Gate.G2, ["wf.not_playground"]),
        ([playground_flow("draft", method_ok=False)], Gate.G2, ["wf.method_wrong"]),
        (
            [playground_flow("draft", method_ok=False, target_ok=False)],
            Gate.G2,
            ["wf.method_wrong", "wf.target_wrong"],
        ),
        (
            [playground_flow("active", action="dry_run_block", prediction=PREDICTION)],
            Gate.G3,
            ["wf.not_block", "ch4.why_block"],
        ),
        ([playground_flow("paused", prediction=PREDICTION)], Gate.G3, ["wf.paused"]),
    ],
)
def test_a_workflow_that_needs_the_user_hands_the_question_back_at_once(root, steps, gate, keys):
    online, _ = _opened(root, FakeServer(Script(steps)))

    async def run() -> StepDone:
        api = online._api()
        async with api:
            return await online.wait_workflow(api, gate)

    done = asyncio.run(run())
    assert not done.passed and _keys(done) == keys


def test_the_dry_run_wait_mirrors_the_dashboard_and_freezes_the_prediction(root, monkeypatch):
    monkeypatch.setattr("playground.online.WORKFLOW_POLL_S", 0)
    steps = [
        playground_flow("queued"),
        playground_flow("dry_running", stage=1),
        playground_flow("dry_running", stage=2),
        playground_flow("ready", prediction=PREDICTION),
    ]
    online, session = _opened(root, FakeServer(Script(steps, advance_on_poll=True)))

    async def run() -> StepDone:
        api = online._api()
        async with api:
            return await online.wait_workflow(api, Gate.G2)

    done = asyncio.run(run())
    assert done.passed and done.prediction.conv_pred == PREDICTION["conv_pred"]
    assert [say.key for say in session.waits] == [
        "wait.queued",
        "wait.dry_running",
        "wait.dry_running",
        "wait.ready",
    ]


def test_no_workflow_yet_is_waited_on_only_for_a_while(root, monkeypatch):
    monkeypatch.setattr("playground.online.WORKFLOW_POLL_S", 0)
    monkeypatch.setattr("playground.online.WORKFLOW_NUDGE_S", 0)
    online, _ = _opened(root, FakeServer(Script([no_workflow()])))

    async def run() -> StepDone:
        api = online._api()
        async with api:
            return await online.wait_workflow(api, Gate.G2)

    done = asyncio.run(run())
    assert not done.passed and _keys(done) == ["wf.no_workflow"]


def test_reading_the_stage_puts_the_state_and_the_frozen_line_into_words(root):
    script = Script([playground_flow("active", protecting=True, protection_stage=2)])
    online, _ = _opened(root, FakeServer(script))

    async def run() -> StepDone:
        api = online._api()
        async with api:
            return await online.read_stage(api)

    done = asyncio.run(run())
    assert done.stage == 2 and not done.paused
    assert _keys(done) == ["status.workflow", "stage.partial"]


# -- completing, telemetry -----------------------------------------------------------------------


@pytest.mark.parametrize("telemetry", [True, False])
def test_complete_sends_the_results_only_with_telemetry(root, telemetry):
    fake = FakeServer()
    online, _ = _opened(root, fake, telemetry=telemetry)

    async def run() -> StepDone:
        api = online._api()
        async with api:
            return await online.complete(api, Complete(False, 0.2, 0.7))

    done = asyncio.run(run())
    assert _keys(done) == ["ch6.paused"]
    expected = {"conv_before": 0.2, "conv_after": 0.7} if telemetry else None
    assert fake.completions[0].get("results") == expected
    assert fake.completions[0]["keep_workflow"] is False


def _failed_gate() -> GateResult:
    findings = (
        Finding("SUCCESS_LOG_ON_FAILURE"),
        Finding("DUPLICATE_DELIVERY", blocks=False),
    )
    return GateResult("code", (Check("probe_4", Verdict.FAIL, findings),))


@pytest.mark.parametrize("telemetry", [True, False])
def test_telemetry_carries_the_failed_finding_ids_only(root, telemetry):
    fake = FakeServer()
    online, session = _opened(root, fake, telemetry=telemetry)

    async def run() -> None:
        api = online._api()
        async with api:
            await online._report_findings(api, 3, _failed_gate())

    asyncio.run(run())
    sid = session.machine.session["session_id"]
    expected = [{"session_id": sid, "chapter": 3, "finding_ids": ["SUCCESS_LOG_ON_FAILURE"]}]
    assert fake.telemetry == (expected if telemetry else [])


# -- /report ---------------------------------------------------------------------------------------


def test_the_support_bundle_holds_no_key_no_path_and_no_envelope(root):
    fake = FakeServer()
    online, _ = _opened(root, fake)

    async def run() -> StepDone:
        api = online._api()
        async with api:
            await online._report_findings(api, 3, _failed_gate())
        return await online.write_report()

    done = asyncio.run(run())
    assert _keys(done) == ["report.written"]
    raw = (root / ".playground" / "report.json").read_text()
    bundle = json.loads(raw)
    assert bundle["findings"] == ["SUCCESS_LOG_ON_FAILURE", "DUPLICATE_DELIVERY"]
    assert bundle["session_open"] is True
    assert KEY not in raw
    assert str(root) not in raw and "/" not in raw.replace("\\/", "")
    assert "encrypted" not in raw and "nonce" not in raw


# -- a protected round's numbers, from recorded evidence ------------------------------------------


def test_round_stats_count_real_users_blocked_and_the_median_check_time():
    visits = [
        {"ref": "real-1", "class": "real"},
        {"ref": "real-2", "class": "real"},
        {"ref": "attack-1", "class": "attack"},
    ]
    records = [
        check_call(1, "real-1", action="allow", t0=10.0, t1=10.010, processing_time_us=1000),
        check_attempt(1, verdict="allow"),
        check_call(2, "real-2", action="block", t0=11.0, t1=11.030, processing_time_us=3000),
        check_attempt(2, verdict="block"),
        check_call(3, "attack-1", action="block", t0=12.0, t1=12.020, processing_time_us=2000),
        check_attempt(3, verdict="block"),
        check_call(4, "real-1", outcome="failed_open", t0=13.0, t1=16.0),
    ]
    ledger = [action_entry(5, "real-1")]
    stats = round_stats(visits, evidence(records, ledger), actions=1, successes=1)
    assert (stats.real_blocked, stats.real_total, stats.total) == (1, 2, 3)
    assert stats.check_ms == pytest.approx(20.0)
    assert stats.inside_ms == pytest.approx(2.0)


# -- a run that didn't go smoothly ------------------------------------------------------------------


class _Control:
    async def metrics(self) -> dict[str, int]:
        return {}


class _Held:
    async def intact(self) -> bool:
        return True


class _RunApp(_App):
    control = _Control()

    @contextlib.asynccontextmanager
    async def hold(self):
        yield _Held()


class _RunSession(_Session):
    """The session, with what a traffic run needs; the run itself is played by the test."""

    def __init__(self) -> None:
        super().__init__()
        self.app = _RunApp()
        self.stop_run = asyncio.Event()

    def progress(self, _event: object) -> None:
        pass

    def panel_begin(self, _total: int, _title: str) -> None:
        pass

    def panel_end(self) -> None:
        pass

    async def as_run(self, awaitable, _held):
        return await awaitable

    async def drain_after_run(self) -> None:
        pass


ACTIONS, SUCCESSES = 100, 30


def _run_through(
    root,
    monkeypatch,
    phase: Phase,
    *,
    cut_short: bool,
    records,
    fake: FakeServer | None = None,
    stored_counts: dict[str, int] | None = None,
) -> tuple[StepDone, Online]:
    """One traffic run whose pacing and evidence are given; the rest is the real step.

    SignalGate reports every event stored, unless ``stored_counts`` says otherwise.
    """
    fake = fake or FakeServer()
    online, stub = _opened(root, fake)
    session = _RunSession()
    session.machine.session = stub.machine.session
    online._s = session  # type: ignore[assignment]
    ev = evidence(records)

    async def paced(self, api, control, *, paced, **_kwargs) -> RunResult:
        paced.degraded = cut_short
        return RunResult(
            total=ACTIONS,
            done=ACTIONS,
            actions=ACTIONS,
            successes=SUCCESSES,
            attack=0,
            real=0,
            stopped=False,
        )

    async def mark(_control) -> Mark:
        return Mark("boot-recorded", 0, {}, 1)

    async def collect(_control, _start, *, run: str):
        return ev

    async def stored(_fetch, _posted, _wait_s):
        return stored_counts or posted_counts(ev)

    monkeypatch.setattr(Online, "_paced", paced)
    monkeypatch.setattr(online_module, "mark", mark)
    monkeypatch.setattr(online_module, "collect", collect)
    monkeypatch.setattr(online_module, "wait_for_stored", stored)

    async def run() -> StepDone:
        api = online._api()
        async with api:
            return await online.run_traffic(api, phase, ACTIONS, 1)

    return asyncio.run(run()), online


def _logs() -> list:
    return accepted_logs(ACTIONS) + accepted_logs(
        SUCCESSES, method="playground.success", start=ACTIONS + 1
    )


def _checks(failed_open: int) -> list:
    return [
        check_call(5000 + n, f"ref-c{n}", outcome="failed_open" if n < failed_open else "verdict")
        for n in range(ACTIONS)
    ]


@pytest.mark.parametrize("phase", [Phase.LEARN, Phase.PROTECT])
def test_a_run_cut_short_by_the_pacer_is_marked_degraded_and_says_so(root, monkeypatch, phase):
    done, _ = _run_through(root, monkeypatch, phase, cut_short=True, records=_logs())
    assert done.passed, done
    assert _keys(done)[-1] == "run.degraded"
    assert done.stats is not None and done.stats.degraded is True


def test_a_round_with_too_many_checks_failing_open_is_marked_degraded(root, monkeypatch):
    records = _logs() + _checks(failed_open=2)
    done, _ = _run_through(root, monkeypatch, Phase.PROTECT, cut_short=False, records=records)
    assert done.passed, done
    assert _keys(done)[-1] == "run.degraded"
    assert done.stats is not None and done.stats.degraded is True


def test_a_smooth_run_carries_no_mark(root, monkeypatch):
    records = _logs() + _checks(failed_open=1)
    done, _ = _run_through(root, monkeypatch, Phase.PROTECT, cut_short=False, records=records)
    assert done.passed, done
    assert "run.degraded" not in _keys(done)
    assert done.stats is not None and done.stats.degraded is False


def test_events_still_not_stored_after_the_wait_are_not_run_and_reported(root, monkeypatch):
    fake = FakeServer()
    short = {"action": ACTIONS // 2, "success": SUCCESSES}
    done, online = _run_through(
        root,
        monkeypatch,
        Phase.PROTECT,
        cut_short=False,
        records=_logs(),
        fake=fake,
        stored_counts=short,
    )
    assert not done.passed
    lines = [say.params["line"] for say in done.says if say.key == "verify.line"]
    assert lines[0].startswith(text("mark.not_run"))
    assert not any(line.startswith(text("mark.fail")) for line in lines), lines
    # The ID still reaches telemetry and the support bundle.
    assert [t["finding_ids"] for t in fake.telemetry] == [["EVENTS_NOT_STORED"]]
    assert asyncio.run(online.write_report()).passed
    bundle = json.loads((root / ".playground" / "report.json").read_text())
    assert bundle["findings"] == ["EVENTS_NOT_STORED"]
