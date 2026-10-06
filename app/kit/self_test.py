"""The sample app's boot self-test: are the kit's hooks in place?

`result()` runs each check once, lazily, and caches the outcome. No check
builds a SignalGate client, reads the API key or touches the network.

Check codes: `sdk_version`, `transport_import`, `transport_signature`,
`transport_protocol`, `facade`, `client_hook`, `probe_hooks`, `exit_hook`.
Each check ends as pass, fail or skip; a skip is not a failure.
"""

from __future__ import annotations

import importlib.metadata
import inspect
import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

import signalgate

from app.kit import context, facade, kit_transport, lifecycle
from app.kit.actions import perform_action

CheckOutcome = Literal["pass", "fail", "skip"]

CODES = (
    "sdk_version",
    "transport_import",
    "transport_signature",
    "transport_protocol",
    "facade",
    "client_hook",
    "probe_hooks",
    "exit_hook",
)


@dataclass(frozen=True)
class SelfTest:
    """The self-test outcome.

    ``ok`` is true when no check failed; ``failed`` lists the failed check
    codes in check order; ``checks`` maps every check code to its outcome.
    """

    ok: bool
    failed: tuple[str, ...]
    checks: dict[str, CheckOutcome]


def _parameters(function: object) -> list[tuple[str, inspect._ParameterKind]]:
    return [(p.name, p.kind) for p in inspect.signature(function).parameters.values()]


def _check_sdk_version() -> CheckOutcome:
    try:
        installed = importlib.metadata.version("signalgate")
    except importlib.metadata.PackageNotFoundError:
        return "fail"
    if installed == signalgate.SDK_VERSION == kit_transport.PINNED_SDK_VERSION:
        return "pass"
    return "fail"


def _check_transport_import() -> CheckOutcome:
    return "pass" if kit_transport.IMPORT_ERROR is None else "fail"


def _check_transport_signature() -> CheckOutcome:
    transport = kit_transport.transport_class()
    if transport is None:
        return "fail"
    try:
        for name in ("post", "close"):
            ours = _parameters(getattr(transport, name))
            theirs = _parameters(getattr(signalgate.Transport, name))
            if ours != theirs:
                return "fail"
    except Exception:  # noqa: BLE001 - a self-test check must never raise
        return "fail"
    return "pass"


def _check_transport_protocol() -> CheckOutcome:
    transport = kit_transport.transport_class()
    if transport is None:
        return "fail"
    try:
        return "pass" if issubclass(transport, signalgate.Transport) else "fail"
    except TypeError:
        return "fail"


def _check_facade() -> CheckOutcome:
    return "pass" if facade.missing_members() == () else "fail"


def _check_client_hook() -> CheckOutcome:
    return "pass" if facade.client_hook_installed() else "fail"


def _check_probe_hooks() -> CheckOutcome:
    """The existing probe hooks are still wired: `ContextMiddleware` is on the app,
    and `perform_action` still consults the action-fail flag and the reserved
    user pattern. Structural only: this never actually runs an action."""
    try:
        from app.main import app as _app
    except Exception:  # noqa: BLE001 - a self-test check must never raise
        return "fail"

    has_middleware = any(
        getattr(mw, "cls", None) is context.ContextMiddleware for mw in _app.user_middleware
    )
    if not has_middleware:
        return "fail"

    names = perform_action.__code__.co_names
    if "action_should_fail" not in names or "is_valid_user" not in names:
        return "fail"
    return "pass"


def _check_exit_hook() -> CheckOutcome:
    return lifecycle.exit_hook_ok()


_CHECKS: dict[str, Callable[[], CheckOutcome]] = {
    "sdk_version": _check_sdk_version,
    "transport_import": _check_transport_import,
    "transport_signature": _check_transport_signature,
    "transport_protocol": _check_transport_protocol,
    "facade": _check_facade,
    "client_hook": _check_client_hook,
    "probe_hooks": _check_probe_hooks,
    "exit_hook": _check_exit_hook,
}

_lock = threading.Lock()
_cache: SelfTest | None = None


def _run_check(code: str) -> CheckOutcome:
    """Run one check, never letting it raise: a check that raises fails,
    the same as a check that returns "fail", so one broken probe can never
    stop the app from booting or answering /health."""
    try:
        return _CHECKS[code]()
    except Exception:  # noqa: BLE001 - a self-test check must never raise
        return "fail"


def _compute() -> SelfTest:
    checks: dict[str, CheckOutcome] = {}
    failed: list[str] = []
    for code in CODES:
        outcome = _run_check(code)
        checks[code] = outcome
        if outcome == "fail":
            failed.append(code)
    return SelfTest(ok=not failed, failed=tuple(failed), checks=checks)


def result() -> SelfTest:
    """The cached self-test outcome for this boot."""
    global _cache
    with _lock:
        if _cache is None:
            _cache = _compute()
        return _cache


def _reset() -> None:
    """Forget the cached result (tests only)."""
    global _cache
    with _lock:
        _cache = None


__all__ = ["CheckOutcome", "SelfTest", "result"]
