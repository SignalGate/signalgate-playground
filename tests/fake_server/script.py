"""What the fake server says about the workflow, and which errors it gives, as a test decides.

A ``Script`` holds a list of workflow steps, each one the ``workflow``
object a status call returns, and a position in it. A test moves the
position itself (``advance``, ``go_to``), or lets every status call move it
one step (``advance_on_poll``). The default steps walk a tutorial workflow
from nothing to full protection: none, draft, a queued and a running dry
run, ready, then active while protection turns on, stage by stage.

With ``advance_on_poll`` and ``hold_until_actions``, the script plays the
dashboard the way a user would: nothing moves until SignalGate holds that
many stored actions (the learning run is in), then each status call moves
one step on.

The script also holds the session's ``limits``, and a queue of errors per
route: ``fail_next("lease", 409, "PLAN_CHANGED")`` makes the next lease call
answer that error instead of its usual answer.

Check verdicts follow the current step: an attack visit's envelope gets the
workflow's action once the workflow is active and protecting; every other
envelope gets "allow".
"""

from __future__ import annotations

import threading
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

STAGES_TOTAL = 3

# Session limits the fake hands out unless a test overrides them. Test
# values only: small, so tests run fast, and chosen for no other reason.
DEFAULT_LIMITS: dict[str, int] = {
    "max_size": 3000,
    "lease_max": 50,
    "use_within_s": 45,
    "max_queue_age_s": 90,
    "clock_warn_s": 7,
    "clock_block_s": 33,
    "readiness_retry_s": 3,
    "readiness_max_attempts": 2,
    "ingest_wait_s": 4,
    "protect_diagnose_after_s": 9,
}

# A dry run's prediction, as the fake reports it once the dry run is done.
PREDICTION: dict[str, float] = {"conv_now": 0.2, "conv_pred": 0.6, "blocked_share": 0.65}


def _empty(state: str) -> dict[str, Any]:
    return {
        "state": state,
        "stage": None,
        "stages_total": None,
        "action": None,
        "methods_match": False,
        "method_ok": None,
        "target_ok": None,
        "prediction": None,
        "protecting": None,
        "protection_stage": None,
    }


def no_workflow() -> dict[str, Any]:
    """No workflow named "playground" yet."""
    return _empty("none")


def name_only(state: str = "draft") -> dict[str, Any]:
    """A workflow named "playground" whose methods aren't the playground's own."""
    return _empty(state)


def playground_flow(
    state: str,
    *,
    stage: int | None = None,
    prediction: Mapping[str, float] | None = None,
    protecting: bool = False,
    protection_stage: int = 0,
    action: str = "block",
    method_ok: bool = True,
    target_ok: bool = True,
) -> dict[str, Any]:
    """A workflow on the playground's own methods, in ``state``."""
    return {
        "state": state,
        "stage": stage,
        "stages_total": STAGES_TOTAL,
        "action": action,
        "methods_match": True,
        "method_ok": method_ok,
        "target_ok": target_ok,
        "prediction": dict(prediction) if prediction is not None else None,
        "protecting": protecting,
        "protection_stage": protection_stage,
    }


def default_steps() -> list[dict[str, Any]]:
    """None, draft, a dry run stage by stage, ready, then active while protection turns on."""
    return [
        no_workflow(),
        playground_flow("draft"),
        playground_flow("queued"),
        *(playground_flow("dry_running", stage=stage) for stage in range(1, STAGES_TOTAL + 1)),
        playground_flow("ready", prediction=PREDICTION),
        playground_flow("active", prediction=PREDICTION),
        *(
            playground_flow(
                "active", prediction=PREDICTION, protecting=True, protection_stage=stage
            )
            for stage in range(1, STAGES_TOTAL + 1)
        ),
    ]


@dataclass(frozen=True)
class ScriptedError:
    """One error a route gives instead of its usual answer."""

    status: int
    code: str
    details: Mapping[str, str] | None = None
    retry_after_s: int | None = None


