"""Stops a blocked action but answers 200 instead of 403 (a note, not a failure)."""

from __future__ import annotations

from fastapi import Request
from signalgate import SignalGateError

from app.kit.actions import perform_action
from app.signalgate_client import sg, sg_event
from tests.verify.fixtures.common import ActionBody, log_action, router_with


def action(request: Request, body: ActionBody) -> dict[str, bool]:
    ev = sg_event(request, body.user, body.signalgate, "playground.action")
    if ev:
        try:
            if sg().check(ev).action == "block":
                return {"ok": False}
        except SignalGateError:
            pass
    perform_action(body.user)
    log_action(request, body)
    return {"ok": True}


router = router_with(action)
