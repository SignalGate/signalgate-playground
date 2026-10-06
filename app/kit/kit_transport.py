"""The transport the sample app's SignalGate client sends through.

`KitTransport` sits between the SDK's `Client` and the SDK's own HTTP
transport. It forwards every request unchanged, and adds three things for
the playground:

- a method guard: only the two playground methods (`METHODS`) ever leave the
  laptop; anything else is refused locally with `METHOD_ONLY_CODE`;
- canary answers: a nonce registered in `app.kit.canary` is answered on the
  laptop, never sent;
- one record per attempt in `app.kit.calls`, so the console can see what
  was sent and what came back.

It uses the SDK's HTTP transport from a private module, which is only safe
because the SDK is pinned to exactly `PINNED_SDK_VERSION`. Importing this
module never raises: if that import fails, `IMPORT_ERROR` names the error
class and the boot self-test reports it.
"""

from __future__ import annotations

import platform
import threading
import time
from typing import Any
from urllib.parse import urlsplit

from signalgate import (
    SDK_NAME,
    SDK_VERSION,
    ConfigError,
    HttpResponse,
    ServerError,
    TimeoutError,
)

from app.kit import calls as calls_module
from app.kit.canary import canaries

METHODS = ("playground.action", "playground.success")
METHOD_ONLY_CODE = "PLAYGROUND_METHOD_ONLY"
METHOD_ONLY_STATUS = 400
CANARY_REJECT_CODE = "REQUEST_REJECTED"
PINNED_SDK_VERSION = "0.3.2"

# Both messages are fixed, neutral text written for this kit.
_METHOD_ONLY_MESSAGE = "only the sample app's own request methods are ever sent"
_CANARY_REJECT_MESSAGE = "the request was rejected locally by a registered canary"

CHECK_SUFFIX = "/v0/check"
LOG_SUFFIX = "/v0/log"

IMPORT_ERROR: str | None
try:
    from requests.exceptions import ConnectTimeout as _ConnectTimeout
    from requests.exceptions import Timeout as _RequestsTimeout
    from signalgate._http import HttpTransport as _HttpTransport
except Exception as exc:  # noqa: BLE001 - importing this module must never raise
    _HttpTransport = None
    _ConnectTimeout = None
    _RequestsTimeout = None
    IMPORT_ERROR = type(exc).__name__
else:
    IMPORT_ERROR = None


def user_agent() -> str:
    """The User-Agent a plain ``Client(key)`` sends, built from the SDK's public constants."""
    return (
        f"{SDK_NAME}/{SDK_VERSION} "
        f"(python/{platform.python_version()}; {platform.system().lower()})"
    )


def _endpoint_for(url: str) -> str:
    """``"check"``, ``"log"`` or ``"other"``, from the URL path's suffix only."""
    path = urlsplit(url).path
    if path.endswith(CHECK_SUFFIX):
        return "check"
    if path.endswith(LOG_SUFFIX):
        return "log"
    return "other"


def _nonce_of(body: Any) -> str | None:
    """``body["payload"]["nonce"]`` if that is a str, else None."""
    if not isinstance(body, dict):
        return None
    payload = body.get("payload")
    if not isinstance(payload, dict):
        return None
    nonce = payload.get("nonce")
    return nonce if isinstance(nonce, str) else None


def _is_playground_method(method: Any) -> bool:
    """Whether ``method`` is one of ``METHODS``, byte for byte."""
    return isinstance(method, str) and method in METHODS


def _timeout_phase(cause: BaseException | None) -> str | None:
    """``"connect"`` or ``"read"``, read off a ``TimeoutError``'s ``__cause__``."""
    if cause is None or _ConnectTimeout is None or _RequestsTimeout is None:
        return None
    if isinstance(cause, _ConnectTimeout):
        return "connect"
    if isinstance(cause, _RequestsTimeout):
        return "read"
    return None


def _outcome_fields(
    response: HttpResponse | None, error: BaseException | None
) -> tuple[int | None, str | None, str | None, bool, str | None]:
    """``(status, code, error_class, timed_out, timeout_phase)`` for one attempt."""
    if error is None:
        status = response.status_code if response is not None else None
        return status, None, None, False, None
    if isinstance(error, ServerError):
        return error.status_code, error.code, "ServerError", False, None
    if isinstance(error, TimeoutError):
        return None, None, "TimeoutError", True, _timeout_phase(error.__cause__)
    return None, None, type(error).__name__, False, None


