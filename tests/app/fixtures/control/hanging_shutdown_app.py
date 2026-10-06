"""A tiny app whose lifespan shutdown never finishes on its own.

Run under uvicorn with the stdin lifeline on, this proves the lifeline's
`os._exit(1)` fallback fires when a graceful shutdown doesn't complete in
time: the module patches `LIFELINE_EXIT_GRACE_S` small for itself, here
only, before the app object is built, so the test doesn't wait around for
the real value.
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.kit import lifecycle
from playground.traffic.local_constants import APP_HEALTH_POLL_S, APP_STOP_GRACE_S

# Patched small, in this fixture process only: a handful of the console's
# own health-poll intervals.
lifecycle.LIFELINE_EXIT_GRACE_S = APP_HEALTH_POLL_S * 10

# Longer than the console ever waits for a stop, so without the fallback the
# process would still be here when the test stops waiting for it.
HANG_S = APP_STOP_GRACE_S * 2


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    lifecycle.start_lifeline()
    yield
    time.sleep(HANG_S)  # noqa: ASYNC251 - the point of this fixture is to hang here


app = FastAPI(lifespan=lifespan)


@app.get("/health")
async def health() -> dict[str, bool]:
    return {"ok": True}
