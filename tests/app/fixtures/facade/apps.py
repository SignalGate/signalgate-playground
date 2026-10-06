"""Small FastAPI apps used only by the facade's own tests.

None of these is the sample app; they exercise `sg()`, `sg_event()` and the
construction hook through real request handling, without touching
`app/routes.py` (frozen and pinned, see `test_routes_pristine.py`).
"""

from __future__ import annotations

from typing import Any

import signalgate
from fastapi import APIRouter, FastAPI, Request

from app.signalgate_client import sg, sg_event

router = APIRouter()

# A key used only to prove the construction hook counts a Client built
# directly, outside `sg()`. Never a real key; never sent anywhere (the
# route closes the client without calling `check()` or `log()` on it).
_DIRECT_KEY = "dummy-direct-key-0123456789"


@router.post("/taught/{outcome}")
async def taught_pattern(outcome: str, request: Request) -> dict[str, bool]:
    """The pattern every app is taught to use. Must return 200 whatever `outcome` is."""
    envelope: Any = None
    if outcome == "valid":
        envelope = {"encrypted": "cipher", "nonce": "nonce-1", "timestamp": 1_700_000_000}
    elif outcome == "malformed":
        envelope = {"encrypted": "cipher"}  # missing nonce and timestamp
    ev = sg_event(request, "user-1", envelope, "playground.action")
    if ev:
        sg().log(ev)
    return {"ok": True}


@router.post("/sync-log")
def sync_log() -> dict[str, bool]:
    """A ``def`` route: FastAPI dispatches it on the AnyIO threadpool."""
    sg().log(_sample_event())
    return {"ok": True}


@router.post("/async-log")
async def async_log() -> dict[str, bool]:
    """An ``async def`` route: it runs on the event loop itself."""
    sg().log(_sample_event())
    return {"ok": True}


@router.post("/direct-client")
def direct_client() -> dict[str, bool]:
    """Builds a ``Client`` directly, never through ``sg()``. Proves the
    construction hook counts and records it too, as a non-singleton."""
    client = signalgate.Client(_DIRECT_KEY)
    client.close()
    return {"ok": True}


def _sample_event() -> signalgate.Event:
    return signalgate.Event(
        user_id="user-1",
        ip="203.0.113.9",
        method="playground.action",
        timestamp="2026-01-01T00:00:00+00:00",
        payload=signalgate.EncryptedPayload(
            encrypted="cipher", timestamp=1_700_000_000, nonce="nonce-1"
        ),
    )


def build_app() -> FastAPI:
    app = FastAPI()
    app.include_router(router)
    return app


app = build_app()

__all__ = ["app", "build_app", "router"]
