"""A sample-app ``routes.py`` that logs to SignalGate, for the end-to-end tests.

Copied over ``app/routes.py`` in a test's temp tree, never used in this
checkout. It follows the pattern the tutorial teaches (build the event with
``sg_event``, then ``if ev: sg().log(ev)``), logging one action in every
``LOG_EVERY``. The visitors of an offline run carry no envelope, so this
file makes a dummy one on the laptop for each logged action. Its ciphertext
is a fixed marker the tests look for: it must never show up in any output.
"""

from __future__ import annotations

import itertools
import threading
import time
import uuid
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from app.kit import tokens
from app.kit.actions import perform_action
from app.signalgate_client import sg, sg_event

router = APIRouter()

LOG_EVERY = 10
DUMMY_CIPHERTEXT = "e2e-dummy-ciphertext-marker"

_counter = itertools.count(1)
_counter_lock = threading.Lock()


class ActionBody(BaseModel):
    user: str
    signalgate: Any = None
    signalgate_log: Any = None


class ConfirmBody(BaseModel):
    user: str
    token: str
    signalgate: Any = None


def _dummy_envelope() -> dict[str, Any]:
    return {
        "encrypted": DUMMY_CIPHERTEXT,
        "timestamp": int(time.time()),
        "nonce": uuid.uuid4().hex,
    }


@router.post("/api/action")
def action(request: Request, body: ActionBody) -> dict[str, bool]:
    # the action happens here
    perform_action(body.user)
    with _counter_lock:
        number = next(_counter)
    if number % LOG_EVERY == 0:
        ev = sg_event(request, body.user, _dummy_envelope(), "playground.action")
        if ev:
            sg().log(ev)
    return {"ok": True}


@router.post("/api/confirm")
def confirm(request: Request, body: ConfirmBody) -> dict[str, bool]:
    # the user succeeded here
    if not tokens.verify(body.user, body.token):
        raise HTTPException(status_code=400, detail="invalid_token")
    return {"ok": True}


__all__ = ["ActionBody", "ConfirmBody", "router"]
