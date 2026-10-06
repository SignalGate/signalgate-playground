"""A stub app with no ``/shutdown`` route at all.

Used to prove the launcher falls back to a plain ``terminate()`` when there
is nothing to POST to: ordinary uvicorn signal handling then takes over, and
the process exits gracefully well inside the grace.
"""

from __future__ import annotations

import os

from fastapi import FastAPI, HTTPException, Request

app = FastAPI()

BOOT_ID = "stub-boot-id"


def _authorize(request: Request) -> None:
    token = os.environ.get("PLAYGROUND_CONTROL_TOKEN")
    if not token or request.headers.get("Authorization") != f"Bearer {token}":
        raise HTTPException(status_code=401)


@app.get("/__playground/health")
def health(request: Request):
    _authorize(request)
    return {
        "boot_id": BOOT_ID,
        "sdk": "stub",
        "sdk_version": "0",
        "methods": [],
        "clients_created": 0,
        "client_ready": False,
        "self_test": {"ok": True, "failed": [], "checks": {}},
        "key": "placeholder",
    }