class Script:
    """The workflow steps, the limits and the queued errors of one fake server."""

    def __init__(
        self,
        steps: Sequence[Mapping[str, Any]] | None = None,
        *,
        advance_on_poll: bool = False,
        hold_until_actions: int = 0,
        limits: Mapping[str, int] | None = None,
        plan_version: str = "plan-test-1",
    ) -> None:
        self._lock = threading.Lock()
        self._steps = [dict(step) for step in (steps if steps is not None else default_steps())]
        if not self._steps:
            raise ValueError("a script needs at least one step")
        self._position = 0
        self._errors: dict[str, list[ScriptedError]] = {}
        self.advance_on_poll = advance_on_poll
        self.hold_until_actions = hold_until_actions
        self.limits = {**DEFAULT_LIMITS, **(limits or {})}
        self.plan_version = plan_version

    # --- the workflow ---------------------------------------------------------

    @property
    def position(self) -> int:
        with self._lock:
            return self._position

    def workflow(self) -> dict[str, Any]:
        """The current step's workflow object (a copy)."""
        with self._lock:
            return dict(self._steps[self._position])

    def advance(self, steps: int = 1) -> dict[str, Any]:
        """Move ``steps`` steps on (never past the last); return the new current step."""
        with self._lock:
            self._position = min(self._position + steps, len(self._steps) - 1)
            return dict(self._steps[self._position])

    def go_to(self, state: str, **fields: Any) -> dict[str, Any]:
        """Move to the first step in ``state`` whose fields match ``fields``."""
        with self._lock:
            for index, step in enumerate(self._steps):
                if step["state"] == state and all(step.get(k) == v for k, v in fields.items()):
                    self._position = index
                    return dict(step)
        raise LookupError(f"no step in state {state!r} matching {fields!r}")

    def set_workflow(self, workflow: Mapping[str, Any]) -> None:
        """Replace the current step with ``workflow``."""
        with self._lock:
            self._steps[self._position] = dict(workflow)

    def polled(self, window_actions: int | None = None) -> dict[str, Any]:
        """The workflow a status call reports; moves one step on after it if
        ``advance_on_poll``, once ``window_actions`` reaches ``hold_until_actions``."""
        with self._lock:
            current = dict(self._steps[self._position])
            held = (window_actions or 0) < self.hold_until_actions
            if self.advance_on_poll and not held:
                self._position = min(self._position + 1, len(self._steps) - 1)
            return current

    def pause(self) -> None:
        """Pause the current workflow, if it is on the playground's own methods."""
        with self._lock:
            step = self._steps[self._position]
            if step["methods_match"]:
                self._steps[self._position] = {
                    **step,
                    "state": "paused",
                    "protecting": False,
                    "protection_stage": 0,
                }

    def verdict(self, cls: str) -> str:
        """The check verdict for a visit of class ``cls``, as of the current step."""
        step = self.workflow()
        if cls == "attack" and step["state"] == "active" and step["protecting"]:
            return step["action"] or "block"
        return "allow"

    # --- errors -------------------------------------------------------------------

    def fail_next(
        self,
        route: str,
        status: int,
        code: str,
        *,
        details: Mapping[str, str] | None = None,
        retry_after_s: int | None = None,
        times: int = 1,
    ) -> None:
        """Make the next ``times`` calls to ``route`` answer this error instead."""
        error = ScriptedError(status, code, dict(details) if details else None, retry_after_s)
        with self._lock:
            self._errors.setdefault(route, []).extend([error] * times)

    def take_error(self, route: str) -> ScriptedError | None:
        """The next queued error for ``route``, if any; it is used up."""
        with self._lock:
            queued = self._errors.get(route)
            return queued.pop(0) if queued else None


__all__ = [
    "DEFAULT_LIMITS",
    "PREDICTION",
    "STAGES_TOTAL",
    "Script",
    "ScriptedError",
    "default_steps",
    "name_only",
    "no_workflow",
    "playground_flow",
]
