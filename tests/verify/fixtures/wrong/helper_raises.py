"""Passes log() an argument it doesn't take inside a helper next to the
handler, so a valid action raises one call below the handler."""

from __future__ import annotations

from fastapi import Request

from app.kit.actions import perform_action
from app.signalgate_client import sg, sg_event
from tests.verify.fixtures.common import ActionBody, router_with


def log_it(request: Request, body: ActionBody) -> None:
    ev = sg_event(request, body.user, body.signalgate_log, "playground.action")
    if ev:
        sg().log(ev, timeout=2.0)


def action(request: Request, body: ActionBody) -> dict[str, bool]:
    perform_action(body.user)
    log_it(request, body)
    return {"ok": True}


router = router_with(action)
