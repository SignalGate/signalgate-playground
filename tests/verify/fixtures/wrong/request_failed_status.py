"""Hides the same error behind its own 500 answer, so the log shows no traceback."""

from __future__ import annotations

from fastapi import HTTPException, Request

from app.kit.actions import perform_action
from app.signalgate_client import sg, sg_event
from tests.verify.fixtures.common import ActionBody, router_with


def action(request: Request, body: ActionBody) -> dict[str, bool]:
    perform_action(body.user)
    try:
        ev = sg_event(request, body.user, body.signalgate_log, "playground.action")
        if ev:
            sg().log(ev, timeout=2.0)
    except TypeError:
        raise HTTPException(status_code=500, detail="Something went wrong") from None
    return {"ok": True}


router = router_with(action)
