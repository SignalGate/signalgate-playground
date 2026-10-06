"""Control routes the console uses to steer and inspect the sample app.

These are operator routes, not part of the app under test: every one of them
requires the per-boot bearer token, and every one refuses a request that
carries `X-Forwarded-For` (a real visitor never has a reason to reach them).
Every route here is `async def`, so none of them is ever scheduled on the
AnyIO threadpool; in particular `/health` never waits behind whatever a
`def` route handler in `app/routes.py` is doing there.
"""

from __future__ import annotations

import asyncio
import hmac
import os
import uuid
from concurrent.futures import ThreadPoolExecutor
from importlib import metadata
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel

from app import signalgate_client
from app.kit import kit_transport, lifecycle, self_test
from app.kit.calls import calls
from app.kit.canary import canaries
from app.kit.inbox import take as take_inbox_token
from app.kit.local_constants import CLIENT_CLOSE_TIMEOUT_S
from app.kit.sequence import ledger

BOOT_ID = uuid.uuid4().hex

# The SDK's join and close never run on a request's own AnyIO task; this
# dedicated, single-thread executor keeps /shutdown's wait off that pool.
_SHUTDOWN_EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix="playground-shutdown")


def _expected_token() -> str:
    return os.environ.get("PLAYGROUND_CONTROL_TOKEN", "")


def _token_bytes(text: str) -> bytes:
    """`text` as the bytes a client would send for it.

    An environment value that wasn't valid UTF-8 comes back to its original
    bytes; anything else that can't be encoded still becomes *some* bytes,
    so the comparison below always has two byte strings to work with.
    """
    try:
        return text.encode("utf-8", "surrogateescape")
    except UnicodeEncodeError:
        return text.encode("utf-8", "surrogatepass")


def _presented_authorization(request: Request) -> bytes:
    """The request's own `Authorization` bytes, exactly as they arrived."""
    for name, value in request.headers.raw:
        if name.lower() == b"authorization":
            return value
    return b""


async def _authorize(request: Request) -> None:
    if "x-forwarded-for" in request.headers:
        raise HTTPException(status_code=403, detail="forbidden")
    token = _expected_token()
    # Without a configured token the control routes stay closed.
    if not token:
        raise HTTPException(status_code=401, detail="unauthorized")
    # Bytes on both sides: a header may carry any byte, and a wrong token
    # with a non-ASCII byte in it is still just a wrong token.
    expected = b"Bearer " + _token_bytes(token)
    if not hmac.compare_digest(_presented_authorization(request), expected):
        raise HTTPException(status_code=401, detail="unauthorized")


def _read_sdk_version() -> str:
    """Read the installed SDK's version without importing it."""
    try:
        return metadata.version("signalgate")
    except metadata.PackageNotFoundError:
        return "unknown"


# Read once at import: health() reads module state only, never the disk.
_SDK_VERSION = _read_sdk_version()


def _validation_error(loc: tuple[str, ...], message: str) -> RequestValidationError:
    """A `RequestValidationError` so a semantic 400 goes through the one
    handler every other validation failure already uses."""
    return RequestValidationError([{"loc": loc, "msg": message, "type": "value_error"}])


router = APIRouter(prefix="/__playground", dependencies=[Depends(_authorize)])


@router.get("/health")
async def health() -> dict[str, Any]:
    outcome = self_test.result()
    clients_created, client_ready = signalgate_client.client_state()
    return {
        "boot_id": BOOT_ID,
        "sdk": "signalgate",
        "sdk_version": _SDK_VERSION,
        "methods": list(kit_transport.METHODS),
        "clients_created": clients_created,
        "client_ready": client_ready,
        "self_test": {
            "ok": outcome.ok,
            "failed": list(outcome.failed),
            "checks": dict(outcome.checks),
        },
        "key": signalgate_client.key_state(),
    }


@router.get("/metrics")
async def read_metrics() -> dict[str, int]:
    return signalgate_client.metrics_snapshot()


