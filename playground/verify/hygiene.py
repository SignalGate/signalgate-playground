"""Keeping your API key out of git, checked before the key is ever used.

Four checks, in order, each on the checkout at ``root``:

1. ``app/.env`` is not tracked, and git ignores it;
2. the exact key is in no tracked file;
3. the exact key is in no commit, on any branch;
4. nothing shaped like an API key is in a tracked file outside ``tests/``.

The key never goes on a command line: check 2 uses a pattern file only
this user can read (mode 0600), deleted straight after, and check 3 reads
git's output and looks for the key here. A folder that isn't a git checkout
gets NOT RUN, never a refusal. Any hit means the key has to be rotated.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from playground.api.key import ENV_RELATIVE
from playground.traffic.local_constants import GIT_TIMEOUT_S
from playground.verify.model import Verdict

# What ``check_hygiene`` may find.
ENV_TRACKED = "env_tracked"
ENV_NOT_IGNORED = "env_not_ignored"
KEY_IN_FILE = "key_in_file"
KEY_IN_HISTORY = "key_in_history"
KEY_SHAPE_IN_FILE = "key_shape_in_file"
NO_GIT = "no_git"

# A key's full shape, for ``git grep -E`` (no lookarounds there): the
# characters around it may not continue it.
_SHAPE_PATTERN = r"(^|[^A-Za-z0-9_-])pk_live_[A-Za-z0-9_-]{32}([^A-Za-z0-9_-]|$)"
_READ_CHUNK = 1 << 16


@dataclass(frozen=True)
class Hygiene:
    """PASS, FAIL with what was found (and in which tracked file), or NOT RUN."""

    verdict: Verdict
    problem: str | None = None
    file: str | None = None


def _git(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=GIT_TIMEOUT_S,
        check=False,
    )


def _first_line(output: str) -> str | None:
    lines = [line for line in output.splitlines() if line.strip()]
    return lines[0] if lines else None


def _in_history(root: Path, key: str) -> bool:
    """Whether any commit's patch holds ``key``; git's output is searched here."""
    process = subprocess.Popen(
        ["git", "log", "-p", "--all", "--no-color", "--no-ext-diff"],
        cwd=root,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    assert process.stdout is not None
    needle = key.encode("utf-8")
    tail = b""
    found = False
    try:
        while True:
            chunk = process.stdout.read(_READ_CHUNK)
            if not chunk:
                break
            if needle in tail + chunk:
                found = True
                break
            tail = chunk[-len(needle) :]
    finally:
        process.stdout.close()
        if process.poll() is None:
            process.kill()
        process.wait(timeout=GIT_TIMEOUT_S)
    return found


def check_hygiene(root: Path, key: str) -> Hygiene:
    """Run the four checks on the checkout at ``root`` for ``key``."""
    if not (root / ".git").exists() or shutil.which("git") is None:
        return Hygiene(Verdict.NOT_RUN, NO_GIT)
    env_file = ENV_RELATIVE.as_posix()
    try:
        if _git(root, "ls-files", "--error-unmatch", "--", env_file).returncode == 0:
            return Hygiene(Verdict.FAIL, ENV_TRACKED)
        if _git(root, "check-ignore", "-q", "--", env_file).returncode != 0:
            return Hygiene(Verdict.FAIL, ENV_NOT_IGNORED)

        fd, pattern_path = tempfile.mkstemp(prefix="playground-", suffix=".pattern")
        try:
            os.chmod(pattern_path, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(key + "\n")
            found = _git(root, "grep", "-l", "-F", "-f", pattern_path, "--", ".")
        finally:
            os.remove(pattern_path)
        hit = _first_line(found.stdout) if found.returncode == 0 else None
        if hit is not None:
            return Hygiene(Verdict.FAIL, KEY_IN_FILE, hit)

        if _in_history(root, key):
            return Hygiene(Verdict.FAIL, KEY_IN_HISTORY)

        shaped = _git(root, "grep", "-l", "-E", "-e", _SHAPE_PATTERN, "--", ".", ":(exclude)tests")
        hit = _first_line(shaped.stdout) if shaped.returncode == 0 else None
        if hit is not None:
            return Hygiene(Verdict.FAIL, KEY_SHAPE_IN_FILE, hit)
    except (OSError, subprocess.SubprocessError):
        return Hygiene(Verdict.NOT_RUN, NO_GIT)
    return Hygiene(Verdict.PASS)


__all__ = [
    "ENV_NOT_IGNORED",
    "ENV_TRACKED",
    "KEY_IN_FILE",
    "KEY_IN_HISTORY",
    "KEY_SHAPE_IN_FILE",
    "NO_GIT",
    "Hygiene",
    "check_hygiene",
]
