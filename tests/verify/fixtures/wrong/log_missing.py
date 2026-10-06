"""Chapter 3 with only the success log: the action is never logged."""

from __future__ import annotations

from fastapi import Request

from app.kit.actions import perform_action
from tests.verify.fixtures.common import ActionBody, router_with


def action(request: Request, body: ActionBody) -> dict[str, bool]:
    perform_action(body.user)
    return {"ok": True}


router = router_with(action)
