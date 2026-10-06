"""Chapter 3, the taught way: log each step after it happens, only when the envelope is there."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from app.kit import tokens
from app.kit.actions import perform_action
from app.signalgate_client import sg, sg_event

router = APIRouter()


class ActionBody(BaseModel):
    user: str
    signalgate: Any = None
    signalgate_log: Any = None


class ConfirmBody(BaseModel):
    user: str
    token: str
    signalgate: Any = None


@router.post("/api/action")
def action(request: Request, body: ActionBody) -> dict[str, bool]:
    # the action happens here
    perform_action(body.user)
    ev = sg_event(request, body.user, body.signalgate_log, "playground.action")
    if ev:
        sg().log(ev)
    return {"ok": True}


@router.post("/api/confirm")
def confirm(request: Request, body: ConfirmBody) -> dict[str, bool]:
    # the user succeeded here
    if not tokens.verify(body.user, body.token):
        raise HTTPException(status_code=400, detail="invalid_token")
    ev = sg_event(request, body.user, body.signalgate, "playground.success")
    if ev:
        sg().log(ev)
    return {"ok": True}
