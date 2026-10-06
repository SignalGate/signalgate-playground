"""Chapter 3 in another style: one helper for both logs, get_client(), named method constants."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from app.kit import tokens
from app.kit.actions import perform_action
from app.signalgate_client import get_client, sg_event

ACTION = "playground.action"
SUCCESS = "playground.success"

router = APIRouter()


class ActionBody(BaseModel):
    user: str
    signalgate: dict[str, Any] | None = None
    signalgate_log: dict[str, Any] | None = None


class ConfirmBody(BaseModel):
    user: str
    token: str
    signalgate: dict[str, Any] | None = None


def record(request: Request, user: str, envelope: Any, method: str) -> None:
    event = sg_event(request, user, envelope, method)
    if event is None:
        return
    get_client().log(event)


@router.post("/api/action")
def action(request: Request, body: ActionBody) -> dict[str, bool]:
    perform_action(body.user)
    record(request, body.user, body.signalgate_log, ACTION)
    return {"ok": True}


@router.post("/api/confirm")
def confirm(request: Request, body: ConfirmBody) -> dict[str, bool]:
    confirmed = tokens.verify(body.user, body.token)
    if not confirmed:
        raise HTTPException(status_code=400, detail="invalid_token")
    record(request, body.user, body.signalgate, SUCCESS)
    return {"ok": confirmed}
