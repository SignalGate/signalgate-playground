"""Logs the action in a finally block, so a failed action is logged too."""

from __future__ import annotations

from fastapi import Request

from app.kit.actions import perform_action
from app.signalgate_client import sg, sg_event
from tests.verify.fixtures.common import ActionBody, router_with


def action(request: Request, body: ActionBody) -> dict[str, bool]:
    try:
        perform_action(body.user)
    finally:
        ev = sg_event(request, body.user, body.signalgate_log, "playground.action")
        if ev:
            sg().log(ev)
    return {"ok": True}


router = router_with(action)
