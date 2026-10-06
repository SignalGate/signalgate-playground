"""Exceptions a route can raise, and the handlers that turn them into a
response. `app.main` registers these handlers; nothing else builds one of
these responses by hand.
"""

from __future__ import annotations

from fastapi import Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse


class InvalidUser(Exception):
    """`user` failed validation."""


class ActionFailed(Exception):
    """The caller asked this request to fail the action step on purpose."""


async def invalid_user_handler(request: Request, exc: InvalidUser) -> JSONResponse:
    return JSONResponse(status_code=400, content={"ok": False, "error": "invalid_user"})


async def action_failed_handler(request: Request, exc: ActionFailed) -> JSONResponse:
    return JSONResponse(status_code=502, content={"ok": False, "error": "action_failed"})


async def validation_error_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    return JSONResponse(status_code=400, content={"ok": False, "error": "validation_error"})


__all__ = [
    "ActionFailed",
    "InvalidUser",
    "action_failed_handler",
    "invalid_user_handler",
    "validation_error_handler",
]
