"""Where the console finds your API key, and whether it has the right shape.

The key comes from ``SIGNALGATE_API_KEY`` in ``app/.env`` (the last
assignment wins), else from the environment, exactly as the sample app reads
it. Never from the command line. Nothing here prints, logs or stores it.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

KEY_NAME = "SIGNALGATE_API_KEY"
ENV_RELATIVE = Path("app") / ".env"
PLACEHOLDER = "PASTE_YOUR_API_KEY_HERE"

KEY_PREFIX = "pk_live_"
KEY_LENGTH = 40
# A value of this length without the prefix is the workspace's public key.
PUBLIC_KEY_LENGTH = 43
_FULL_SHAPE = re.compile(r"pk_live_[A-Za-z0-9_-]{32}")

_ASSIGNMENT = re.compile(r"^[ \t]*(?:export[ \t]+)?([A-Za-z_][A-Za-z0-9_]*)[ \t]*=(.*)$")
_INLINE_COMMENT = re.compile(r"[ \t]#")

# What ``problem()`` may answer.
MISSING = "missing"
PUBLIC_KEY = "public_key"
BAD_FORMAT = "format"


def _unquote(raw: str) -> str:
    value = raw.lstrip(" \t")
    if value[:1] in ("'", '"'):
        end = value.find(value[0], 1)
        return value[1:end] if end != -1 else value[1:]
    comment = _INLINE_COMMENT.search(raw)
    if comment is not None:
        raw = raw[: comment.start()]
    return raw.strip(" \t")


def _from_file(path: Path) -> str | None:
    try:
        text = path.read_text(encoding="utf-8-sig", errors="replace")
    except OSError:
        return None
    found: str | None = None
    for line in re.split(r"\r\n|\r|\n", text):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        match = _ASSIGNMENT.match(line)
        if match and match.group(1) == KEY_NAME:
            found = _unquote(match.group(2))
    return found


def read_key(root: Path) -> str | None:
    """The key in ``<root>/app/.env``, else in the environment; None when there's none."""
    for value in (_from_file(root / ENV_RELATIVE), os.environ.get(KEY_NAME)):
        if value and value != PLACEHOLDER:
            return value
    return None


def problem(value: str | None) -> str | None:
    """What's wrong with ``value`` as an API key (``MISSING``, ``PUBLIC_KEY``,
    ``BAD_FORMAT``), or None when it has the right shape."""
    if not value or value == PLACEHOLDER:
        return MISSING
    if _FULL_SHAPE.fullmatch(value):
        return None
    if len(value) == PUBLIC_KEY_LENGTH and not value.startswith(KEY_PREFIX):
        return PUBLIC_KEY
    return BAD_FORMAT


__all__ = [
    "BAD_FORMAT",
    "ENV_RELATIVE",
    "KEY_LENGTH",
    "KEY_NAME",
    "KEY_PREFIX",
    "MISSING",
    "PUBLIC_KEY",
    "problem",
    "read_key",
]
