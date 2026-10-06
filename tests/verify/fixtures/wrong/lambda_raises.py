"""Passes log() an argument it doesn't take inside a lambda in the handler,
so a valid action raises one call below the handler."""

from __future__ import annotations

from fastapi import Request

from app.kit.actions import perform_action
from app.signalgate_client import sg, sg_event
from tests.verify.fixtures.common import ActionBody, router_with


def action(request: Request, body: ActionBody) -> dict[str, bool]:
    perform_action(body.user)
    send = lambda ev: sg().log(ev, timeout=2.0)
    ev = sg_event(request, body.user, body.signalgate_log, "playground.action")
    if ev:
        send(ev)
    return {"ok": True}


router = router_with(action)
