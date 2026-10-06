"""``playground.clipboard``: which tool takes the text, and what happens without one.

No test here ever runs a real clipboard tool: ``PATH`` holds only the fake
tools each test writes (small scripts that record what they got), and
``sys.platform`` is set to the system being played.
"""

from __future__ import annotations

import os
import stat
import sys
import time
from pathlib import Path

import pytest

import playground.clipboard as clipboard_module
from playground.clipboard import copy_to_clipboard

TEXT = "from app.signalgate_client import sg, sg_event\n\n    if ev:\n        sg().log(ev) ✓"

_RECORDING = """\
import pathlib, sys
here = pathlib.Path(sys.argv[0]).resolve()
here.with_suffix(".got").write_bytes(sys.stdin.buffer.read())
here.with_suffix(".argv").write_text("\\n".join(sys.argv[1:]))
"""
_FAILING = """\
import sys
sys.stdin.buffer.read()
sys.exit(1)
"""
_HANGING = """\
import time
time.sleep({hang_s})
"""


class Tools:
    """A directory of fake clipboard tools, the only thing on ``PATH``."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory
        directory.mkdir(parents=True, exist_ok=True)

    def add(self, name: str, body: str = _RECORDING) -> None:
        path = self.directory / name
        path.write_text(f"#!{sys.executable}\n{body}")
        path.chmod(path.stat().st_mode | stat.S_IXUSR)

    def got(self, name: str) -> bytes | None:
        path = (self.directory / name).with_suffix(".got")
        return path.read_bytes() if path.exists() else None

    def argv(self, name: str) -> list[str]:
        text = (self.directory / name).with_suffix(".argv").read_text()
        return text.split("\n") if text else []


@pytest.fixture
def tools(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Tools:
    found = Tools(tmp_path / "bin")
    monkeypatch.setenv("PATH", str(found.directory))
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    return found


def _on(monkeypatch: pytest.MonkeyPatch, platform: str) -> None:
    monkeypatch.setattr(sys, "platform", platform)


def test_macos_uses_pbcopy(tools, monkeypatch):
    _on(monkeypatch, "darwin")
    tools.add("pbcopy")
    tools.add("xclip")
    assert copy_to_clipboard(TEXT) is True
    assert tools.got("pbcopy") == TEXT.encode("utf-8")
    assert tools.got("xclip") is None


def test_linux_uses_xclip_for_the_clipboard_selection(tools, monkeypatch):
    _on(monkeypatch, "linux")
    tools.add("wl-copy")
    tools.add("xclip")
    assert copy_to_clipboard(TEXT) is True
    assert tools.got("xclip") == TEXT.encode("utf-8")
    assert tools.argv("xclip") == ["-selection", "clipboard"]
    assert tools.got("wl-copy") is None  # not under Wayland


def test_linux_under_wayland_tries_wl_copy_first(tools, monkeypatch):
    _on(monkeypatch, "linux")
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    tools.add("wl-copy")
    tools.add("xclip")
    assert copy_to_clipboard(TEXT) is True
    assert tools.got("wl-copy") == TEXT.encode("utf-8")
    assert tools.got("xclip") is None


def test_a_tool_that_fails_gives_way_to_the_next(tools, monkeypatch):
    _on(monkeypatch, "linux")
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    tools.add("wl-copy", _FAILING)
    tools.add("xclip")
    assert copy_to_clipboard(TEXT) is True
    assert tools.got("xclip") == TEXT.encode("utf-8")


@pytest.mark.parametrize("platform", ["darwin", "linux"])
def test_every_tool_failing_is_no_copy(tools, monkeypatch, platform):
    _on(monkeypatch, platform)
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    for name in ("pbcopy", "wl-copy", "xclip"):
        tools.add(name, _FAILING)
    assert copy_to_clipboard(TEXT) is False


@pytest.mark.parametrize("platform", ["darwin", "linux"])
def test_no_tool_at_all_is_no_copy(tools, monkeypatch, platform):
    _on(monkeypatch, platform)
    assert copy_to_clipboard(TEXT) is False


@pytest.mark.parametrize("platform", ["win32", "freebsd14"])
def test_other_systems_have_no_tool_to_try(tools, monkeypatch, platform):
    _on(monkeypatch, platform)
    for name in ("pbcopy", "wl-copy", "xclip"):
        tools.add(name)
    assert copy_to_clipboard(TEXT) is False
    assert all(tools.got(name) is None for name in ("pbcopy", "wl-copy", "xclip"))


def test_a_tool_that_hangs_is_given_up_on_in_time(tools, monkeypatch):
    _on(monkeypatch, "darwin")
    timeout_s = 0.5
    monkeypatch.setattr(clipboard_module, "CLIPBOARD_TIMEOUT_S", timeout_s)
    tools.add("pbcopy", _HANGING.format(hang_s=timeout_s * 40))
    started = time.monotonic()
    assert copy_to_clipboard(TEXT) is False
    assert time.monotonic() - started < timeout_s * 20


def test_a_tool_that_cannot_run_is_no_copy(tools, monkeypatch):
    _on(monkeypatch, "darwin")
    path = tools.directory / "pbcopy"
    path.write_text("#!/nonexistent/interpreter\n")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    assert copy_to_clipboard(TEXT) is False


def test_the_real_path_is_never_searched(tools):
    # The fixture leaves only the fake directory on PATH.
    assert os.environ["PATH"] == str(tools.directory)
