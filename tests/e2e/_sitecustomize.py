"""Test-only start-up hook for the end-to-end tests.

``tests/e2e/seam.py`` copies this file into a temporary directory as
``sitecustomize.py`` and puts that directory on ``PYTHONPATH``, so Python
imports it at start-up in the console and in the sample app it launches.
Nothing in the product imports it or knows it exists.

In every process it refuses any socket connect that isn't to loopback,
which proves a run makes no outbound network call.

Everything else happens only in the sample app's own process (the one the
console starts with a control token in its environment), and only when the
test hands it a config file:

- a boot file, ``boot-<pid>.json``, with the process id, its port and its
  control token, plus its boot id once the control routes are imported, so
  a test can read ``/__playground/health`` and knows which processes are
  its own;
- the SDK's HTTP calls are answered here, never sent: a log is accepted, a
  check gets an allow verdict, anything else fails to connect. Log
  deliveries can be held until the test creates a release file;
- one line per log delivery in ``deliveries-<pid>.jsonl``: the event's
  method and nonce, the idempotency key, and a sha256 of the authorization
  value (never the value itself);
- right after an SDK client closes, its counters go to
  ``closed-<pid>-<n>.json``, with the SDK worker threads alive just before
  and just after the close;
- at interpreter exit, ``exit-<pid>.json`` lists the SDK worker threads
  still alive.

With a ``fake_url`` in the config (the test-only fake of the playground
API, served on loopback by the test), the SDK's check and log calls go on
to that fake instead of being answered here, and in the console's own
process the playground API client gets a transport that dials the fake:
the one seam the client has, used the way a unit test uses it.

It never imports the SDK or ``requests`` itself. The patches go in when the
app first imports those modules, so the app's own import order is
unchanged.
"""

from __future__ import annotations

import sys

