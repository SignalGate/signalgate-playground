"""Each self-test check's pass and fail path.

``self_test.result()`` is built only on the frozen, read-only probes and
constants that ``tests/app/test_self_test_probes.py`` pins. Every case here
boots the real app through ``with TestClient(app)`` -- so the lifespan's own
call to ``self_test.result()`` runs first, exactly as it does under uvicorn
-- and reads the outcome back from ``GET /__playground/health``, rather than
calling ``self_test.result()`` directly: a probe that raises must still let
the app boot and answer, which a direct call could never catch.
"""

from __future__ import annotations

import pytest
import signalgate
from fastapi.testclient import TestClient

from app.kit import context, facade, kit_transport, lifecycle, self_test
from app.kit.actions import perform_action
from app.main import app


def _health(control_token: str) -> dict:
    """Boot the real app, read `/health` once, and shut it back down."""
    with TestClient(app) as test_client:
        response = test_client.get(
            "/__playground/health", headers={"Authorization": f"Bearer {control_token}"}
        )
    assert response.status_code == 200
    body = response.json()
    assert body["clients_created"] == 0
    return body


def test_every_check_passes_when_every_probe_does(control_token, monkeypatch):
    monkeypatch.setattr(facade, "client_hook_installed", lambda: True)
    body = _health(control_token)
    outcome = body["self_test"]
    assert outcome["ok"] is True
    assert outcome["failed"] == []
    assert set(outcome["checks"]) == set(self_test.CODES)
    assert all(value in ("pass", "skip") for value in outcome["checks"].values())


# --- sdk_version ---------------------------------------------------------------


def test_sdk_version_passes_when_all_three_agree(control_token):
    assert _health(control_token)["self_test"]["checks"]["sdk_version"] == "pass"


def test_sdk_version_fails_on_a_patched_sdk_version(control_token, monkeypatch):
    monkeypatch.setattr(signalgate, "SDK_VERSION", "0.0.0-test")
    body = _health(control_token)
    assert body["self_test"]["checks"]["sdk_version"] == "fail"
    assert "sdk_version" in body["self_test"]["failed"]


def test_sdk_version_fails_on_a_patched_installed_metadata_version(control_token, monkeypatch):
    import importlib.metadata

    monkeypatch.setattr(importlib.metadata, "version", lambda _name: "0.0.0-test")
    assert _health(control_token)["self_test"]["checks"]["sdk_version"] == "fail"


# --- transport_import / transport_protocol / transport_signature -----------------


def test_transport_checks_fail_together_when_the_import_failed(control_token, monkeypatch):
    monkeypatch.setattr(kit_transport, "IMPORT_ERROR", "ImportError")
    checks = _health(control_token)["self_test"]["checks"]
    assert checks["transport_import"] == "fail"
    assert checks["transport_protocol"] == "fail"
    assert checks["transport_signature"] == "fail"


def test_transport_signature_fails_alone_when_post_loses_a_parameter(control_token, monkeypatch):
    class BadTransport:
        def post(self, url, body, *, timeout_ms, request_id):  # missing idempotency_key
            raise NotImplementedError

        def close(self) -> None:
            raise NotImplementedError

    monkeypatch.setattr(kit_transport, "transport_class", lambda: BadTransport)
    checks = _health(control_token)["self_test"]["checks"]
    # Only transport_signature fails: transport_protocol's issubclass check
    # is structural (Transport is a runtime-checkable Protocol) and doesn't
    # look at parameter names, so a class with the right methods still
    # passes it even with the wrong signature.
    assert checks["transport_signature"] == "fail"
    assert checks["transport_import"] == "pass"
    assert checks["transport_protocol"] == "pass"


# --- facade -----------------------------------------------------------------------


def test_facade_fails_when_a_member_is_missing(control_token, monkeypatch):
    monkeypatch.setattr(facade, "missing_members", lambda: ("check",))
    body = _health(control_token)
    assert body["self_test"]["checks"]["facade"] == "fail"
    assert "facade" in body["self_test"]["failed"]


def test_facade_passes_when_nothing_is_missing(control_token, monkeypatch):
    monkeypatch.setattr(facade, "missing_members", lambda: ())
    assert _health(control_token)["self_test"]["checks"]["facade"] == "pass"


def test_a_raising_probe_still_boots_the_app_and_fails_only_its_own_check(
    control_token, monkeypatch
):
    """A probe that raises must fail its own check, never crash the
    lifespan: the app still boots and `/health` still answers 200."""

    def _raises() -> tuple[str, ...]:
        raise RuntimeError("a probe blowing up must not take the app down with it")

    monkeypatch.setattr(facade, "missing_members", _raises)
    body = _health(control_token)
    assert body["self_test"]["checks"]["facade"] == "fail"
    assert "facade" in body["self_test"]["failed"]


