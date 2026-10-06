"""Shared parts of the "wrong" fixture routes: the body models and the handlers done right.

Each file under ``wrong/`` gets one thing wrong and takes everything else
from here, so a fixture shows only its own mistake. The files under
``right/`` stand alone, the way a user's ``app/routes.py`` would.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel
from signalgate import SignalGateError

from app.kit import tokens
from app.kit.actions import perform_action
from app.signalgate_client import sg, sg_event


class ActionBody(BaseModel):
    user: str
    signalgate: Any = None
    signalgate_log: Any = None


class ConfirmBody(BaseModel):
    user: str
    token: str
    signalgate: Any = None


def log_action(request: Request, body: ActionBody) -> None:
    ev = sg_event(request, body.user, body.signalgate_log, "playground.action")
    if ev:
        sg().log(ev)


def gate(request: Request, body: ActionBody) -> None:
    ev = sg_event(request, body.user, body.signalgate, "playground.action")
    if ev:
        try:
            if sg().check(ev).action == "block":
                raise HTTPException(status_code=403, detail="Request could not be completed")
        except SignalGateError:
            pass


def action_chapter_3(request: Request, body: ActionBody) -> dict[str, bool]:
    perform_action(body.user)
    log_action(request, body)
    return {"ok": True}


def action_chapter_5(request: Request, body: ActionBody) -> dict[str, bool]:
    gate(request, body)
    perform_action(body.user)
    log_action(request, body)
    return {"ok": True}


def confirm(request: Request, body: ConfirmBody) -> dict[str, bool]:
    if not tokens.verify(body.user, body.token):
        raise HTTPException(status_code=400, detail="invalid_token")
    ev = sg_event(request, body.user, body.signalgate, "playground.success")
    if ev:
        sg().log(ev)
    return {"ok": True}


def router_with(
    action: Callable[..., Any] = action_chapter_3,
    confirm_handler: Callable[..., Any] = confirm,
) -> APIRouter:
    """A router with these two handlers on the sample app's two routes."""
    router = APIRouter()
    router.post("/api/action")(action)
    router.post("/api/confirm")(confirm_handler)
    return router


__all__ = [
    "ActionBody",
    "ConfirmBody",
    "action_chapter_3",
    "action_chapter_5",
    "confirm",
    "gate",
    "log_action",
    "router_with",
]
