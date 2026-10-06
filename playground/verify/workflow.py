"""Workflow gates G1-G4, the readiness verdict and the live tripwire.

G1-G4 read the session status the playground API returns (its wire shape,
as a mapping) and say whether the tutorial may move on, with a reason code
the chapter turns into words:

- G1 (chapter 4 opens): at least ``G1_MIN_ACTIONS`` actions stored in the
  analysis window; ``shortfall`` says how many more a top-up run needs;
- G2 (chapter 4): the workflow is on the playground's own methods, its dry
  run is complete and a prediction is present, which the chapter freezes;
- G3 (chapter 5 opens): the workflow is active with action ``block``;
- G4 (chapter 5): protection is on and the methods still match.

Readiness and the tripwire are computed on the laptop only, from each
visit's ``class`` in the lease and the check() verdicts the app recorded.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from playground.verify.evidence import Evidence
from playground.verify.model import Verdict

G1_MIN_ACTIONS = 1000

# Readiness rules, evaluated in this order (see ``readiness``).
OVER_BLOCKING = "over_blocking"
LIVE = "live"
ARMING = "arming"
WEAK = "weak"
READINESS_VERDICTS = (OVER_BLOCKING, LIVE, ARMING, WEAK)
# over_blocking: at least this many real users blocked.
OVER_BLOCKING_MIN_REAL = 2
# live: at least this share of the attack blocked, and at most this many real users.
LIVE_MIN_ATTACK_SHARE = 35 / 58
LIVE_MAX_REAL = 1
# arming: at most this many of the attack blocked.
ARMING_MAX_ATTACK = 2

# What the chapter does after a readiness round.
CONTINUE = "continue"
CONTINUE_WEAK = "continue_weak"
RETRY = "retry"
STOP = "stop"
DIAGNOSE = "diagnose"


@dataclass(frozen=True)
class WorkflowGate:
    """One workflow gate: PASS, FAIL (not yet, see ``reason``) or NOT RUN."""

    verdict: Verdict
    reason: str
    shortfall: int = 0
    prediction: Mapping[str, Any] | None = None
    method_ok: bool | None = None
    target_ok: bool | None = None


def _workflow(status: Mapping[str, Any] | None) -> Mapping[str, Any] | None:
    workflow = status.get("workflow") if isinstance(status, Mapping) else None
    return workflow if isinstance(workflow, Mapping) else None


def _unmatched(workflow: Mapping[str, Any]) -> WorkflowGate | None:
    """Why a workflow isn't the tutorial's own, or None when it is."""
    if workflow.get("state") == "none":
        return WorkflowGate(Verdict.FAIL, "no_workflow")
    if not workflow.get("methods_match"):
        return WorkflowGate(Verdict.FAIL, "not_playground")
    method_ok, target_ok = workflow.get("method_ok"), workflow.get("target_ok")
    if method_ok is False or target_ok is False:
        return WorkflowGate(
            Verdict.FAIL, "method_mismatch", method_ok=method_ok, target_ok=target_ok
        )
    return None


def gate_g1(status: Mapping[str, Any] | None) -> WorkflowGate:
    window = status.get("window") if isinstance(status, Mapping) else None
    received = window.get("action_received") if isinstance(window, Mapping) else None
    if not isinstance(received, int):
        return WorkflowGate(Verdict.NOT_RUN, "status")
    if received >= G1_MIN_ACTIONS:
        return WorkflowGate(Verdict.PASS, "ok")
    return WorkflowGate(Verdict.FAIL, "too_few", shortfall=G1_MIN_ACTIONS - received)


def gate_g2(status: Mapping[str, Any] | None) -> WorkflowGate:
    workflow = _workflow(status)
    if workflow is None:
        return WorkflowGate(Verdict.NOT_RUN, "status")
    unmatched = _unmatched(workflow)
    if unmatched is not None:
        return unmatched
    state = workflow.get("state")
    if state == "draft":
        return WorkflowGate(Verdict.FAIL, "draft")
    if state in ("queued", "dry_running"):
        return WorkflowGate(Verdict.FAIL, "dry_run_pending")
    prediction = workflow.get("prediction")
    if not isinstance(prediction, Mapping):
        return WorkflowGate(Verdict.FAIL, "no_prediction")
    return WorkflowGate(Verdict.PASS, "ok", prediction=dict(prediction))


def gate_g3(status: Mapping[str, Any] | None) -> WorkflowGate:
    workflow = _workflow(status)
    if workflow is None:
        return WorkflowGate(Verdict.NOT_RUN, "status")
    unmatched = _unmatched(workflow)
    if unmatched is not None:
        return unmatched
    if workflow.get("state") == "paused":
        return WorkflowGate(Verdict.FAIL, "paused")
    if workflow.get("state") != "active":
        return WorkflowGate(Verdict.FAIL, "not_active")
    if workflow.get("action") != "block":
        return WorkflowGate(Verdict.FAIL, "not_block")
    return WorkflowGate(Verdict.PASS, "ok")


def gate_g4(status: Mapping[str, Any] | None) -> WorkflowGate:
    workflow = _workflow(status)
    if workflow is None:
        return WorkflowGate(Verdict.NOT_RUN, "status")
    unmatched = _unmatched(workflow)
    if unmatched is not None:
        return unmatched
    if workflow.get("state") == "paused":
        return WorkflowGate(Verdict.FAIL, "paused")
    if workflow.get("protecting") is not True:
        return WorkflowGate(Verdict.FAIL, "not_protecting")
    return WorkflowGate(Verdict.PASS, "ok")


# --- readiness and the tripwire ---------------------------------------------------------


def _blocked_refs(ev: Evidence) -> set[str]:
    """Every request ref that got a ``block`` verdict from a check() call."""
    return {call.get("ref", "") for call in ev.calls("check") if ev.verdict(call) == "block"}


@dataclass(frozen=True)
class Readiness:
    """A readiness round's verdict (None: no check() verdict was recorded) and its counts."""

    verdict: str | None
    real_blocked: int
    real_total: int
    attack_blocked: int
    attack_total: int


