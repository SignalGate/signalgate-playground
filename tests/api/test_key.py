"""Where the console finds the API key, and the shape it must have.

Every key here is built at run time from harmless parts, so no file in the
repo holds a key-shaped string.
"""

from __future__ import annotations

import pytest

from playground.api import key

GOOD = key.KEY_PREFIX + "Ab3-" * 8
ENDS_IN_DASH = key.KEY_PREFIX + "a" * 31 + "-"


def _env_file(root, text: str) -> None:
    (root / "app").mkdir(exist_ok=True)
    (root / "app" / ".env").write_text(text, encoding="utf-8")


@pytest.fixture(autouse=True)
def _no_key_in_the_environment(monkeypatch):
    monkeypatch.delenv(key.KEY_NAME, raising=False)


@pytest.mark.parametrize(
    "line",
    [
        f"SIGNALGATE_API_KEY={GOOD}",
        f"export SIGNALGATE_API_KEY = {GOOD}",
        f'SIGNALGATE_API_KEY="{GOOD}"',
        f"SIGNALGATE_API_KEY='{GOOD}'  # mine",
        f"SIGNALGATE_API_KEY={GOOD} # mine",
    ],
)
def test_the_key_is_read_from_app_env(tmp_path, line):
    _env_file(tmp_path, f"# a comment\nOTHER=1\n{line}\n")
    assert key.read_key(tmp_path) == GOOD


def test_the_last_assignment_wins(tmp_path):
    _env_file(tmp_path, f"SIGNALGATE_API_KEY=first\nSIGNALGATE_API_KEY={GOOD}\n")
    assert key.read_key(tmp_path) == GOOD


def test_the_environment_is_the_fallback(tmp_path, monkeypatch):
    _env_file(tmp_path, f"SIGNALGATE_API_KEY={key.PLACEHOLDER}\n")
    assert key.read_key(tmp_path) is None
    monkeypatch.setenv(key.KEY_NAME, GOOD)
    assert key.read_key(tmp_path) == GOOD


def test_no_file_and_no_variable_is_no_key(tmp_path):
    assert key.read_key(tmp_path) is None


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, key.MISSING),
        ("", key.MISSING),
        (key.PLACEHOLDER, key.MISSING),
        (GOOD, None),
        (ENDS_IN_DASH, None),
        ("x" * 43, key.PUBLIC_KEY),
        (GOOD[:-1], key.BAD_FORMAT),
        (GOOD + "x", key.BAD_FORMAT),
        ("pk_test_" + "a" * 32, key.BAD_FORMAT),
    ],
)
def test_the_shape_check(value, expected):
    assert key.problem(value) == expected
