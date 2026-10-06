"""The runner's side of chapters 2-6: each online effect, carried out.

``Online.handle(effect)`` does what one effect from ``playground.machine``
asks (read the key, open the session, run a gate, send a run, watch the
workflow, ...) and answers with the ``StepDone`` the machine turns into the
next question. What it finds goes into words here, as copy keys; the
machine adds the chapter's own narration.

Everything the console knows about a run comes from what it sent, the
session's answers and what the sample app recorded (``playground.verify``).
It talks to the playground API through ``PlaygroundApi`` only, and never to
SignalGate's check and log endpoints: the sample app does that.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import platform
import statistics
import time
from collections.abc import Iterable, Mapping
from datetime import datetime, timezone
from pathlib import Path
from tempfile import mkstemp
from typing import TYPE_CHECKING, Any

import httpx

from playground import CLIENT_VERSION
from playground.api import key as api_key
from playground.api.client import (
    CONSENT_VERSION,
    ApiError,
    ApiUnreachable,
    PlaygroundApi,
    ProtocolOutdated,
)
from playground.clipboard import copy_to_clipboard
from playground.copy.en import TASKS, snippet_text, text
from playground.machine.events import (
    CheckKey,
    Complete,
    CopySnippets,
    EndSession,
    Event,
    Gate,
    OpenSession,
    Phase,
    Prediction,
    ReadStage,
    RunGate,
    RunProgress,
    RunReadiness,
    RunStats,
    RunTraffic,
    Say,
    StepDone,
    WaitWorkflow,
    WriteReport,
)
from playground.machine.steps import stage_say
from playground.progress import eta_seconds
from playground.supervisor.control_client import (
    KEY_DROPPED_RETRY,
    KEY_ENQUEUED,
    KEY_SENT,
    ControlClient,
    ControlError,
)
from playground.supervisor.launcher import AppUnavailable
from playground.traffic.local_constants import (
    DRAIN_MAX_S,
    STATUS_READ_FAILURES_MAX,
    WORKFLOW_NUDGE_S,
    WORKFLOW_POLL_S,
)
from playground.traffic.pacer import DrainMeter, LeaseStopped, PacedRun
from playground.verify import (
    PROBE_VISITS,
    AppLink,
    EvidenceUnavailable,
    GateResult,
    Verdict,
    collect,
    data_gate,
    gate_g1,
    gate_g2,
    gate_g3,
    gate_g4,
    gate_lines,
    mark,
    new_run_id,
    posted_counts,
    probe_roles,
    readiness,
    readiness_next,
    run_code_gate,
    run_rehearsal,
    tripwire,
    wait_for_stored,
)
from playground.verify import workflow as wf
from playground.verify.hygiene import check_hygiene
from playground.verify.model import not_run

if TYPE_CHECKING:
    from playground.runner import _Session

# One readiness round: this many visitors through the gated app.
READINESS_VISITORS = 80

_ROUTES = Path("app") / "routes.py"
_REPORT = Path(".playground") / "report.json"
# Where /copy writes the code when no clipboard tool takes it.
_SNIPPETS = Path(".playground") / "snippets"

# The playground API's names for the kinds of run.
_PHASE_NAMES = {Phase.LEARN: "learn", Phase.PROTECT: "protect"}
_GATES = {Gate.G2: gate_g2, Gate.G3: gate_g3, Gate.G4: gate_g4}

# Reasons a workflow gate keeps waiting on, without asking you anything.
_WAIT_FREELY = frozenset({"dry_run_pending"})
# Reasons it waits on for a while, then hands the question back.
_WAIT_A_WHILE = frozenset({"no_workflow", "draft", "no_prediction", "not_active"})

_REASON_KEYS = {
    "no_workflow": "wf.no_workflow",
    "not_playground": "wf.not_playground",
    "draft": "wf.draft",
    "no_prediction": "wf.no_prediction",
    "paused": "wf.paused",
    "not_active": "wf.not_active",
    "not_protecting": "wf.not_protecting",
    "status": "wf.status",
}

_STATE_WORDS = {
    "none": "status.state.none",
    "draft": "wait.draft",
    "queued": "wait.queued",
    "ready": "wait.ready",
    "active": "wait.active",
    "paused": "wait.paused",
}

_UNREACHABLE = 4
_AUTH = 3


def _say(key: str, **params: Any) -> Say:
    return Say(key, params)


def _lines(lines: Iterable[str]) -> tuple[Say, ...]:
    return tuple(_say("verify.line", line=line) for line in lines)


def _median(values: list[float]) -> float | None:
    return statistics.median(values) if values else None


def round_stats(
    visits: Iterable[Mapping[str, Any]],
    ev: Any,
    actions: int,
    successes: int,
    *,
    degraded: bool = False,
) -> RunStats:
    """What a protected round measured, from its leased visits and its evidence.

    A real user counts as blocked when a check() for its visit got ``block``
    and its action never happened. check() times leave out calls that
    failed open.
    """
    classes = {v.get("ref"): v.get("class") for v in visits}
    blocked = {c.get("ref") for c in ev.calls("check") if ev.verdict(c) == "block"}
    acted = {e.get("ref") for e in ev.entries("action")}
    real = [ref for ref, cls in classes.items() if cls == "real"]
    real_blocked = sum(1 for ref in real if ref in blocked and ref not in acted)
    took: list[float] = []
    inside: list[float] = []
    for call in ev.calls("check"):
        if call.get("outcome") != "verdict":
            continue
        t0, t1 = call.get("t0"), call.get("t1")
        if isinstance(t0, (int, float)) and isinstance(t1, (int, float)) and t1 >= t0:
            took.append((t1 - t0) * 1000)
        processing = call.get("processing_time_us")
        if isinstance(processing, int) and not isinstance(processing, bool):
            inside.append(processing / 1000)
    return RunStats(
        total=len(classes),
        actions=actions,
        successes=successes,
        real_blocked=real_blocked,
        real_total=len(real),
        check_ms=_median(took),
        inside_ms=_median(inside),
        degraded=degraded,
    )


class Online:
    """Carries out the online effects for one console session."""

    def __init__(
        self,
        session: _Session,
        *,
        root: Path,
        api_transport: httpx.AsyncBaseTransport | None,
        telemetry: bool,
    ) -> None:
        self._s = session
        self._root = root
        self._transport = api_transport
        self._telemetry = telemetry
        self._seq = 0
        self._findings: dict[str, None] = {}

    # -- plumbing ----------------------------------------------------------------------------

    def _api(self) -> PlaygroundApi | None:
        value = api_key.read_key(self._root)
        if api_key.problem(value) is not None:
            return None
        assert value is not None
        return PlaygroundApi(value, transport=self._transport)

    def _session(self) -> Mapping[str, Any]:
        session = self._s.machine.session
        assert session is not None, "an online step needs its session"
        return session

    def _limit(self, name: str) -> float:
        return float(self._session()["limits"][name])

    def _next_seq(self) -> int:
        self._seq += 1
        return self._seq

    async def handle(self, effect: Event) -> StepDone | None:
        """Carry out ``effect``; None for the one effect with no outcome."""
        if isinstance(effect, CheckKey):
            return await self.check_key()
        if isinstance(effect, OpenSession):
            return await self.open_session(effect.size)
        if isinstance(effect, EndSession):
            await self.end_session(effect.session)
            return None
        if isinstance(effect, WriteReport):
            return await self.write_report()
        if isinstance(effect, CopySnippets):
            return await self.copy_snippets(effect.task, effect.index)
        api = self._api()
        if api is None:
            return StepDone(False, (_say("ch2.key.missing"),), exit_code=_AUTH)
        async with api:
            try:
                return await self._with_api(api, effect)
            except ProtocolOutdated:
                return StepDone(False, (_say("online.outdated"),), exit_code=_AUTH, fatal=True)
            except ApiError as error:
                if error.status == 404:
                    # The session was closed: the machine words what follows.
                    return StepDone(False, session_closed=True)
                if error.status == 401:
                    return StepDone(False, (_say("ch2.session.bad_key"),), exit_code=_AUTH)
                return StepDone(False, (_say("online.unreachable"),), exit_code=_UNREACHABLE)
            except ApiUnreachable:
                return StepDone(False, (_say("online.unreachable"),), exit_code=_UNREACHABLE)
            except (AppUnavailable, ControlError, httpx.HTTPError):
                # The sample app stopped answering on your laptop.
                return StepDone(False, (_say("online.no_app"),))

    async def _with_api(self, api: PlaygroundApi, effect: Event) -> StepDone:
        if isinstance(effect, RunGate):
            return await self.run_gate(api, effect.chapter)
        if isinstance(effect, RunTraffic):
            return await self.run_traffic(api, effect.phase, effect.size, effect.round)
        if isinstance(effect, WaitWorkflow):
            return await self.wait_workflow(api, effect.gate)
        if isinstance(effect, RunReadiness):
            return await self.run_readiness(api)
        if isinstance(effect, ReadStage):
            return await self.read_stage(api)
        if isinstance(effect, Complete):
            return await self.complete(api, effect)
        raise ValueError(effect)

    async def _report_findings(self, api: PlaygroundApi, chapter: int, result: GateResult) -> None:
        """Remember the finding IDs, and tell SignalGate the ones that failed a check."""
        for finding_id in result.finding_ids:
            self._findings[finding_id] = None
        failed = list(dict.fromkeys(f.id for f in result.findings if f.blocks))
        if not self._telemetry or not failed or result.verdict is Verdict.PASS:
            return
        with contextlib.suppress(ApiError, ApiUnreachable):
            await api.telemetry(self._session()["session_id"], chapter, failed)

    # -- /copy --------------------------------------------------------------------------------

    async def copy_snippets(self, task: str, index: int | None = None) -> StepDone:
        """Copy a task's snippets for ``/copy``: all of them, or snippet ``index``.

        They go on the clipboard when a clipboard tool takes them; otherwise
        into ``.playground/snippets/chapter<N>.py`` (``chapter<N>-<index>.py``
        for one snippet), and the answer names that file.
        """
        if task not in TASKS:
            return StepDone(True, (_say("copy.none"),))
        count = len(TASKS[task][1])
        try:
            code = snippet_text(task, index)
        except IndexError:
            return StepDone(True, (_say("copy.bad_index", count=count),))
        if await asyncio.to_thread(copy_to_clipboard, code):
            if index is None:
                return StepDone(True, (_say("copy.done", count=count),))
            return StepDone(True, (_say("copy.done_one", index=index),))
        chapter = "".join(ch for ch in task.split(".", 1)[0] if ch.isdigit())
        name = f"chapter{chapter}" + (f"-{index}" if index is not None else "") + ".py"
        relative = _SNIPPETS / name
        if not await asyncio.to_thread(_write_text, self._root / relative, code):
            return StepDone(True, (_say("copy.failed"),))
        return StepDone(True, (_say("copy.file", path=relative.as_posix()),))

    # -- chapter 2 ----------------------------------------------------------------------------

    async def check_key(self) -> StepDone:
        value = api_key.read_key(self._root)
        problem = api_key.problem(value)
        if problem is not None:
            return StepDone(False, (_say(f"ch2.key.{problem}"),), exit_code=_AUTH)
        assert value is not None
        found = await asyncio.to_thread(check_hygiene, self._root, value)
        says: list[Say] = [_say("ch2.key.ok")]
        if found.verdict is Verdict.FAIL:
            says.append(_say(f"ch2.git.{found.problem}", file=found.file))
            return StepDone(False, tuple(says), exit_code=_AUTH)
        says.append(_say("ch2.git.no_git" if found.verdict is Verdict.NOT_RUN else "ch2.git.ok"))
        return StepDone(True, tuple(says))

    async def open_session(self, size: int) -> StepDone:
        api = self._api()
        if api is None:
            return StepDone(False, (_say("ch2.key.missing"),), exit_code=_AUTH)
        async with api:
            sent_at = time.time()
            try:
                created = await api.create_session(size, CONSENT_VERSION)
            except ProtocolOutdated:
                return StepDone(False, (_say("online.outdated"),), exit_code=_AUTH, fatal=True)
            except ApiError as error:
                return self._session_refused(error)
            except ApiUnreachable:
                return StepDone(False, (_say("online.unreachable"),), exit_code=_UNREACHABLE)
            answered_at = time.time()
            limits = created["limits"]
            skew = abs(created["server_time"].timestamp() - (sent_at + answered_at) / 2)
            if skew >= limits["clock_block_s"]:
                with contextlib.suppress(ApiError, ApiUnreachable):
                    await api.end_session(created["session_id"])
                return StepDone(False, (_say("ch2.clock.block", skew=skew),))
        self._s.show(_say("ch2.session.ok"))
        if skew >= limits["clock_warn_s"]:
            self._s.show(_say("ch2.clock.warn", skew=skew))
        await self._s.restart_with_key()
        session = {"session_id": created["session_id"], "size": size, "limits": dict(limits)}
        return StepDone(True, session=session)

    @staticmethod
    def _session_refused(error: ApiError) -> StepDone:
        if error.status == 401:
            return StepDone(False, (_say("ch2.session.bad_key"),), exit_code=_AUTH)
        if error.status == 409 and error.code == "SESSION_ACTIVE":
            details = {"started_at": "?", "key_prefix": "?", **error.details}
            details["started_at"] = _started_words(details["started_at"])
            return StepDone(False, (_say("ch2.session.active", **details),), exit_code=_AUTH)
        if error.status == 412:
            return StepDone(False, (_say("ch2.session.no_public_key"),), exit_code=_AUTH)
        if error.status == 429:
            return StepDone(False, (_say("ch2.session.busy"),), exit_code=_UNREACHABLE)
        return StepDone(False, (_say("ch2.session.rejected", status=error.status),))

    async def end_session(self, session: Mapping[str, Any]) -> None:
        api = self._api()
        if api is None:
            return
        async with api:
            with contextlib.suppress(ApiError, ApiUnreachable):
                await api.end_session(session["session_id"])

    # -- the gates -----------------------------------------------------------------------------

    async def run_gate(self, api: PlaygroundApi, chapter: int) -> StepDone:
        routes = self._root / _ROUTES
        async with self._s.app.hold():
            boot = self._s.app.boot
            assert boot is not None
            link = AppLink(boot.base_url, boot.token)
            try:
                lease = await api.lease(
                    self._session()["session_id"],
                    phase="probe",
                    count=PROBE_VISITS,
                    seq=self._next_seq(),
                    probe_roles=probe_roles(chapter),
                )
                visits = lease["visits"]
            except ApiError as error:
                if error.status == 404:
                    raise  # the session was closed: handle() says so
                code = not_run("code", "lease", needs_proof=True)
            except ApiUnreachable:
                code = not_run("code", "lease", needs_proof=True)
            else:
                code = await run_code_gate(
                    link, visits, chapter=chapter, routes=routes, root=self._root
                )
            says = list(_lines(gate_lines(code)))
            await self._report_findings(api, chapter, code)
            if code.verdict is not Verdict.PASS or chapter < 5:
                return StepDone(code.verdict is Verdict.PASS, tuple(says))
            rehearsal = await run_rehearsal(link, routes=routes, root=self._root)
            says += _lines(gate_lines(rehearsal))
            await self._report_findings(api, chapter, rehearsal)
            if rehearsal.verdict is not Verdict.PASS:
                return StepDone(False, tuple(says))
        status = await api.status(self._session()["session_id"])
        live = gate_g4(status)
        if live.verdict is not Verdict.PASS:
            return StepDone(False, (*says, *self._reason_says(live)))
        return StepDone(True, tuple(says))

    # -- traffic runs ----------------------------------------------------------------------------

    def _lease_for(self, api: PlaygroundApi, phase: str, round_no: int | None) -> Any:
        async def lease(count: int) -> list[Mapping[str, Any]]:
            while True:
                try:
                    answer = await api.lease(
                        self._session()["session_id"],
                        phase=phase,
                        count=count,
                        seq=self._next_seq(),
                        round=round_no,
                    )
                except ApiError as error:
                    if error.status == 429 and error.retry_after_s is not None:
                        await asyncio.sleep(error.retry_after_s)
                        continue
                    if error.status == 409:
                        raise LeaseStopped(error.code or "conflict") from error
                    raise
                return answer["visits"]

        return lease

    async def _paced(
        self,
        api: PlaygroundApi,
        control: ControlClient,
        *,
        phase: str,
        size: int,
        round_no: int | None,
        run: str,
        meter: DrainMeter,
        paced: PacedRun,
    ) -> Any:
        boot = self._s.app.boot
        assert boot is not None
        return await paced.run(
            lease=self._lease_for(api, phase, round_no),
            lease_max=int(self._limit("lease_max")),
            meter=meter,
            use_within_s=self._limit("use_within_s"),
            max_queue_age_s=self._limit("max_queue_age_s"),
            base_url=boot.base_url,
            control_token=boot.token,
            stop=self._s.stop_run,
            run_id=run,
        )

    def _progress(self, paced: PacedRun, meter: DrainMeter, stored: int | None) -> RunProgress:
        tally = paced.tally
        delivered = meter.delta(KEY_SENT) + meter.delta(KEY_DROPPED_RETRY)
        # How long the app's SDK still needs: only once no visitor is left to
        # run, since until then the run doesn't end when the queue empties.
        # The wait after the run gives up after DRAIN_MAX_S, so no estimate
        # says more than that.
        eta_s = None
        if tally.done >= paced.size or self._s.stop_run.is_set():
            eta_s = eta_seconds(meter.backlog, meter.rate, at_most=DRAIN_MAX_S)
        return RunProgress(
            tally.done,
            tally.total,
            tally.actions,
            tally.successes,
            posted=meter.delta(KEY_ENQUEUED),
            delivered=delivered,
            accepted=meter.delta(KEY_SENT),
            stored=stored,
            eta_s=eta_s,
        )

    async def run_traffic(
        self, api: PlaygroundApi, phase: Phase, size: int, round_no: int
    ) -> StepDone:
        sid = self._session()["session_id"]
        if phase is Phase.LEARN:
            title = "panel.learn.title"
        else:
            title = "panel.round.title" if round_no == 1 else "panel.rerun.title"
        async with self._s.app.hold() as held:
            control = self._s.app.control
            assert control is not None
            start = await mark(control)
            run = new_run_id(_PHASE_NAMES[phase])
            since = datetime.now(timezone.utc)
            paced = PacedRun(size)
            meter = DrainMeter(control, start.metrics)
            stored_total: list[int | None] = [None]

            def tick() -> None:
                self._s.progress(self._progress(paced, meter, stored_total[0]))

            self._s.panel_begin(size, title)
            ticker = asyncio.ensure_future(meter.run(tick))
            try:
                result = await self._s.as_run(
                    self._paced(
                        api,
                        control,
                        phase=_PHASE_NAMES[phase],
                        size=size,
                        round_no=round_no if phase is Phase.PROTECT else None,
                        run=run,
                        meter=meter,
                        paced=paced,
                    ),
                    held,
                )
                await self._s.drain_after_run()
                intact = await held.intact()
                if not intact:
                    return StepDone(False, (_say("run.invalidated"),))
                if paced.stopped_by is not None:
                    return StepDone(False, (_say("online.lease_conflict"),))
                if result.stopped:
                    return StepDone(False, (_say("run.stopped", done=result.done, total=size),))
                if result.actions == 0:
                    return StepDone(False, (_say("run.none"),))
                try:
                    ev = await collect(control, start, run=run)
                except EvidenceUnavailable as unavailable:
                    gate = not_run("data", unavailable.reason)
                    return StepDone(False, _lines(gate_lines(gate)))
                posted = posted_counts(ev)

                async def fetch() -> Mapping[str, int] | None:
                    status = await api.status(sid, since=since)
                    return status["stored_since"]

                stored = await wait_for_stored(fetch, posted, self._limit("ingest_wait_s"))
                stored_total[0] = sum(stored.values()) if stored is not None else None
                await meter.read()
                tick()
            finally:
                ticker.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await ticker
                self._s.panel_end()
        data = data_gate(ev, stored)
        says = list(_lines(gate_lines(data.result)))
        chapter = 3 if phase is Phase.LEARN else 5
        await self._report_findings(api, chapter, data.result)
        if data.result.verdict is not Verdict.PASS:
            return StepDone(False, tuple(says))
        # Cut short because the app's events fell behind, or too many check()
        # calls failed open: the run counts, but its numbers may be off.
        degraded = paced.degraded or data.degraded
        if degraded:
            says.append(_say("run.degraded"))
        if phase is Phase.LEARN:
            g1 = gate_g1(await api.status(sid))
            if g1.verdict is Verdict.NOT_RUN:
                return StepDone(False, (*says, _say("wf.status")), exit_code=_UNREACHABLE)
            stats = RunStats(
                total=size,
                actions=result.actions,
                successes=result.successes,
                degraded=degraded,
            )
            shortfall = g1.shortfall if g1.verdict is Verdict.FAIL else 0
            return StepDone(True, tuple(says), stats=stats, shortfall=shortfall)
        tripped = tripwire(ev)
        if tripped:
            return StepDone(False, (*says, _say("ch5.tripwire", count=len(tripped))))
        stats = round_stats(paced.visits, ev, result.actions, result.successes, degraded=degraded)
        return StepDone(True, tuple(says), stats=stats)

    async def run_readiness(self, api: PlaygroundApi) -> StepDone:
        verdicts: list[str | None] = []
        attempts = int(self._limit("readiness_max_attempts"))
        while True:
            async with self._s.app.hold() as held:
                control = self._s.app.control
                assert control is not None
                start = await mark(control)
                run = new_run_id("readiness")
                paced = PacedRun(READINESS_VISITORS)
                meter = DrainMeter(control, start.metrics)
                self._s.wait_show(_say("wait.readiness"))
                try:
                    await self._s.as_run(
                        self._paced(
                            api,
                            control,
                            phase="readiness",
                            size=READINESS_VISITORS,
                            round_no=None,
                            run=run,
                            meter=meter,
                            paced=paced,
                        ),
                        held,
                    )
                finally:
                    self._s.wait_end()
                await self._s.drain_after_run()
                if not await held.intact():
                    return StepDone(False, (_say("run.invalidated"),))
                try:
                    ev = await collect(control, start, run=run)
                except EvidenceUnavailable as unavailable:
                    return StepDone(False, _lines(gate_lines(not_run("data", unavailable.reason))))
            found = readiness(paced.visits, ev)
            verdicts.append(found.verdict)
            step = readiness_next(verdicts, attempts)
            if step == wf.CONTINUE:
                return StepDone(True, (_say("ch5.readiness.live"),))
            if step == wf.CONTINUE_WEAK:
                return StepDone(True, (_say("ch5.readiness.weak"),))
            if step == wf.STOP:
                return StepDone(False, (_say("ch5.readiness.over_blocking"),))
            if step == wf.DIAGNOSE:
                key = (
                    "ch5.readiness.no_checks" if found.verdict is None else "ch5.readiness.diagnose"
                )
                return StepDone(False, (_say(key),), exit_code=_UNREACHABLE)
            self._s.wait_show(_say("wait.protecting"))
            try:
                await asyncio.sleep(self._limit("readiness_retry_s"))
            finally:
                self._s.wait_end()

    # -- the workflow ----------------------------------------------------------------------------

    @staticmethod
    def _reason_says(gate: wf.WorkflowGate) -> tuple[Say, ...]:
        if gate.reason == "method_mismatch":
            says = []
            if gate.method_ok is False:
                says.append(_say("wf.method_wrong"))
            if gate.target_ok is False:
                says.append(_say("wf.target_wrong"))
            return tuple(says)
        if gate.reason == "not_block":
            return (_say("wf.not_block"), _say("ch4.why_block"))
        return (_say(_REASON_KEYS.get(gate.reason, "wf.status")),)

    @staticmethod
    def _words(workflow: Mapping[str, Any], gate: Gate) -> Say:
        """What the dashboard shows for ``workflow``, for the spinner line."""
        state = workflow.get("state")
        if state == "none":
            return _say("wait.none")
        if state == "dry_running" and workflow.get("stage") is not None:
            return _say(
                "wait.dry_running",
                stage=workflow["stage"],
                stages_total=workflow.get("stages_total") or 3,
            )
        if state == "ready" and gate is Gate.G3:
            return _say("wait.promoting")
        if state == "active" and gate is Gate.G4 and not workflow.get("protecting"):
            return _say("wait.protecting")
        key = {
            "draft": "wait.draft",
            "queued": "wait.queued",
            "dry_running": "wait.queued",
            "ready": "wait.ready",
            "active": "wait.active",
            "paused": "wait.paused",
        }.get(str(state), "wait.none")
        return _say(key)

    async def wait_workflow(self, api: PlaygroundApi, gate: Gate) -> StepDone:
        sid = self._session()["session_id"]
        started = time.monotonic()
        last: Say | None = None
        failures = 0
        try:
            while True:
                try:
                    status = await api.status(sid)
                except ApiUnreachable:
                    failures += 1
                    if failures >= STATUS_READ_FAILURES_MAX:
                        return StepDone(False, (_say("wf.status"),), exit_code=_UNREACHABLE)
                    await asyncio.sleep(WORKFLOW_POLL_S)
                    continue
                failures = 0
                workflow = status["workflow"]
                words = self._words(workflow, gate)
                if words != last:
                    self._s.wait_show(words)
                    last = words
                found = _GATES[gate](status)
                stage = workflow.get("protection_stage")
                if found.verdict is Verdict.PASS:
                    prediction = Prediction(**found.prediction) if found.prediction else None
                    return StepDone(True, prediction=prediction, stage=stage)
                waited = time.monotonic() - started
                if found.reason in _WAIT_FREELY:
                    pass
                elif found.reason == "not_protecting":
                    if waited >= self._limit("protect_diagnose_after_s"):
                        return StepDone(False, self._reason_says(found), exit_code=_UNREACHABLE)
                elif found.reason in _WAIT_A_WHILE:
                    if waited >= WORKFLOW_NUDGE_S:
                        return StepDone(False, self._reason_says(found), exit_code=_UNREACHABLE)
                else:
                    return StepDone(False, self._reason_says(found), stage=stage)
                await asyncio.sleep(WORKFLOW_POLL_S)
        finally:
            self._s.wait_end()

    async def read_stage(self, api: PlaygroundApi) -> StepDone:
        try:
            status = await api.status(self._session()["session_id"])
        except ApiUnreachable:
            return StepDone(False, (_say("status.workflow_unknown"),), exit_code=_UNREACHABLE)
        workflow = status["workflow"]
        state = workflow["state"]
        stage = workflow["protection_stage"]
        if state == "dry_running":
            words = self._words(workflow, Gate.G2)
        else:
            words = _say(_STATE_WORDS.get(state, "wait.active"))
        says: list[Say] = [_say("status.workflow", state=_render(words))]
        line = stage_say(stage) if workflow.get("protecting") else None
        if line is not None:
            says.append(line)
        return StepDone(True, tuple(says), stage=stage, paused=state == "paused")

    async def complete(self, api: PlaygroundApi, effect: Complete) -> StepDone:
        results = None
        if self._telemetry and effect.conv_before is not None and effect.conv_after is not None:
            results = {"conv_before": effect.conv_before, "conv_after": effect.conv_after}
        try:
            await api.complete(
                self._session()["session_id"], keep_workflow=effect.keep, results=results
            )
        except (ApiError, ApiUnreachable):
            return StepDone(True, (_say("ch6.complete_failed"),))
        return StepDone(True, (_say("ch6.kept" if effect.keep else "ch6.paused"),))

    # -- /report -------------------------------------------------------------------------------------

    async def write_report(self) -> StepDone:
        """A support bundle: versions, where you are, check results and counters.

        No code, no keys, no envelopes and no paths.
        """
        snapshot = self._s.machine.to_dict()
        app = self._s.app
        health: dict[str, Any] | None = None
        metrics: Mapping[str, int] | None = None
        if app.alive and app.control is not None:
            with contextlib.suppress(Exception):
                found = await app.control.health()
                health = {
                    "sdk_version": found.sdk_version,
                    "self_test_ok": bool(found.self_test.get("ok")),
                    "key": found.key,
                    "clients_created": found.clients_created,
                    "client_ready": found.client_ready,
                }
            with contextlib.suppress(Exception):
                metrics = await app.control.metrics()
        bundle = {
            "v": 1,
            "console": CLIENT_VERSION,
            "python": platform.python_version(),
            "system": platform.system(),
            "chapter": self._s.machine.chapter,
            "step": snapshot["step"],
            "running": snapshot["running"],
            "session_open": snapshot["session"] is not None,
            "telemetry": self._telemetry,
            "findings": list(self._findings),
            "stage": snapshot["stage"],
            "learn": snapshot["learn"],
            "prediction": snapshot["prediction"],
            "rounds": snapshot["rounds"],
            "app": {"alive": app.alive, "boots": app.number, "health": health},
            "metrics": dict(metrics) if metrics is not None else None,
        }
        path = self._root / _REPORT
        written = await asyncio.to_thread(_write_json, path, bundle)
        return StepDone(True, (_say("report.written" if written else "report.failed"),))


def _render(say: Say) -> str:
    return text(say.key, **dict(say.params))


def _started_words(raw: str) -> str:
    """When another session started, in words, in this computer's local time.

    ``raw`` is the time the API gave. A value that doesn't read as a time
    with an offset is shown as it came.
    """
    try:
        moment = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return raw
    if moment.tzinfo is None:
        return raw
    return text("ch2.session.started", moment=moment.astimezone())


def _write_text(path: Path, content: str) -> bool:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    except OSError:
        return False
    return True


def _write_json(path: Path, data: Any) -> bool:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2)
        os.replace(temporary, path)
    except OSError:
        return False
    return True


__all__ = ["READINESS_VISITORS", "Online", "round_stats"]
