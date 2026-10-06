"""Chapter 3 with only the action log: the success is never logged."""

from __future__ import annotations

from fastapi import HTTPException, Request

from app.kit import tokens
from tests.verify.fixtures.common import ConfirmBody, router_with


def confirm(request: Request, body: ConfirmBody) -> dict[str, bool]:
    if not tokens.verify(body.user, body.token):
        raise HTTPException(status_code=400, detail="invalid_token")
    return {"ok": True}


router = router_with(confirm_handler=confirm)