if __name__ == "sitecustomize":
    import atexit
    import hashlib
    import ipaddress
    import itertools
    import json
    import os
    import socket
    import threading
    import time

    _CONFIG_ENV = "PLAYGROUND_E2E_SEAM"
    _TOKEN_ENV = "PLAYGROUND_CONTROL_TOKEN"
    _LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost"}
    _WORKER_THREAD = "signalgate-log-worker"

    def _is_loopback(address: object) -> bool:
        host = address[0] if isinstance(address, tuple) and address else address
        if not isinstance(host, str):
            return True  # not an INET/INET6 address (an AF_UNIX path, say)
        if host in _LOOPBACK_HOSTS:
            return True
        try:
            return ipaddress.ip_address(host).is_loopback
        except ValueError:
            return False

    def _audit_hook(event: str, args: tuple) -> None:
        if event != "socket.connect":
            return
        sock, address = args
        if getattr(sock, "family", None) not in (socket.AF_INET, socket.AF_INET6):
            return
        if not _is_loopback(address):
            raise RuntimeError(f"blocked non-loopback connect: {address!r}")

    sys.addaudithook(_audit_hook)

    def _write_json(path: str, data: object) -> None:
        temporary = f"{path}.tmp"
        with open(temporary, "w", encoding="utf-8") as handle:
            json.dump(data, handle)
        os.replace(temporary, path)

    def _worker_threads() -> list[str]:
        return sorted(t.name for t in threading.enumerate() if t.name == _WORKER_THREAD)

    class _AfterImport:
        """Runs a patch right after a named module first finishes importing."""

        def __init__(self, hooks: dict) -> None:
            self._hooks = hooks

        def find_spec(self, fullname, path=None, target=None):
            hook = self._hooks.pop(fullname, None)
            if hook is None:
                return None
            spec = None
            for finder in sys.meta_path:
                if finder is self:
                    continue
                find_spec = getattr(finder, "find_spec", None)
                if find_spec is None:
                    continue
                spec = find_spec(fullname, path, target)
                if spec is not None:
                    break
            if spec is None or spec.loader is None:
                return spec
            exec_module = spec.loader.exec_module

            def exec_then_patch(module) -> None:
                exec_module(module)
                hook(module)

            spec.loader.exec_module = exec_then_patch
            return spec

    def _install(config: dict) -> None:
        dump_dir = config["dump_dir"]
        release_file = config.get("release_file")
        poll_s = config["poll_s"]
        pid = os.getpid()
        boot_path = os.path.join(dump_dir, f"boot-{pid}.json")
        deliveries_path = os.path.join(dump_dir, f"deliveries-{pid}.jsonl")
        write_lock = threading.Lock()
        close_numbers = itertools.count(1)

        port = None
        argv = list(sys.argv)
        if "--port" in argv:
            port = int(argv[argv.index("--port") + 1])
        boot = {
            "pid": pid,
            "port": port,
            "token": os.environ[_TOKEN_ENV],
            "started_ns": time.time_ns(),
        }
        _write_json(boot_path, boot)

        def after_control(module) -> None:
            _write_json(boot_path, {**boot, "boot_id": module.BOOT_ID})

        def after_adapters(module) -> None:
            from requests.exceptions import ConnectionError as RequestsConnectionError

            def record(request, body: dict) -> None:
                payload = body.get("payload")
                nonce = payload.get("nonce") if isinstance(payload, dict) else None
                authorization = request.headers.get("Authorization", "")
                line = {
                    "method": body.get("method"),
                    "nonce": nonce,
                    "idempotency_key": request.headers.get("Idempotency-Key"),
                    "auth_sha256": hashlib.sha256(authorization.encode()).hexdigest(),
                }
                with write_lock, open(deliveries_path, "a", encoding="utf-8") as handle:
                    handle.write(json.dumps(line) + "\n")

            def send(adapter, request, *args, **kwargs):
                path = (request.path_url or "").split("?", 1)[0]
                if path.endswith("/v0/log"):
                    if release_file:
                        while not os.path.exists(release_file):
                            time.sleep(poll_s)
                    raw = request.body or b"{}"
                    body = json.loads(raw.decode("utf-8") if isinstance(raw, bytes) else raw)
                    record(request, body)
                    reply = {"ok": True, "request_id": "req-e2e", "error": None, "data": None}
                elif path.endswith("/v0/check"):
                    reply = {
                        "ok": True,
                        "request_id": "req-e2e",
                        "error": None,
                        "data": {
                            "action": "allow",
                            "request_id": "req-e2e",
                            "processing_time_us": 0,
                        },
                    }
                else:
                    raise RequestsConnectionError("no such endpoint here", request=request)
                response = module.Response()
                response.status_code = 200
                response._content = json.dumps(reply).encode("utf-8")
                response.headers = module.CaseInsensitiveDict(
                    {"Content-Type": "application/json", "X-Request-Id": "req-e2e"}
                )
                response.encoding = "utf-8"
                response.url = request.url
                response.request = request
                response.reason = "stubbed"
                response.connection = adapter
                return response

            module.HTTPAdapter.send = send

        def after_client(module) -> None:
            original_close = module.Client.close

            def close(self, *args, **kwargs):
                before = _worker_threads()
                try:
                    return original_close(self, *args, **kwargs)
                finally:
                    number = next(close_numbers)
                    _write_json(
                        os.path.join(dump_dir, f"closed-{pid}-{number}.json"),
                        {
                            "pid": pid,
                            "metrics": self.metrics.snapshot_flat(),
                            "workers_before": before,
                            "workers_after": _worker_threads(),
                        },
                    )

            module.Client.close = close

        def at_exit() -> None:
            _write_json(
                os.path.join(dump_dir, f"exit-{pid}.json"),
                {"pid": pid, "workers": _worker_threads()},
            )

        def after_adapters_forward(module) -> None:
            from requests.exceptions import ConnectionError as RequestsConnectionError

            fake_url = config["fake_url"]
            original_send = module.HTTPAdapter.send

            def send(adapter, request, *args, **kwargs):
                path = (request.path_url or "").split("?", 1)[0]
                if not path.endswith(("/v0/log", "/v0/check")):
                    raise RequestsConnectionError("no such endpoint here", request=request)
                request.url = fake_url + path
                request.headers["Host"] = fake_url.split("://", 1)[1]
                kwargs["proxies"] = {}
                return original_send(adapter, request, *args, **kwargs)

            module.HTTPAdapter.send = send

        hooks = {"app.kit.control": after_control}
        if config.get("fake_url"):
            hooks["requests.adapters"] = after_adapters_forward
        elif config.get("stub", True):
            hooks["requests.adapters"] = after_adapters
            hooks["signalgate.client"] = after_client
            atexit.register(at_exit)
        sys.meta_path.insert(0, _AfterImport(hooks))

    def _install_console(config: dict) -> None:
        """In the console's process: its playground API client dials the fake."""
        scheme_and_host = config["fake_url"].split("://", 1)[1]
        host, port = scheme_and_host.rsplit(":", 1)

        def after_api_client(module) -> None:
            import httpx

            class _ToFake(httpx.AsyncHTTPTransport):
                async def handle_async_request(self, request):
                    request.url = request.url.copy_with(scheme="http", host=host, port=int(port))
                    request.headers["Host"] = scheme_and_host
                    return await super().handle_async_request(request)

            original_init = module.PlaygroundApi.__init__

            def init(self, *args, transport=None, **kwargs):
                original_init(self, *args, transport=transport or _ToFake(), **kwargs)

            module.PlaygroundApi.__init__ = init

        sys.meta_path.insert(0, _AfterImport({"playground.api.client": after_api_client}))

    def _config() -> dict | None:
        path = os.environ.get(_CONFIG_ENV)
        if not path:
            return None
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)

    _loaded = _config()
    if _loaded is not None:
        if os.environ.get(_TOKEN_ENV):
            _install(_loaded)
        elif _loaded.get("fake_url"):
            _install_console(_loaded)
