"""A stub app whose ``/shutdown`` handler blocks the event loop outright.

Used to prove the launcher bounds the whole stop by the grace deadline, not
by however long the shutdown request itself is allowed to wait: this handler
never returns, so the POST never answers and no SIGTERM is ever processed
either (the loop that would run the handler for it is stuck).
"""

from __future__ import annotations

import json
import os
import threading

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
async def shutdown(request: Request):
    _authorize(request)
    _record("request", path="/__playground/shutdown")
    # A synchronous wait with no timeout, called from inside a coroutine with
    # no ``await``: this genuinely freezes the event loop rather than just
    # occupying a threadpool worker, so nothing else this process serves can
    # make progress either -- including its own signal handling.
    threading.Event().wait()
    return {"ok": True, "boot_id": BOOT_ID, "closed": True, "metrics": None}  # unreachable
