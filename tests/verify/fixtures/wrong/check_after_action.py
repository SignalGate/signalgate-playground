"""Runs check() only after the action has happened."""

from __future__ import annotations

from fastapi import Request

from app.kit.actions import perform_action
from tests.verify.fixtures.common import ActionBody, gate, log_action, router_with


def action(request: Request, body: ActionBody) -> dict[str, bool]:
    perform_action(body.user)
    gate(request, body)
    log_action(request, body)
    return {"ok": True}


router = router_with(action)
