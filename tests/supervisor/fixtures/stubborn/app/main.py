"""A stub app that answers ``/shutdown`` but never actually leaves.

Used to prove the launcher's hard-kill fallback: this app takes the request,
says yes, and then does nothing else -- it also swallows SIGTERM outright, so
only SIGKILL ever ends it.
"""

from __future__ import annotations

import json
import os
import signal

from fastapi import FastAPI, HTTPException, Request

app = FastAPI()

BOOT_ID = "stub-boot-id"

_LOG_PATH = os.environ.get("PLAYGROUND_FIXTURE_LOG")


def _record(kind: str, **fields: object) -> None:
    if not _LOG_PATH:
        return
    with open(_LOG_PATH, "a", encoding="utf-8") as handle:
        handle.write(json.dumps({"kind": kind, **fields}) + "\n")


def _authorize(request: Request) -> None:
    token = os.environ.get("PLAYGROUND_CONTROL_TOKEN")
    if not token or request.headers.get("Authorization") != f"Bearer {token}":
        raise HTTPException(status_code=401)


def _ignore_sigterm(_signum: int, _frame: object) -> None:
    _record("signal", name="SIGTERM")


@app.on_event("startup")
def _install_sigterm_override() -> None:
    # Runs after uvicorn's own Server.run() has installed its handler, so
    # this one replaces it rather than the other way around.
    signal.signal(signal.SIGTERM, _ignore_sigterm)


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


@app.post("/__playground/shutdown")
def shutdown(request: Request):
    _authorize(request)
    _record("request", path="/__playground/shutdown")
    return {"ok": True, "boot_id": BOOT_ID, "closed": True, "metrics": None}
