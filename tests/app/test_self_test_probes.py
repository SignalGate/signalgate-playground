"""The self-test's read-only probes, and the names other code relies on.

The boot self-test builds its ``transport_*`` and ``facade`` checks on the
probes below. They already hold for the module skeletons, so a later
change that breaks one fails here, on the branch that made it.
"""

from __future__ import annotations

import dataclasses
import inspect
import os
import typing
from pathlib import Path
from typing import Any

import signalgate

import app
from app import signalgate_client
from app.kit import facade, kit_transport, self_test, settings
from tests.contract import samples

# Read while this module is collected, before tests/app/conftest.py points
# ENV_PATH at a file of each test's own.
_DEFAULT_ENV_PATH = settings.ENV_PATH


def _parameters(function) -> list[tuple[str, Any]]:
    return [(p.name, p.kind) for p in inspect.signature(function).parameters.values()]


def _names(function) -> list[str]:
    return list(inspect.signature(function).parameters)


# --- the probes the self-test checks are built on --------------------------------


def test_transport_import_probe_succeeds():
    assert kit_transport.IMPORT_ERROR is None


def test_transport_protocol_probe_succeeds():
    transport = kit_transport.transport_class()
    assert transport is not None
    assert issubclass(transport, signalgate.Transport)


def test_transport_signature_probe_succeeds():
    transport = kit_transport.transport_class()
    for name in ("post", "close"):
        assert _parameters(getattr(transport, name)) == _parameters(
            getattr(signalgate.Transport, name)
        ), name


def test_facade_probe_succeeds():
    assert facade.missing_members() == ()


def test_transport_class_is_none_once_the_import_failed(monkeypatch):
    monkeypatch.setattr(kit_transport, "IMPORT_ERROR", "ImportError")
    assert kit_transport.transport_class() is None


def test_missing_members_notices_a_new_public_client_member(monkeypatch):
    monkeypatch.setattr(signalgate.Client, "flush", lambda self: None, raising=False)
    assert facade.missing_members() == ("flush",)


def test_missing_members_notices_a_member_the_facade_lost(monkeypatch):
    monkeypatch.delattr(facade.RecordingClient, "check")
    assert facade.missing_members() == ("check",)


def test_client_hook_probe_reads_the_marker_on_client_init(monkeypatch):
    original = signalgate.Client.__init__

    def marked(self, *args, **kwargs):
        original(self, *args, **kwargs)

    setattr(marked, facade.HOOK_MARKER, True)
    monkeypatch.setattr(signalgate.Client, "__init__", marked)
    assert facade.client_hook_installed() is True

    setattr(marked, facade.HOOK_MARKER, "yes")
    assert facade.client_hook_installed() is False

    delattr(marked, facade.HOOK_MARKER)
    assert facade.client_hook_installed() is False


def test_is_recording_client_tells_the_facade_apart():
    assert facade.is_recording_client(object.__new__(facade.RecordingClient)) is True
    assert facade.is_recording_client(object()) is False
    assert facade.is_recording_client(None) is False


# --- frozen names and values -------------------------------------------------------


def test_kit_transport_constants_are_frozen():
    assert kit_transport.METHODS == ("playground.action", "playground.success")
    assert kit_transport.METHOD_ONLY_CODE == "PLAYGROUND_METHOD_ONLY"
    assert kit_transport.METHOD_ONLY_STATUS == 400
    assert kit_transport.CANARY_REJECT_CODE == "REQUEST_REJECTED"
    assert kit_transport.PINNED_SDK_VERSION == "0.3.2"


def test_the_user_agent_is_built_from_the_sdks_public_constants():
    agent = kit_transport.user_agent()
    assert agent.startswith(f"{signalgate.SDK_NAME}/{signalgate.SDK_VERSION} (python/")


def test_kit_transport_signature_is_frozen():
    assert _parameters(kit_transport.KitTransport.__init__) == [
        ("self", inspect.Parameter.POSITIONAL_OR_KEYWORD),
        ("api_key", inspect.Parameter.POSITIONAL_OR_KEYWORD),
        ("logger", inspect.Parameter.KEYWORD_ONLY),
    ]


def test_facade_names_are_frozen():
    assert facade.HOOK_MARKER == "_playground_client_hook"
    assert facade.FORWARDED == ("metrics", "log", "check", "close", "__enter__", "__exit__")
    for name in facade.FORWARDED:
        assert name in facade.RecordingClient.__dict__, name
    assert isinstance(facade.RecordingClient.__dict__["metrics"], property)
    assert _names(facade.build) == ["key"]
    assert _names(facade.install_client_hook) == []


def test_signalgate_client_names_are_frozen():
    for name in ("get_client", "sg", "close_client", "client_state", "metrics_snapshot"):
        assert _names(getattr(signalgate_client, name)) == [], name
    assert _names(signalgate_client.key_state) == []
    assert _names(signalgate_client._reset) == []
    # close_client() answers nothing: callers read client_state() afterwards.
    assert inspect.signature(signalgate_client.close_client).return_annotation in (None, "None")
    signature = inspect.signature(signalgate_client.sg_event)
    assert list(signature.parameters) == ["request", "user", "envelope", "method", "custom"]
    assert signature.parameters["custom"].default is None


def test_settings_names_are_frozen():
    assert settings.PLACEHOLDER_KEY == "PASTE_YOUR_API_KEY_HERE"
    assert _names(settings.load_api_key) == []
    assert _names(settings.key_state) == []
    assert _DEFAULT_ENV_PATH == Path(app.__file__).resolve().parent / ".env"


def test_each_test_starts_with_empty_key_sources():
    # The isolation tests/app/conftest.py gives every test in this directory.
    assert "SIGNALGATE_API_KEY" not in os.environ
    assert settings.ENV_PATH != _DEFAULT_ENV_PATH
    assert settings.ENV_PATH.parent.is_dir()
    assert not settings.ENV_PATH.exists()
    assert settings.load_api_key() is None
    assert signalgate_client.key_state() == "absent"


def test_self_test_shape_is_frozen():
    # Only the shape: calling result() here would fill its per-boot cache.
    assert [f.name for f in dataclasses.fields(self_test.SelfTest)] == ["ok", "failed", "checks"]
    assert _names(self_test.result) == []
    assert typing.get_args(self_test.CheckOutcome) == samples.CHECK_OUTCOMES