def _verdict_fields(endpoint: str, response: HttpResponse | None) -> tuple[str | None, int | None]:
    """``(verdict, processing_time_us)``, taken from a 2xx check body's ``data``."""
    if endpoint != "check" or response is None or not isinstance(response.body, dict):
        return None, None
    data = response.body.get("data")
    if not isinstance(data, dict):
        return None, None
    action = data.get("action")
    verdict = action if isinstance(action, str) else None
    micros = data.get("processing_time_us")
    processing_time_us = (
        micros if isinstance(micros, int) and not isinstance(micros, bool) else None
    )
    return verdict, processing_time_us


class KitTransport:
    """A ``signalgate.Transport`` that guards, answers canaries and records attempts."""

    def __init__(self, api_key: str, *, logger: Any = None) -> None:
        if IMPORT_ERROR is not None:
            raise ConfigError("the SDK's private HTTP transport is unavailable")
        self._inner = _HttpTransport(api_key, user_agent(), logger)
        self._closed = False
        self._close_lock = threading.Lock()
        # The kit's own first-sight ordinal for log posts: only the last
        # idempotency_key is tracked, since the pinned SDK's one log worker
        # delivers one job's retries consecutively before starting the next.
        self._log_lock = threading.Lock()
        self._last_log_key: str | None = None
        self._last_log_job = 0
        self._last_log_attempt = 0

    def post(
        self,
        url: str,
        body: dict[str, Any],
        *,
        timeout_ms: int,
        request_id: str,
        idempotency_key: str,
    ) -> HttpResponse:
        """Refuse, answer a canary or forward, and record one attempt either way.

        The attempt's ``t0`` and ``t1`` are ``time.perf_counter()`` readings on
        the calling thread, as this starts and as it finishes: the same clock
        as the call records' ``t0`` and ``t1``.
        """
        t0 = time.perf_counter()
        endpoint, call_seq, method, nonce, log_job, attempt = self._bookkeeping(
            url, body, idempotency_key
        )
        local: str | None = None
        response: HttpResponse | None = None
        error: BaseException | None = None
        try:
            if not isinstance(body, dict) or not _is_playground_method(method):
                local = "guard"
                raise ServerError(
                    METHOD_ONLY_STATUS, METHOD_ONLY_CODE, _METHOD_ONLY_MESSAGE, request_id, None
                )
            canary = canaries.lookup(nonce) if endpoint in ("check", "log") else None
            if canary is not None:
                local = "canary"
                response = self._answer_canary(canary, endpoint, url, timeout_ms, request_id)
            else:
                response = self._inner.post(
                    url,
                    body,
                    timeout_ms=timeout_ms,
                    request_id=request_id,
                    idempotency_key=idempotency_key,
                )
        except BaseException as exc:
            error = exc
            raise
        finally:
            t1 = time.perf_counter()
            self._record_attempt(
                endpoint=endpoint,
                call_seq=call_seq,
                log_job=log_job,
                attempt=attempt,
                method=method,
                nonce=nonce,
                idempotency_key=idempotency_key,
                request_id=request_id,
                local=local,
                response=response,
                error=error,
                t0=t0,
                t1=t1,
            )
        return response

    def _bookkeeping(
        self, url: str, body: Any, idempotency_key: str
    ) -> tuple[str, int | None, Any, str | None, int | None, int]:
        """``(endpoint, call_seq, method, nonce, log_job, attempt)``, computed
        defensively so a malformed ``url`` or ``body`` never stops this call:
        each piece falls back on its own, and the forward still goes ahead so
        the inner transport can raise its own error, exactly as it would for
        a plain client given the same input."""
        try:
            endpoint = _endpoint_for(url)
        except Exception:  # noqa: BLE001 - this bookkeeping must never raise
            endpoint = "other"
        try:
            call_seq = calls_module.current_call()
        except Exception:  # noqa: BLE001 - this bookkeeping must never raise
            call_seq = None
        try:
            method = body.get("method") if isinstance(body, dict) else None
        except Exception:  # noqa: BLE001 - this bookkeeping must never raise
            method = None
        try:
            nonce = _nonce_of(body)
        except Exception:  # noqa: BLE001 - this bookkeeping must never raise
            nonce = None
        try:
            log_job, attempt = self._log_numbering(endpoint, idempotency_key)
        except Exception:  # noqa: BLE001 - this bookkeeping must never raise
            log_job, attempt = None, 1
        return endpoint, call_seq, method, nonce, log_job, attempt

    def _answer_canary(
        self,
        canary: Any,
        endpoint: str,
        url: str,
        timeout_ms: int,
        request_id: str,
    ) -> HttpResponse:
        """A local answer for a registered nonce: a verdict, a 422 or a log ack."""
        wait_ms = min(canary.delay_ms, timeout_ms)
        if wait_ms:
            time.sleep(wait_ms / 1000.0)
        if canary.delay_ms >= timeout_ms:
            raise TimeoutError(f"request to {url} timed out after {timeout_ms}ms")
        if endpoint == "log":
            return HttpResponse(
                200, {"ok": True, "request_id": request_id, "error": None, "data": None}, request_id
            )
        if canary.answer == "422":
            raise ServerError(422, CANARY_REJECT_CODE, _CANARY_REJECT_MESSAGE, request_id, None)
        return HttpResponse(
            200,
            {
                "ok": True,
                "request_id": request_id,
                "error": None,
                "data": {
                    "action": canary.answer,
                    "request_id": request_id,
                    "processing_time_us": 0,
                },
            },
            request_id,
        )

    def _log_numbering(self, endpoint: str, idempotency_key: str) -> tuple[int | None, int]:
        """``(log_job, attempt)`` for this post.

        A log post's job is numbered by first sight of its ``idempotency_key``,
        from 1 per transport; its attempt counts from 1 within that job. Any
        other post has no job and is always attempt 1.
        """
        if endpoint != "log":
            return None, 1
        with self._log_lock:
            if idempotency_key == self._last_log_key:
                self._last_log_attempt += 1
            else:
                self._last_log_key = idempotency_key
                self._last_log_job += 1
                self._last_log_attempt = 1
            return self._last_log_job, self._last_log_attempt

    def _record_attempt(
        self,
        *,
        endpoint: str,
        call_seq: int | None,
        log_job: int | None,
        attempt: int,
        method: Any,
        nonce: str | None,
        idempotency_key: str,
        request_id: str,
        local: str | None,
        response: HttpResponse | None,
        error: BaseException | None,
        t0: float,
        t1: float,
    ) -> None:
        """Append one ``AttemptRecord``. Never raises."""
        try:
            status, code, error_class, timed_out, timeout_phase = _outcome_fields(response, error)
            verdict, processing_time_us = _verdict_fields(endpoint, response)
            calls_module.calls.append(
                {
                    "kind": "attempt",
                    "endpoint": endpoint,
                    "call_seq": call_seq,
                    "log_job": log_job,
                    "attempt": attempt,
                    "method": calls_module.j(method),
                    "nonce": nonce,
                    "idempotency_key": idempotency_key,
                    "request_id": request_id,
                    "status": status,
                    "code": code,
                    "error_class": error_class,
                    "timed_out": timed_out,
                    "timeout_phase": timeout_phase,
                    "verdict": verdict,
                    "processing_time_us": processing_time_us,
                    "local": local,
                    "t0": t0,
                    "t1": t1,
                }
            )
        except Exception:  # noqa: BLE001, S110 - recording must never break the caller
            pass

    def close(self) -> None:
        with self._close_lock:
            if self._closed:
                return
            self._closed = True
        try:
            self._inner.close()
        except Exception:  # noqa: BLE001, S110 - the kit's own code must never raise
            pass

    def __repr__(self) -> str:
        return "KitTransport(api_key=<redacted>)"


def transport_class() -> type | None:
    """The kit transport class, or None when the SDK's HTTP transport didn't import.

    A read-only probe for the boot self-test: it never constructs anything.
    """
    return KitTransport if IMPORT_ERROR is None else None


__all__ = [
    "CANARY_REJECT_CODE",
    "IMPORT_ERROR",
    "METHODS",
    "METHOD_ONLY_CODE",
    "METHOD_ONLY_STATUS",
    "PINNED_SDK_VERSION",
    "KitTransport",
    "transport_class",
    "user_agent",
]
