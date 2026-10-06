"""Chapter 5 that checks and logs with the same envelope."""

from __future__ import annotations

from fastapi import HTTPException, Request
from signalgate import SignalGateError

from app.kit.actions import perform_action
from app.signalgate_client import sg, sg_event
from tests.verify.fixtures.common import ActionBody, router_with


def action(request: Request, body: ActionBody) -> dict[str, bool]:
    ev = sg_event(request, body.user, body.signalgate, "playground.action")
    if ev:
        try:
            if sg().check(ev).action == "block":
                raise HTTPException(status_code=403, detail="Request could not be completed")
        except SignalGateError:
            pass
    perform_action(body.user)
    if ev:
        sg().log(ev)
    return {"ok": True}


router = router_with(action)