# --- client_hook --------------------------------------------------------------------


@pytest.fixture(params=["marker_present", "marker_absent"])
def real_hook_marker(request, monkeypatch) -> bool:
    """Put the real ``Client.__init__`` in either state the process can be in.

    Whether the counting hook is already on ``signalgate.Client.__init__``
    depends on what ``app.signalgate_client`` did at import, not on these
    tests. Each ``client_hook`` case runs once with the marker present and
    once without it, so a case that only passed because of whichever state
    the import left behind fails in the other one. Returns what the real,
    unpatched probe reports for this state.
    """
    original = signalgate.Client.__init__

    def init(self, *args, **kwargs):
        return original(self, *args, **kwargs)

    present = request.param == "marker_present"
    if present:
        setattr(init, facade.HOOK_MARKER, True)
    monkeypatch.setattr(signalgate.Client, "__init__", init)
    assert facade.client_hook_installed() is present
    return present


def test_client_hook_fails_when_the_probe_reports_it_missing(
    control_token, monkeypatch, real_hook_marker
):
    monkeypatch.setattr(facade, "client_hook_installed", lambda: False)
    body = _health(control_token)
    assert body["self_test"]["checks"]["client_hook"] == "fail"
    assert "client_hook" in body["self_test"]["failed"]
    assert body["self_test"]["ok"] is False


def test_client_hook_passes_once_the_probe_reports_it_installed(
    control_token, monkeypatch, real_hook_marker
):
    monkeypatch.setattr(facade, "client_hook_installed", lambda: True)
    body = _health(control_token)
    assert body["self_test"]["checks"]["client_hook"] == "pass"
    assert "client_hook" not in body["self_test"]["failed"]


def test_client_hook_check_reads_the_frozen_probe(real_hook_marker):
    """Unpatched, the check reports exactly what the probe does, in either state."""
    assert self_test._check_client_hook() == ("pass" if real_hook_marker else "fail")


# --- probe_hooks ------------------------------------------------------------------


def test_probe_hooks_passes_normally(control_token):
    assert _health(control_token)["self_test"]["checks"]["probe_hooks"] == "pass"


def test_probe_hooks_fails_without_the_context_middleware(control_token, monkeypatch):
    kept = [mw for mw in app.user_middleware if mw.cls is not context.ContextMiddleware]
    monkeypatch.setattr(app, "user_middleware", kept)
    body = _health(control_token)
    assert body["self_test"]["checks"]["probe_hooks"] == "fail"


def test_probe_hooks_fails_if_perform_action_stops_consulting_the_hooks(control_token, monkeypatch):
    def fake_perform_action(user: str) -> None:
        return None

    monkeypatch.setattr(self_test, "perform_action", fake_perform_action)
    assert _health(control_token)["self_test"]["checks"]["probe_hooks"] == "fail"


def test_probe_hooks_check_never_leaves_a_ledger_entry_behind():
    from app.kit.sequence import ledger

    before = len(ledger)
    perform_action_result = self_test._check_probe_hooks()
    assert perform_action_result == "pass"
    assert len(ledger) == before


# --- exit_hook ------------------------------------------------------------------


def test_exit_hook_is_skip_without_a_real_uvicorn_server(control_token):
    assert _health(control_token)["self_test"]["checks"]["exit_hook"] == "skip"


def test_exit_hook_is_pass_when_a_uvicorn_like_server_is_found(control_token, monkeypatch):
    import signal as signal_module

    class FakeServer:
        should_exit = False

    server = FakeServer()

    def fake_handler(*_args, **_kwargs):
        raise AssertionError("never called; only .__self__ is read")

    fake_handler.__self__ = server
    monkeypatch.setattr(signal_module, "getsignal", lambda _sig: fake_handler)
    assert _health(control_token)["self_test"]["checks"]["exit_hook"] == "pass"


# --- result() caching ------------------------------------------------------------


def test_result_is_cached_until_reset(monkeypatch, real_hook_marker):
    self_test._reset()
    monkeypatch.setattr(facade, "client_hook_installed", lambda: False)
    first = self_test.result()
    assert first.checks["client_hook"] == "fail"

    monkeypatch.setattr(facade, "client_hook_installed", lambda: True)
    second = self_test.result()
    assert second is first  # the probe changed, the cached outcome did not
    assert second.checks["client_hook"] == "fail"

    self_test._reset()
    third = self_test.result()
    assert third is not first
    assert third.checks["client_hook"] == "pass"


def test_perform_action_still_used_by_the_real_probe_hooks_check():
    # Sanity: the probe really does reference `perform_action` from this
    # module's own namespace (what the fail-path test above monkeypatches).
    assert self_test.perform_action is perform_action
    assert self_test._CHECKS["exit_hook"] is self_test._check_exit_hook
    assert self_test._check_exit_hook() == lifecycle.exit_hook_ok()
