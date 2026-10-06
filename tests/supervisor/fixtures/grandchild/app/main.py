"""A stub app that spawns its own long-lived subprocess, inheriting fd 0.

Used to prove that stopping this app doesn't wait on that grandchild: a
subprocess started with no ``stdin=`` override inherits the parent's file
descriptor 0 (here, the launcher's own pipe), and keeps it open even after
this app exits, on both Python 3.10 and 3.12.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

from fastapi import BackgroundTasks, FastAPI, HTTPException, Request

app = FastAPI()

BOOT_ID = "stub-boot-id"

_LOG_PATH = os.environ.get("PLAYGROUND_FIXTURE_LOG")

# A grandchild that never returns, started once, that does nothing but hold
# the inherited descriptor open; no numeric timeout needed since it's only
# ever ended by the test's own teardown.
_GRANDCHILD_SOURCE = "import threading; threading.Event().wait()"
_grandchild: subprocess.Popen | None = None


def _record(kind: str, **fields: object) -> None:
    if not _LOG_PATH:
        return
    with open(_LOG_PATH, "a", encoding="utf-8") as handle:
        handle.write(json.dumps({"kind": kind, **fields}) + "\n")


def _authorize(request: Request) -> None:
    token = os.environ.get("PLAYGROUND_CONTROL_TOKEN")
    if not token or request.headers.get("Authorization") != f"Bearer {token}":
        raise HTTPException(status_code=401)


@app.on_event("startup")
def _spawn_grandchild() -> None:
    global _grandchild
    # No stdin= override: the default is to inherit fd 0 from this process.
    _grandchild = subprocess.Popen([sys.executable, "-c", _GRANDCHILD_SOURCE])
    _record("grandchild", pid=_grandchild.pid)


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


def _exit_now() -> None:
    os._exit(0)


@app.post("/__playground/shutdown")
def shutdown(request: Request, background: BackgroundTasks):
    _authorize(request)
    _record("request", path="/__playground/shutdown")
    background.add_task(_exit_now)
    return {"ok": True, "boot_id": BOOT_ID, "closed": True, "metrics": None}
