"""Logs the server's own address instead of the visitor's."""

from __future__ import annotations

import dataclasses

from fastapi import Request

from app.kit.actions import perform_action
from app.signalgate_client import sg, sg_event
from tests.verify.fixtures.common import ActionBody, router_with


def action(request: Request, body: ActionBody) -> dict[str, bool]:
    perform_action(body.user)
    ev = sg_event(request, body.user, body.signalgate_log, "playground.action")
    if ev:
        sg().log(dataclasses.replace(ev, ip=request.scope["server"][0]))
    return {"ok": True}


router = router_with(action)
