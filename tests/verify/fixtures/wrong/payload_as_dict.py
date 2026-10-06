"""Builds the event by hand with the envelope as a plain dict, so log() raises."""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import Request
from signalgate import Event

from app.kit.actions import perform_action
from app.signalgate_client import sg
from tests.verify.fixtures.common import ActionBody, router_with


def action(request: Request, body: ActionBody) -> dict[str, bool]:
    perform_action(body.user)
    if body.signalgate_log:
        sg().log(
            Event(
                user_id=body.user,
                ip=request.client.host,
                method="playground.action",
                timestamp=datetime.now(timezone.utc).isoformat(),
                payload=body.signalgate_log,
            )
        )
    return {"ok": True}


router = router_with(action)
