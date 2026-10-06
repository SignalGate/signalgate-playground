"""Calls check() with no try/except, so a rejected call becomes a 500."""

from __future__ import annotations

from fastapi import HTTPException, Request

from app.kit.actions import perform_action
from app.signalgate_client import sg, sg_event
from tests.verify.fixtures.common import ActionBody, log_action, router_with


def action(request: Request, body: ActionBody) -> dict[str, bool]:
    ev = sg_event(request, body.user, body.signalgate, "playground.action")
    if ev and sg().check(ev).action == "block":
        raise HTTPException(status_code=403, detail="Request could not be completed")
    perform_action(body.user)
    log_action(request, body)
    return {"ok": True}


router = router_with(action)
