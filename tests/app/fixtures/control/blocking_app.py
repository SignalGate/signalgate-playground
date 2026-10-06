"""A small app for the health-latency test.

Mounts the real control router next to two test-only routes that can hog
the AnyIO threadpool (a plain `def`) or block the event loop directly (an
`async def`), so a test can show `/__playground/health` stays fast in the
first case and is delayed in the second -- proving it never waits behind a
`def` route the way `app/routes.py` might, but would be hurt by a badly
written `async def` one the same way any route would.
"""

from __future__ import annotations

import threading
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import anyio.to_thread
from fastapi import FastAPI

from app.kit.control import router as control_router

# A tiny pool makes it trivial to saturate on purpose, from a handful of
# concurrent requests, without waiting around for a large one to fill up.
THREADPOOL_SIZE = 2

# A test sets this to a `threading.Barrier(THREADPOOL_SIZE + 1)` and waits on
# it itself, so it learns exactly when every thread-pool slot is occupied by
# a `/block-thread` call sleeping inside it, with no timing guess either way.
ready_barrier: threading.Barrier | None = None

# A test sets this before calling `/block-loop`, so it can tell exactly when
# the event loop has become blocked, again with no timing guess.
loop_block_started = threading.Event()

# Setting this lets every `/block-thread` call still inside the pool return
# at once, instead of at the end of its own `seconds`.
release_threads = threading.Event()

_in_pool_lock = threading.Lock()
_in_pool = 0


def threads_in_pool() -> int:
    """How many `/block-thread` calls are holding a thread-pool slot right now."""
    with _in_pool_lock:
        return _in_pool


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    anyio.to_thread.current_default_thread_limiter().total_tokens = THREADPOOL_SIZE
    yield


app = FastAPI(lifespan=lifespan)
app.include_router(control_router)


@app.post("/block-thread")
def block_thread(seconds: float) -> dict[str, bool]:
    """A `def` handler: runs on the AnyIO threadpool, like a real route can."""
    global _in_pool
    with _in_pool_lock:
        _in_pool += 1
    try:
        if ready_barrier is not None:
            ready_barrier.wait()
        release_threads.wait(seconds)
    finally:
        with _in_pool_lock:
            _in_pool -= 1
    return {"ok": True}


@app.post("/block-loop")
async def block_loop(seconds: float) -> dict[str, bool]:
    """An `async def` handler that blocks the loop -- what `/health` must not do."""
    loop_block_started.set()
    time.sleep(seconds)  # noqa: ASYNC251 - deliberate: this is the bad handler being tested
    return {"ok": True}