def readiness(visits: Iterable[Mapping[str, Any]], ev: Evidence) -> Readiness:
    """Judge one readiness round from its leased ``visits`` and the round's evidence.

    A visit counts as blocked when a check() call made for its ``ref`` got
    ``block``. The rules run in order: ``over_blocking``, ``live``,
    ``arming``, then ``weak`` for anything else.
    """
    classes = {v.get("ref"): v.get("class") for v in visits}
    checked = {call.get("ref") for call in ev.calls("check")} & set(classes)
    blocked = _blocked_refs(ev)
    real = [ref for ref, cls in classes.items() if cls == "real"]
    attack = [ref for ref, cls in classes.items() if cls == "attack"]
    real_blocked = sum(ref in blocked for ref in real)
    attack_blocked = sum(ref in blocked for ref in attack)
    verdict: str | None
    if not checked:
        verdict = None
    elif real_blocked >= OVER_BLOCKING_MIN_REAL:
        verdict = OVER_BLOCKING
    elif (
        attack
        and attack_blocked >= LIVE_MIN_ATTACK_SHARE * len(attack)
        and real_blocked <= LIVE_MAX_REAL
    ):
        verdict = LIVE
    elif attack_blocked <= ARMING_MAX_ATTACK:
        verdict = ARMING
    else:
        verdict = WEAK
    return Readiness(verdict, real_blocked, len(real), attack_blocked, len(attack))


def readiness_next(verdicts: list[str | None], max_attempts: int) -> str:
    """What to do after the latest of ``verdicts`` (one per round so far).

    ``max_attempts`` is the session's ``limits.readiness_max_attempts``;
    the wait before a retry is its ``limits.readiness_retry_s``.
    """
    last = verdicts[-1] if verdicts else None
    if last == OVER_BLOCKING:
        return STOP
    if last == LIVE:
        return CONTINUE
    if last == WEAK:
        if len(verdicts) >= 2 and verdicts[-2] == WEAK or len(verdicts) >= max_attempts:
            return CONTINUE_WEAK
        return RETRY
    return RETRY if len(verdicts) < max_attempts else DIAGNOSE


def tripwire(ev: Evidence) -> list[str]:
    """Refs whose check() got ``block`` but whose action still happened."""
    blocked = _blocked_refs(ev)
    return sorted({e.get("ref", "") for e in ev.entries("action") if e.get("ref") in blocked})


__all__ = [
    "ARMING",
    "CONTINUE",
    "CONTINUE_WEAK",
    "DIAGNOSE",
    "G1_MIN_ACTIONS",
    "LIVE",
    "OVER_BLOCKING",
    "READINESS_VERDICTS",
    "RETRY",
    "STOP",
    "WEAK",
    "Readiness",
    "WorkflowGate",
    "gate_g1",
    "gate_g2",
    "gate_g3",
    "gate_g4",
    "readiness",
    "readiness_next",
    "tripwire",
]
