"""Where the sample app's SignalGate API key comes from.

The key is read once per boot from `SIGNALGATE_API_KEY`: first in `app/.env`
(the file `ENV_PATH` names), then in the environment. The placeholder that
`app/.env.example` ships with, a blank value and a malformed value all count
as no key. Both sources are read at import into one snapshot, and
`load_api_key()` and `key_state()` answer from it, so the key can't change
while the app runs. Saving `app/.env` restarts the app, which reads the key
again.

Nothing here ever logs, prints or shows the key's value.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Literal

# The value `app/.env.example` ships with; it counts as no key.
PLACEHOLDER_KEY = "PASTE_YOUR_API_KEY_HERE"

# The key file: `app/.env`, found from this package's location, never from the
# working directory. The snapshot reads whichever file this names at the moment
# it reads (at import, and again on each `app.signalgate_client._reset()`),
# which lets a test point it at a file of its own.
ENV_PATH = Path(__file__).resolve().parent.parent / ".env"

# What the key sources held at boot: "set" (a usable key), "invalid" (only a
# malformed value), "placeholder" (only the placeholder) or "absent".
KeyState = Literal["absent", "placeholder", "invalid", "set"]

_KEY_NAME = "SIGNALGATE_API_KEY"

# ``export KEY = value``, with whitespace allowed around the key and the `=`.
# Everything after the `=`, leading whitespace included, is the raw value text
# that `_unquote` picks apart: the whitespace has to stay, because it's what
# marks a `#` right after it as an inline comment.
_ASSIGNMENT = re.compile(r"^[ \t]*(?:export[ \t]+)?([A-Za-z_][A-Za-z0-9_]*)[ \t]*=(.*)$")

# Where an unquoted value's inline comment starts: a `#` after a space or tab.
_INLINE_COMMENT = re.compile(r"[ \t]#")

_ValueKind = Literal["absent", "blank", "placeholder", "malformed", "usable"]

# A real line ending only: LF, CRLF or a lone CR. Splitting on this, instead
# of `str.splitlines()`, keeps every other control character and every other
# Unicode line-boundary character (a form feed, NEL, a line separator, ...)
# inside the logical line it appeared on, so a stray one inside a value is
# caught as a malformed character instead of silently truncating the line.
_LINE_ENDING = re.compile(r"\r\n|\r|\n")


def _unquote(raw: str) -> tuple[str, bool]:
    """The assignment's own value, from ``raw`` (all the text after its
    `=`), and whether it parsed cleanly.

    A value opening with a quote (after any spaces or tabs) is taken
    verbatim up to its matching close; a quote that never closes (which is
    how a stray line ending inside it shows up, once the file's been split
    into logical lines) parses uncleanly and is never usable. An unquoted
    value ends at its inline comment, the first `#` after a space or tab,
    and is then stripped of plain spaces and tabs only, so a non-ASCII
    whitespace character at either end is kept and reported as malformed
    rather than silently dropped. A value that is only a comment
    (``KEY= # paste here``) is therefore blank.
    """
    value = raw.lstrip(" \t")
    if value[:1] in ("'", '"'):
        quote = value[0]
        end = value.find(quote, 1)
        if end == -1:
            return value[1:], False
        return value[1:end], True
    comment = _INLINE_COMMENT.search(raw)
    if comment is not None:
        raw = raw[: comment.start()]
    return raw.strip(" \t"), True


def _read_env_file() -> tuple[tuple[str, bool] | None, bool]:
    """The last ``SIGNALGATE_API_KEY`` assignment in ``ENV_PATH``, as
    ``(value, well_formed)``, plus whether the file exists at all.

    ``(None, False)`` means the file is absent (or otherwise unreadable as a
    file, for example a permissions error): that counts as no key, so a
    later source can still supply one.

    A byte that isn't valid UTF-8 only affects the line it's on: it's kept
    as a non-ASCII character, so a key line holding one is malformed, while
    one in a comment or another variable's line changes nothing.
    """
    try:
        data = ENV_PATH.read_bytes()
    except OSError:
        return None, False
    text = data.decode("utf-8-sig", errors="surrogateescape")
    found: tuple[str, bool] | None = None
    for line in _LINE_ENDING.split(text):
        stripped = line.lstrip()
        if not stripped or stripped.startswith("#"):
            continue
        match = _ASSIGNMENT.match(line)
        if not match or match.group(1) != _KEY_NAME:
            continue
        found = _unquote(match.group(2))
    return found, True


def _categorize(value: str) -> _ValueKind:
    """Where one already-parsed value stands: blank, the placeholder, usable
    (printable ASCII, no whitespace, not the placeholder) or malformed."""
    if value == "":
        return "blank"
    if value == PLACEHOLDER_KEY:
        return "placeholder"
    if all("\x21" <= ch <= "\x7e" for ch in value):
        return "usable"
    return "malformed"


def _file_kind(assignment: tuple[str, bool] | None, file_exists: bool) -> _ValueKind:
    if not file_exists or assignment is None:
        return "absent"
    value, well_formed = assignment
    if not well_formed:
        return "malformed"
    return _categorize(value)


def _compute() -> tuple[str | None, KeyState]:
    assignment, file_exists = _read_env_file()
    file_kind = _file_kind(assignment, file_exists)
    env_value = os.environ.get(_KEY_NAME)
    env_kind: _ValueKind = "absent" if env_value is None else _categorize(env_value)

    if file_kind == "usable":
        value, _ = assignment  # type: ignore[misc]
        return value, "set"
    if env_kind == "usable":
        return env_value, "set"
    if "malformed" in (file_kind, env_kind):
        return None, "invalid"
    if "placeholder" in (file_kind, env_kind):
        return None, "placeholder"
    return None, "absent"


_snapshot: tuple[str | None, KeyState] = _compute()


def load_api_key() -> str | None:
    """The usable API key read at boot, or None when there isn't one."""
    return _snapshot[0]


def key_state() -> KeyState:
    """What the key sources held at boot (see ``KeyState``)."""
    return _snapshot[1]


def _reload() -> None:
    """Recompute the boot snapshot from the current sources (tests only)."""
    global _snapshot
    _snapshot = _compute()


__all__ = ["ENV_PATH", "PLACEHOLDER_KEY", "KeyState", "key_state", "load_api_key"]
