"""Chapter 5 whose async def handler calls a plain helper that runs check()."""

from __future__ import annotations

from fastapi import Request

from app.kit.actions import perform_action
from tests.verify.fixtures.common import ActionBody, gate, log_action, router_with


async def action(request: Request, body: ActionBody) -> dict[str, bool]:
    gate(request, body)
    perform_action(body.user)
    log_action(request, body)
    return {"ok": True}


router = router_with(action)
