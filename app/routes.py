"""Route handlers for the sample app.

Keep this file simple: it's the one file meant for editing once you start
wiring SignalGate into your own app. Everything under `app/kit/` is
scaffolding that stays out of the way.

The action below stands in for whatever a real app protects against abuse:
sending a one-time code, starting a free trial, creating an account. The
success step is what a real user goes on to do once the action worked:
entering the code, activating the trial, a first purchase. `token` stands in
for any out-of-band confirmation (an emailed code, a link clicked
elsewhere); the response never contains it.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from app.kit import tokens
from app.kit.actions import perform_action

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
    return {"ok": True}


@router.post("/api/confirm")
def confirm(request: Request, body: ConfirmBody) -> dict[str, bool]:
    # the user succeeded here
    if not tokens.verify(body.user, body.token):
        raise HTTPException(status_code=400, detail="invalid_token")
    return {"ok": True}


__all__ = ["ActionBody", "ConfirmBody", "router"]
