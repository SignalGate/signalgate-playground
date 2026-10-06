"""A tiny ASGI app standing in for the sample app; used only by launcher tests.

Answers the control routes the launcher and the control client rely on:
health, metrics and shutdown (which answers once and then exits by itself --
no signal ever needed), plus stub ledger, calls and canary routes so a
control-client test can exercise every route without a real facade behind
it. All are gated by the same per-boot bearer token the real sample app
would require.
"""

from __future__ import annotations

import json
import os
import signal

from fastapi import BackgroundTasks, FastAPI, HTTPException, Request

app = FastAPI()

BOOT_ID = "stub-boot-id"

# Set only by a test that wants to see the order of requests this boot saw.
_LOG_PATH = os.environ.get("PLAYGROUND_FIXTURE_LOG")

# Set only by a test that wants to script this boot's /metrics answers.
_METRICS_PATH = os.environ.get("PLAYGROUND_FIXTURE_METRICS_FILE")


def _record(kind: str, **fields: object) -> None:
    if not _LOG_PATH:
        return
    with open(_LOG_PATH, "a", encoding="utf-8") as handle:
        handle.write(json.dumps({"kind": kind, **fields}) + "\n")


def _on_sigterm(signum: int, _frame: object) -> None:
    # Evidence for tests that want to prove the launcher's HTTP shutdown
    # path never falls back to a signal: this fixture records the signal
    # itself instead of just going quiet, so "no signal event followed the
    # request" is an assertion over real events, not an absence of proof.
    _record("signal", signum=signum)
    os._exit(0)


signal.signal(signal.SIGTERM, _on_sigterm)


class _UnreadableMetrics(Exception):
    """The scripted metrics file exists but doesn't hold a JSON object."""


def _read_metrics() -> dict:
    """The scripted counters, or ``{}`` when this boot has none scripted.

    A file that exists but can't be read as a JSON object is never taken
    for ``{}``: that would look like an empty queue.
    """
    if not _METRICS_PATH:
        return {}
    try:
        with open(_METRICS_PATH, encoding="utf-8") as handle:
            text = handle.read()
    except FileNotFoundError:
        return {}
    except OSError as exc:
        raise _UnreadableMetrics from exc
    try:
        body = json.loads(text)
    except ValueError as exc:
        raise _UnreadableMetrics from exc
    if not isinstance(body, dict):
        raise _UnreadableMetrics
    return body


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


@app.get("/__playground/metrics")
def metrics(request: Request):
    _authorize(request)
    try:
        body = _read_metrics()
    except _UnreadableMetrics:
        _record("request", path="/__playground/metrics", served=None)
        raise HTTPException(status_code=500) from None
    _record("request", path="/__playground/metrics", served=body)
    return body


def _exit_now() -> None:
    os._exit(0)


@app.post("/__playground/shutdown")
def shutdown(request: Request, background: BackgroundTasks):
    _authorize(request)
    _record("request", path="/__playground/shutdown")
    background.add_task(_exit_now)
    try:
        final = _read_metrics()
    except _UnreadableMetrics:
        final = {}
    return {"ok": True, "boot_id": BOOT_ID, "closed": True, "metrics": final}


@app.get("/__playground/ledger")
def ledger(request: Request):
    _authorize(request)
    _record("request", path="/__playground/ledger")
    return {"entries": [], "cursor": 0, "more": False}


@app.get("/__playground/calls")
def calls(request: Request):
    _authorize(request)
    _record("request", path="/__playground/calls")
    return {
        "v": 1,
        "boot_id": BOOT_ID,
        "records": [],
        "cursor": 0,
        "more": False,
        "oldest_cursor": 0,
        "truncated": False,
        "record_errors": 0,
    }


@app.post("/__playground/canary")
async def canary(request: Request):
    _authorize(request)
    _record("request", path="/__playground/canary")
    body = await request.json()
    items = body.get("canaries", [])
    return {"registered": len(items)}
