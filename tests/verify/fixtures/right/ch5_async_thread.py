"""Chapter 5 with async def handlers: check() runs in a thread, so it never blocks the loop."""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel
from signalgate import SignalGateError

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
async def action(request: Request, body: ActionBody) -> dict[str, bool]:
    ev = sg_event(request, body.user, body.signalgate, "playground.action")
    if ev:
        try:
            result = await asyncio.to_thread(sg().check, ev)
        except SignalGateError:
            result = None
        if result is not None and result.action == "block":
            raise HTTPException(status_code=403, detail="Request could not be completed")
    perform_action(body.user)
    ev = sg_event(request, body.user, body.signalgate_log, "playground.action")
    if ev:
        sg().log(ev)
    return {"ok": True}


@router.post("/api/confirm")
async def confirm(request: Request, body: ConfirmBody) -> dict[str, bool]:
    if not tokens.verify(body.user, body.token):
        raise HTTPException(status_code=400, detail="invalid_token")
    ev = sg_event(request, body.user, body.signalgate, "playground.success")
    if ev:
        sg().log(ev)
    return {"ok": True}
