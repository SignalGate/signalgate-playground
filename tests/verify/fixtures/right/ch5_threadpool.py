"""Chapter 5 with an async def action: the gate runs in Starlette's threadpool, as a helper."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel
from signalgate import SignalGateError
from starlette.concurrency import run_in_threadpool

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


def blocked(request: Request, body: ActionBody) -> bool:
    """True only when SignalGate answers block; any error lets the request through."""
    ev = sg_event(request, body.user, body.signalgate, "playground.action")
    if not ev:
        return False
    try:
        return sg().check(ev).action == "block"
    except SignalGateError:
        return False


@router.post("/api/action")
async def action(request: Request, body: ActionBody) -> dict[str, bool]:
    if await run_in_threadpool(blocked, request, body):
        raise HTTPException(status_code=403, detail="Request could not be completed")
    perform_action(body.user)
    ev = sg_event(request, body.user, body.signalgate_log, "playground.action")
    if ev:
        sg().log(ev)
    return {"ok": True}


@router.post("/api/confirm")
def confirm(request: Request, body: ConfirmBody) -> dict[str, bool]:
    if not tokens.verify(body.user, body.token):
        raise HTTPException(status_code=400, detail="invalid_token")
    ev = sg_event(request, body.user, body.signalgate, "playground.success")
    if ev:
        sg().log(ev)
    return {"ok": True}