@router.get("/ledger")
async def read_ledger(
    run: str | None = None, cursor: int = 0, limit: int | None = None
) -> dict[str, Any]:
    try:
        entries, next_cursor, more = ledger.read_page(cursor=cursor, limit=limit, run=run)
    except ValueError as exc:
        raise _validation_error(("query", "cursor"), str(exc)) from exc
    return {"entries": entries, "cursor": next_cursor, "more": more}


@router.get("/calls")
async def read_calls(cursor: int = 0, limit: int | None = None) -> dict[str, Any]:
    try:
        records, next_cursor, more, oldest_cursor, truncated = calls.read_page(
            cursor=cursor, limit=limit
        )
    except ValueError as exc:
        raise _validation_error(("query", "cursor"), str(exc)) from exc
    return {
        "v": 1,
        "boot_id": BOOT_ID,
        "records": records,
        "cursor": next_cursor,
        "more": more,
        "oldest_cursor": oldest_cursor,
        "truncated": truncated,
        "record_errors": calls.record_errors,
    }


class CanaryBody(BaseModel):
    canaries: list[Any]


@router.post("/canary")
async def register_canaries(body: CanaryBody) -> dict[str, int]:
    try:
        registered = canaries.register(body.canaries)
    except ValueError as exc:
        raise _validation_error(("body", "canaries"), str(exc)) from exc
    return {"registered": registered}


def _consume_outcome(future: asyncio.Future[Any]) -> None:
    """Read a finished close's outcome so a late error is never reported as
    unretrieved after `/shutdown` has already stopped waiting for it."""
    if not future.cancelled():
        future.exception()


async def _close_client_bounded() -> bool:
    """Run `close_client()` on the dedicated executor and wait for it for at
    most `CLIENT_CLOSE_TIMEOUT_S`, the same bound the SDK's own join gets.
    Never raises: a slow or failing close still lets `/shutdown` answer, and
    the process still asks itself to exit, well inside the console's own
    grace before it stops the app.

    Returns whether the call actually returned, with no error, inside the
    bound. A timeout or an exception both count as "not closed": `/shutdown`
    must never claim the client is closed, or hand back a metrics snapshot
    taken mid-close, just because a client had been created. A close still
    running when the wait ends carries on in the executor."""
    loop = asyncio.get_running_loop()
    future = loop.run_in_executor(_SHUTDOWN_EXECUTOR, signalgate_client.close_client)
    future.add_done_callback(_consume_outcome)
    # `asyncio.wait` leaves the future running on a timeout instead of
    # cancelling it, so its outcome can still be read here.
    await asyncio.wait({future}, timeout=CLIENT_CLOSE_TIMEOUT_S)
    if not future.done() or future.cancelled():
        return False
    return future.exception() is None


async def _exit_after_response() -> None:
    """Ask the process to exit, from the event loop itself: a background
    task that is a plain function would wait for a free AnyIO worker
    thread, and a pool kept busy by slow route handlers would hold the exit
    back until the console stopped the app the hard way."""
    lifecycle.request_exit()


@router.post("/shutdown")
async def shutdown(background_tasks: BackgroundTasks) -> dict[str, Any]:
    close_completed = await _close_client_bounded()
    _, client_ready = signalgate_client.client_state()
    closed = close_completed and client_ready
    metrics = signalgate_client.metrics_snapshot() if closed else None
    # The response goes out before the process starts leaving.
    background_tasks.add_task(_exit_after_response)
    return {"ok": True, "boot_id": BOOT_ID, "closed": closed, "metrics": metrics}


@router.get("/inbox")
def read_inbox(ref: list[str] = Query(default=[])) -> dict[str, Any]:  # noqa: B008
    tokens: dict[str, str] = {}
    for one_ref in ref:
        delivered = take_inbox_token(one_ref)
        if delivered is not None:
            token, run = delivered
            tokens[one_ref] = token
            ledger.append({"kind": "inbox", "ref": one_ref, "run": run})
    return {"tokens": tokens}


__all__ = ["BOOT_ID", "router"]
