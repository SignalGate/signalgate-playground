"""Tests for the API key loader (`app.kit.settings`) and how `get_client()`
reacts to what it finds: precedence, the stdlib parser, and the one-read-
per-boot snapshot."""

from __future__ import annotations

import pytest
import signalgate

from app import signalgate_client
from app.kit import facade, settings
from tests.app.fixtures.facade.fake_transport import FakeTransport
from tests.app.fixtures.facade.records import assert_every_record_matches_its_shape

KEY_VARIABLE = "SIGNALGATE_API_KEY"


@pytest.fixture(autouse=True)
def _patch_kit_transport(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(facade, "KitTransport", FakeTransport)


@pytest.fixture(autouse=True)
def _every_record_matches_its_shape():
    """After every test here, every ``construct`` (and ``call``) record a
    ``get_client()`` produced must match its contract shape."""
    yield
    assert_every_record_matches_its_shape()


def _write_env_file(content: str | bytes) -> None:
    if isinstance(content, str):
        settings.ENV_PATH.write_text(content, encoding="utf-8")
    else:
        settings.ENV_PATH.write_bytes(content)


def _refresh() -> None:
    signalgate_client._reset()


# --- precedence: env, file, both, placeholder-plus-env --------------------------


def test_env_only_is_usable(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv(KEY_VARIABLE, "env-key-0123456789")
    _refresh()
    assert settings.load_api_key() == "env-key-0123456789"
    assert signalgate_client.key_state() == "set"


def test_file_only_is_usable():
    _write_env_file("SIGNALGATE_API_KEY=file-key-0123456789\n")
    _refresh()
    assert settings.load_api_key() == "file-key-0123456789"
    assert signalgate_client.key_state() == "set"


def test_both_present_the_file_wins(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv(KEY_VARIABLE, "env-key-0123456789")
    _write_env_file("SIGNALGATE_API_KEY=file-key-0123456789\n")
    _refresh()
    assert settings.load_api_key() == "file-key-0123456789"
    assert signalgate_client.key_state() == "set"


def test_a_placeholder_file_with_a_real_env_value_uses_the_env(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv(KEY_VARIABLE, "env-key-0123456789")
    _write_env_file(f"SIGNALGATE_API_KEY={settings.PLACEHOLDER_KEY}\n")
    _refresh()
    assert settings.load_api_key() == "env-key-0123456789"
    assert signalgate_client.key_state() == "set"


# --- placeholder or blank: no key, but a later source can still supply one -------


def test_placeholder_only_gives_config_error_and_builds_nothing():
    _write_env_file(f"SIGNALGATE_API_KEY={settings.PLACEHOLDER_KEY}\n")
    _refresh()
    assert settings.load_api_key() is None
    assert signalgate_client.key_state() == "placeholder"
    with pytest.raises(signalgate.ConfigError):
        signalgate_client.get_client()
    assert signalgate_client.client_state() == (0, False)


def test_blank_only_gives_config_error_and_builds_nothing():
    _write_env_file("SIGNALGATE_API_KEY=\n")
    _refresh()
    assert settings.load_api_key() is None
    assert signalgate_client.key_state() == "absent"
    with pytest.raises(signalgate.ConfigError):
        signalgate_client.get_client()
    assert signalgate_client.client_state() == (0, False)


def test_no_source_at_all_gives_config_error():
    assert settings.load_api_key() is None
    assert signalgate_client.key_state() == "absent"
    with pytest.raises(signalgate.ConfigError):
        signalgate_client.get_client()


def test_config_error_message_never_contains_a_value():
    _write_env_file("SIGNALGATE_API_KEY=has space and is malformed\n")
    _refresh()
    with pytest.raises(signalgate.ConfigError) as excinfo:
        signalgate_client.get_client()
    assert "has space and is malformed" not in str(excinfo.value)


# --- malformed values: invalid, but a later source can still supply the key -----

_MALFORMED_CONTENTS = [
    pytest.param("SIGNALGATE_API_KEY=has space\n", id="internal_space"),
    pytest.param("SIGNALGATE_API_KEY=has\ttab\n", id="internal_tab"),
    pytest.param(b'SIGNALGATE_API_KEY="abc\rdef"\n', id="stray_cr_inside_quotes"),
    pytest.param("SIGNALGATE_API_KEY=abc\x07def\n", id="control_character"),
    pytest.param("SIGNALGATE_API_KEY=héllo\n", id="non_ascii"),
    pytest.param("SIGNALGATE_API_KEY=abc\x0bdef\n", id="vertical_tab"),
    pytest.param("SIGNALGATE_API_KEY=abc\x0cdef\n", id="form_feed"),
    pytest.param("SIGNALGATE_API_KEY=abc\x85def\n", id="unicode_next_line"),
    pytest.param("SIGNALGATE_API_KEY=abc def\n", id="unicode_line_separator"),
    pytest.param("SIGNALGATE_API_KEY=abc\xa0\n", id="trailing_nbsp"),
    pytest.param(b"SIGNALGATE_API_KEY=abc\xe9def\n", id="non_utf8_byte"),
]


@pytest.mark.parametrize("content", _MALFORMED_CONTENTS)
def test_a_malformed_value_gives_invalid_and_builds_nothing(content):
    _write_env_file(content)
    _refresh()
    assert settings.load_api_key() is None
    assert signalgate_client.key_state() == "invalid"
    with pytest.raises(signalgate.ConfigError):
        signalgate_client.get_client()
    assert signalgate_client.client_state() == (0, False)


@pytest.mark.parametrize("content", _MALFORMED_CONTENTS)
def test_a_malformed_file_with_a_usable_env_value_uses_the_env(
    monkeypatch: pytest.MonkeyPatch, content
):
    monkeypatch.setenv(KEY_VARIABLE, "env-key-0123456789")
    _write_env_file(content)
    _refresh()
    assert settings.load_api_key() == "env-key-0123456789"
    assert signalgate_client.key_state() == "set"


# --- the stdlib parser: quotes, export, comments, newlines, BOM -----------------


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        pytest.param("SIGNALGATE_API_KEY=bare-value-0123\n", "bare-value-0123", id="bare"),
        pytest.param(
            "SIGNALGATE_API_KEY='single-quoted-01'\n", "single-quoted-01", id="single_quotes"
        ),
        pytest.param(
            'SIGNALGATE_API_KEY="double-quoted-01"\n', "double-quoted-01", id="double_quotes"
        ),
        pytest.param(
            "export SIGNALGATE_API_KEY=exported-value-01\n", "exported-value-01", id="export"
        ),
        pytest.param(
            "SIGNALGATE_API_KEY=commented-value  # a trailing comment\n",
            "commented-value",
            id="trailing_comment",
        ),
        pytest.param(
            "SIGNALGATE_API_KEY = spaced-value-01\n", "spaced-value-01", id="spaces_around_equals"
        ),
        pytest.param(b"SIGNALGATE_API_KEY=crlf-value-01\r\n", "crlf-value-01", id="crlf"),
        pytest.param(b"SIGNALGATE_API_KEY=cr-only-value-01\r", "cr-only-value-01", id="cr_only"),
        pytest.param(
            b"\xef\xbb\xbfSIGNALGATE_API_KEY=bom-value-01\n", "bom-value-01", id="utf8_bom"
        ),
        pytest.param(
            "# a comment line\n\nSIGNALGATE_API_KEY=after-blank-and-comment-01\n",
            "after-blank-and-comment-01",
            id="blank_and_comment_lines_skipped",
        ),
        pytest.param(
            "SIGNALGATE_API_KEY=tab-commented-01\t# a trailing comment\n",
            "tab-commented-01",
            id="tab_before_comment",
        ),
        pytest.param(
            "SIGNALGATE_API_KEY= 'spaced-quoted-01'\n", "spaced-quoted-01", id="space_then_quotes"
        ),
        pytest.param(
            b"# caf\xe9 notes\nSIGNALGATE_API_KEY=dummy-probe-0123\n",
            "dummy-probe-0123",
            id="non_utf8_byte_in_a_comment_line",
        ),
        pytest.param(
            b"OTHER_VARIABLE=caf\xe9\nSIGNALGATE_API_KEY=dummy-probe-0123\n",
            "dummy-probe-0123",
            id="non_utf8_byte_in_another_variable",
        ),
    ],
)
def test_the_parser_reads_the_bare_value(content, expected):
    _write_env_file(content)
    _refresh()
    assert settings.load_api_key() == expected
    assert signalgate_client.key_state() == "set"


_COMMENT_ONLY_VALUES = [
    pytest.param("SIGNALGATE_API_KEY= # paste here\n", id="space_then_comment"),
    pytest.param("SIGNALGATE_API_KEY=  # paste here\n", id="two_spaces_then_comment"),
    pytest.param("SIGNALGATE_API_KEY=\t# c\n", id="tab_then_comment"),
    pytest.param("SIGNALGATE_API_KEY = # paste here\n", id="spaced_equals_then_comment"),
]


@pytest.mark.parametrize("content", _COMMENT_ONLY_VALUES)
def test_a_value_that_is_only_a_comment_is_blank(content):
    _write_env_file(content)
    _refresh()
    assert settings.load_api_key() is None
    assert signalgate_client.key_state() == "absent"
    with pytest.raises(signalgate.ConfigError):
        signalgate_client.get_client()
    assert signalgate_client.client_state() == (0, False)


@pytest.mark.parametrize("content", _COMMENT_ONLY_VALUES)
def test_a_comment_only_file_value_with_a_usable_env_value_uses_the_env(
    monkeypatch: pytest.MonkeyPatch, content
):
    monkeypatch.setenv(KEY_VARIABLE, "env-key-0123456789")
    _write_env_file(content)
    _refresh()
    assert settings.load_api_key() == "env-key-0123456789"
    assert signalgate_client.key_state() == "set"


def test_a_non_utf8_byte_with_no_key_line_is_absent():
    _write_env_file(b"# caf\xe9 notes\n")
    _refresh()
    assert settings.load_api_key() is None
    assert signalgate_client.key_state() == "absent"


def test_a_later_assignment_in_the_file_wins():
    _write_env_file("SIGNALGATE_API_KEY=first-value-01\nSIGNALGATE_API_KEY=second-value-01\n")
    _refresh()
    assert settings.load_api_key() == "second-value-01"


def test_an_unrelated_variable_is_ignored():
    _write_env_file("OTHER_VARIABLE=whatever\n")
    _refresh()
    assert settings.load_api_key() is None
    assert signalgate_client.key_state() == "absent"


# --- read once per boot ----------------------------------------------------------


def test_the_key_is_read_once_per_boot_until_reset():
    _write_env_file("SIGNALGATE_API_KEY=first-key-0123456789\n")
    _refresh()
    assert settings.load_api_key() == "first-key-0123456789"

    # Edit the file without calling _reset(): the boot snapshot doesn't move.
    _write_env_file("SIGNALGATE_API_KEY=second-key-0123456789\n")
    assert settings.load_api_key() == "first-key-0123456789"
    assert signalgate_client.key_state() == "set"

    client = signalgate_client.get_client()
    assert client._kit.api_key == "first-key-0123456789"

    _refresh()
    assert settings.load_api_key() == "second-key-0123456789"


def test_a_placeholder_swap_only_takes_effect_after_reset():
    monkey_value = f"SIGNALGATE_API_KEY={settings.PLACEHOLDER_KEY}\n"
    _write_env_file(monkey_value)
    _refresh()
    assert signalgate_client.key_state() == "placeholder"

    _write_env_file("SIGNALGATE_API_KEY=now-real-0123456789\n")
    assert signalgate_client.key_state() == "placeholder"  # unchanged until _reset()

    _refresh()
    assert signalgate_client.key_state() == "set"
    assert settings.load_api_key() == "now-real-0123456789"
