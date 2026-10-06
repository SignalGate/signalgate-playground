"""A standalone app that proves `sg_event`'s `ip` field is `request.client.host`,
never a manual `X-Forwarded-For` parse.

Launched as its own uvicorn subprocess with the exact launch flags the
console uses to start the sample app (see `tests/app/test_sg_event.py`): only
uvicorn's own `--proxy-headers` handling can rewrite `request.client` before
this route ever sees the request, which an in-process `TestClient` never
exercises. This app never builds a SignalGate client, so it needs no API key.
"""

from __future__ import annotations

from fastapi import FastAPI, Request

from app.signalgate_client import sg_event

app = FastAPI()


@app.post("/echo-ip")
async def echo_ip(request: Request) -> dict[str, str | None]:
    envelope = {"encrypted": "cipher", "nonce": "nonce-1", "timestamp": 1_700_000_000}
    ev = sg_event(request, "user-1", envelope, "playground.action")
    return {"ip": ev.ip if ev is not None else None}


__all__ = ["app"]
