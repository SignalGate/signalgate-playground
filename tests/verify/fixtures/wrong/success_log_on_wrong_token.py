"""Logs the success before it knows whether the token was right."""

from __future__ import annotations

from fastapi import HTTPException, Request

from app.kit import tokens
from app.signalgate_client import sg, sg_event
from tests.verify.fixtures.common import ConfirmBody, router_with


def confirm(request: Request, body: ConfirmBody) -> dict[str, bool]:
    confirmed = tokens.verify(body.user, body.token)
    ev = sg_event(request, body.user, body.signalgate, "playground.success")
    if ev:
        sg().log(ev)
    if not confirmed:
        raise HTTPException(status_code=400, detail="invalid_token")
    return {"ok": True}


router = router_with(confirm_handler=confirm)
