"""The success log reads signalgate_log, which only the action request carries."""

from __future__ import annotations

from fastapi import HTTPException, Request

from app.kit import tokens
from app.signalgate_client import sg, sg_event
from tests.verify.fixtures.common import ConfirmBody, router_with


def confirm(request: Request, body: ConfirmBody) -> dict[str, bool]:
    if not tokens.verify(body.user, body.token):
        raise HTTPException(status_code=400, detail="invalid_token")
    ev = sg_event(request, body.user, body.signalgate_log, "playground.success")
    if ev:
        sg().log(ev)
    return {"ok": True}


router = router_with(confirm_handler=confirm)
