"""FastAPI application object for the sample app.

Wires together the two routes meant for editing (`app.routes`) and the
scaffolding under `app.kit`: request context, the control routes, and the
exception handlers that turn a kit error into the right HTTP response.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError

# Imported (and its construction hook installed) before `app.routes`, in
# case a route ever builds a client at import time. Aliased so this name
# never collides with the `app` FastAPI instance built below.
import app.signalgate_client as sg_client
from app.kit import lifecycle, self_test
from app.kit.context import ContextMiddleware
from app.kit.control import router as control_router
from app.kit.errors import (
    ActionFailed,
    InvalidUser,
    action_failed_handler,
    invalid_user_handler,
    validation_error_handler,
)
from app.routes import router as routes_router


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    # Runs the boot self-test now, under uvicorn's own signal handlers, so
    # `exit_hook` reads "pass" here rather than only on the first /health.
    self_test.result()
    lifecycle.start_lifeline()
    yield
    # Off the loop: the SDK's own close can block briefly.
    await asyncio.to_thread(sg_client.close_client)


app = FastAPI(lifespan=lifespan)
app.add_middleware(ContextMiddleware)
app.include_router(routes_router)
app.include_router(control_router)
app.add_exception_handler(RequestValidationError, validation_error_handler)
app.add_exception_handler(InvalidUser, invalid_user_handler)
app.add_exception_handler(ActionFailed, action_failed_handler)
