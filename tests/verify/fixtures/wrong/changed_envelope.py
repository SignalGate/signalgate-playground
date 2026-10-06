"""Changes the envelope before logging it, so SignalGate rejects it."""

from __future__ import annotations

from fastapi import Request

from app.kit.actions import perform_action
from app.signalgate_client import sg, sg_event
from tests.verify.fixtures.common import ActionBody, router_with


def action(request: Request, body: ActionBody) -> dict[str, bool]:
    perform_action(body.user)
    envelope = body.signalgate_log
    if isinstance(envelope, dict) and isinstance(envelope.get("timestamp"), int):
        envelope = {**envelope, "timestamp": envelope["timestamp"] + 1}
    ev = sg_event(request, body.user, envelope, "playground.action")
    if ev:
        sg().log(ev)
    return {"ok": True}


router = router_with(action)
