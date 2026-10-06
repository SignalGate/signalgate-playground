"""Keeps each user's action envelope and logs the success with it."""

from __future__ import annotations

from typing import Any

from fastapi import HTTPException, Request

from app.kit import tokens
from app.kit.actions import perform_action
from app.signalgate_client import sg, sg_event
from tests.verify.fixtures.common import ActionBody, ConfirmBody, router_with

kept: dict[str, Any] = {}


def action(request: Request, body: ActionBody) -> dict[str, bool]:
    perform_action(body.user)
    kept[body.user] = body.signalgate_log
    ev = sg_event(request, body.user, body.signalgate_log, "playground.action")
    if ev:
        sg().log(ev)
    return {"ok": True}


def confirm(request: Request, body: ConfirmBody) -> dict[str, bool]:
    if not tokens.verify(body.user, body.token):
        raise HTTPException(status_code=400, detail="invalid_token")
    ev = sg_event(request, body.user, kept.get(body.user), "playground.success")
    if ev:
        sg().log(ev)
    return {"ok": True}


router = router_with(action, confirm)
