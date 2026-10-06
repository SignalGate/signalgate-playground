"""Logs without checking that the envelope is there."""

from __future__ import annotations

from fastapi import Request

from app.kit.actions import perform_action
from app.signalgate_client import sg, sg_event
from tests.verify.fixtures.common import ActionBody, router_with


def action(request: Request, body: ActionBody) -> dict[str, bool]:
    perform_action(body.user)
    sg().log(sg_event(request, body.user, body.signalgate_log, "playground.action"))
    return {"ok": True}


router = router_with(action)
