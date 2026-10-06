"""Puts text on the system clipboard, for ``/copy``.

It uses the clipboard tool your system already has: ``pbcopy`` on macOS;
on Linux ``wl-copy`` under Wayland, then ``xclip``. The first tool that
takes the text wins. With none of them (or none that works), it says so and
the caller writes the text to a file instead. Nothing here ever raises.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys

from playground.traffic.local_constants import CLIPBOARD_TIMEOUT_S


def _candidates() -> list[list[str]]:
    """The clipboard commands to try on this system, in order."""
    if sys.platform == "darwin":
        return [["pbcopy"]]
    if sys.platform.startswith("linux"):
        commands = []
        if os.environ.get("WAYLAND_DISPLAY"):
            commands.append(["wl-copy"])
        commands.append(["xclip", "-selection", "clipboard"])
        return commands
    return []


def copy_to_clipboard(text: str) -> bool:
    """Put ``text`` on the clipboard; True only when a tool took it (exit 0)."""
    try:
        data = text.encode("utf-8")
        for command in _candidates():
            path = shutil.which(command[0])
            if path is None:
                continue
            try:
                # Some tools stay behind to serve the clipboard: their output
                # goes nowhere, so nothing here waits on it.
                done = subprocess.run(
                    [path, *command[1:]],
                    input=data,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=CLIPBOARD_TIMEOUT_S,
                    check=False,
                )
            except (OSError, subprocess.SubprocessError, ValueError):
                continue
            if done.returncode == 0:
                return True
    except Exception:  # noqa: BLE001 - /copy falls back to a file, never a traceback
        return False
    return False


__all__ = ["copy_to_clipboard"]
