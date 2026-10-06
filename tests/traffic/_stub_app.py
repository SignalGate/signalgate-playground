"""A tiny stand-in for the sample app, used only to test the offline driver.

Not the real sample app (that's ``app/**``, owned elsewhere): just enough of
the action / inbox / confirm dance. ``StubApp`` runs it as a real subprocess
on a free loopback port, the same way the launcher runs the sample app, so
the driver exercises a genuine ``httpx.AsyncClient`` round trip rather than
an in-process shortcut.
"""

from __future__ import annotations

import asyncio
import os
import socket
import sys
from pathlib import Path

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

CONTROL_TOKEN = "stub-control-token"

_DELAY_ENV_VAR = "PLAYGROUND_STUB_ACTION_DELAY_S"
_REPO_ROOT = Path(__file__).resolve().parents[2]


def build_app(*, action_delay_s: float = 0.0) -> FastAPI:
    """Build the stub app. ``action_delay_s`` widens the driver's admission
    window in tests that stop a run mid-flight; it plays no role otherwise.
    """
    app = FastAPI()
    app.state.tokens = {}
    app.state.recorded = []

    @app.middleware("http")
    async def _record_request(request: Request, call_next):
        if not request.url.path.startswith("/__test__"):
            app.state.recorded.append({"path": request.url.path, "headers": dict(request.headers)})
        return await call_next(request)

    @app.get("/")
    async def ready():
        return {"ok": True}

    @app.get("/__test__/requests")
    async def recorded_requests():
        return {"requests": app.state.recorded}

    @app.post("/api/action")
    async def action(request: Request):
        if action_delay_s:
            await asyncio.sleep(action_delay_s)
        body = await request.json()
        user = body.get("user")
        if not user:
            return JSONResponse({"ok": False}, status_code=400)
        ref = request.headers.get("X-Playground-Ref")
        app.state.tokens[ref] = f"tok-{ref}"
        return {"ok": True}

    @app.get("/__playground/inbox")
    async def inbox(request: Request):
        auth = request.headers.get("Authorization", "")
        if auth != f"Bearer {CONTROL_TOKEN}":
            return JSONResponse({}, status_code=401)
        refs = request.query_params.getlist("ref")
        tokens = {}
        for ref in refs:
            token = app.state.tokens.pop(ref, None)
            if token is not None:
                tokens[ref] = token
        return {"tokens": tokens}

    @app.post("/api/confirm")
    async def confirm(request: Request):
        body = await request.json()
        token = body.get("token")
        if isinstance(token, str) and token.startswith("tok-"):
            return {"ok": True}
        return JSONResponse({"ok": False}, status_code=400)

    return app


# Importable as ``tests.traffic._stub_app:app`` by the subprocess ``StubApp``
# launches; the delay is threaded through an env var set before spawning.
app = build_app(action_delay_s=float(os.environ.get(_DELAY_ENV_VAR, "0")))


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class StubApp:
    """Runs the stub app as a real subprocess for the life of an ``async with``."""

    def __init__(self, *, action_delay_s: float = 0.0) -> None:
        self._action_delay_s = action_delay_s
        self._process: asyncio.subprocess.Process | None = None
        self._base_url: str | None = None

    async def requests_for(self, path: str) -> list[dict[str, str]]:
        """The headers of every request the stub has seen for ``path``, in order."""
        assert self._base_url is not None
        async with httpx.AsyncClient(base_url=self._base_url, trust_env=False) as client:
            response = await client.get("/__test__/requests")
            response.raise_for_status()
            entries = response.json()["requests"]
        return [entry["headers"] for entry in entries if entry["path"] == path]

    async def __aenter__(self) -> str:
        port = _free_port()
        base_url = f"http://127.0.0.1:{port}"
        self._base_url = base_url
        env = os.environ | {_DELAY_ENV_VAR: str(self._action_delay_s)}

        self._process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "uvicorn",
            "tests.traffic._stub_app:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--no-access-log",
            cwd=_REPO_ROOT,
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )

        async with httpx.AsyncClient(base_url=base_url, trust_env=False) as client:
            for _ in range(200):
                if self._process.returncode is not None:
                    output = (await self._process.stdout.read()).decode(errors="replace")
                    raise RuntimeError(f"stub app exited before it was ready:\n{output}")
                try:
                    response = await client.get("/")
                    if response.status_code == 200:
                        return base_url
                except httpx.HTTPError:
                    pass
                await asyncio.sleep(0.02)

        await self.__aexit__()
        raise RuntimeError("stub app did not become ready in time")

    async def __aexit__(self, *exc_info: object) -> None:
        process = self._process
        if process is None or process.returncode is not None:
            return
        process.terminate()
        try:
            await asyncio.wait_for(process.wait(), timeout=5)
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()
