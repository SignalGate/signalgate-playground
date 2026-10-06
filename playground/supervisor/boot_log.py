"""Pure text parsing over one boot's slice of ``.playground/app.log``.

Nothing here touches a live process: every function takes the log text (or
an error line) the caller already read, so they're trivial to unit test with
hand-built samples. OS-neutral on purpose: a developer's copy-pasted
traceback might carry Windows-shaped paths.

``boot_failure`` reads why a boot failed: the exception's name and message,
the ``app/`` line it points at (with the source line Python printed and the
caret line under it, if any), and a copy key for a fix hint in the common
cases. Anything shaped like an API key is masked, absolute paths under the
repo root are shortened to repo-relative ones, and a very long source line
is cut short.
"""

from __future__ import annotations

import os
import re

from playground.copy.en import text as copy_text
from playground.machine.events import BootFailure
from playground.verify.static import KEY_SHAPE

# A traceback frame, or the frame line CPython prints for a SyntaxError-family
# exception: both look like `File "<path>", line <n>`.
_FRAME_LINE = re.compile(r'File "(?P<path>[^"]+)", line (?P<line>\d+)')

# The address-in-use wording differs by OS but each is a fixed substring,
# regardless of the errno (48 on macOS, 98 on Linux): a raw OSError prints it
# capitalized, uvicorn's own wrapped log line prints it lower-case, so this
# compares case-insensitively. Windows raises OSError with this WinError
# number, always in this casing.
_BIND_IN_USE_MARKER = "address already in use"
_BIND_IN_USE_WINDOWS_MARKER = "WinError 10048"

# The line that ends a traceback: the exception's (possibly dotted) name and
# its message; or, for an exception raised with no message, its bare name.
_EXCEPTION_LINE = re.compile(r"^(?P<error>[A-Za-z_][\w.]*): (?P<message>.*)$")
_BARE_EXCEPTION_LINE = re.compile(r"^(?P<error>[A-Za-z_]\w*(?:Error|Exception))$")
# A server's own log line looks like one ("ERROR:    ..."); it never is.
_LOG_LEVELS = frozenset({"TRACE", "DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"})
# The marker line Python prints under a source line: spaces and ^ or ~ only.
_CARET_LINE = re.compile(r"^[ ^~]*[\^~][ ^~]*$")
# How many characters of the source line are shown; a longer one is cut short.
SOURCE_MAX_CHARS = 160

# Fix hints, by exception name.
_INDENT_ERRORS = frozenset({"IndentationError", "TabError"})
_IMPORT_ERRORS = frozenset({"ImportError", "ModuleNotFoundError"})
_CLIENT_MODULE = "'app.signalgate_client'"
_HINT_INDENT = "app.down.hint.indent"
_HINT_IMPORT = "app.down.hint.import"


def bind_failed(text: str) -> bool:
    """True if ``text`` (a boot's log slice) shows the port was already bound."""
    return _BIND_IN_USE_MARKER in text.lower() or _BIND_IN_USE_WINDOWS_MARKER in text


def _looks_windows_shaped(path_text: str) -> bool:
    return bool(re.match(r"^[A-Za-z]:[\\/]", path_text)) or "\\" in path_text


def _normalize_slashes(path_text: str) -> str:
    return path_text.replace("\\", "/")


def _app_prefixes(root: object) -> tuple[list[str], bool]:
    """The ``<root>/app/`` prefixes a frame path may start with, and whether
    they compare case-insensitively (a Windows-shaped ``root``)."""
    root_text = str(root)
    case_insensitive = _looks_windows_shaped(root_text)
    prefixes = [_normalize_slashes(root_text).rstrip("/") + "/app/"]
    if not case_insensitive:
        resolved = _resolved(root_text)
        if resolved is not None:
            resolved_prefix = _normalize_slashes(resolved).rstrip("/") + "/app/"
            if resolved_prefix not in prefixes:
                prefixes.append(resolved_prefix)
    return prefixes, case_insensitive


def _resolved(root_text: str) -> str | None:
    try:
        return os.path.realpath(root_text)
    except (OSError, ValueError):
        return None


def _last_app_frame(text: str, root: object) -> tuple[str, int] | None:
    """The last frame under ``root/app`` as ``(app/<file>:<n>, end of its line)``."""
    prefixes, case_insensitive = _app_prefixes(root)
    prefixes_cmp = [prefix.lower() if case_insensitive else prefix for prefix in prefixes]

    found: tuple[str, int] | None = None
    for match in _FRAME_LINE.finditer(text):
        candidate = _normalize_slashes(match.group("path"))
        candidate_cmp = candidate.lower() if case_insensitive else candidate
        matched = next(
            (prefix for prefix in prefixes_cmp if candidate_cmp.startswith(prefix)), None
        )
        if matched is None:
            continue
        relative = candidate[len(matched) :]
        end = text.find("\n", match.end())
        found = (f"app/{relative}:{match.group('line')}", len(text) if end < 0 else end)
    return found


def failure_location(text: str, root: object) -> str | None:
    """The last ``app/…py:<n>`` frame in ``text`` that resolves under ``root/app``.

    ``root`` is compared as text first, never touching the real filesystem,
    so a Windows-shaped ``root`` (a drive letter, backslashes) works the same
    way a POSIX one does. A POSIX ``root`` is also compared against its own
    resolved (symlink-free) form, because a real traceback always names the
    resolved path: an unresolved ``root`` under a symlinked directory (a
    developer's temp dir, for example) must still match. Returns None when no
    frame in ``text`` is under ``root/app`` -- for example when every frame is
    in site-packages.
    """
    frame = _last_app_frame(text, root)
    return frame[0] if frame is not None else None


def _exception_after(lines: list[str]) -> tuple[str, str] | None:
    """The exception that ends the traceback ``lines`` carry on from, as
    ``(name, message)``.

    ``lines`` start right after a frame line. Python prints the deeper
    frames, and the source and caret lines under each, indented; the first
    line after them that names an exception is the one this traceback ends
    with. The server's own log lines are skipped, and what comes after the
    exception (more log lines, the rest of a message that runs over several
    lines) is never read as it.
    """
    for line in lines:
        if not line.strip() or line[:1].isspace() or _FRAME_LINE.match(line):
            continue
        match = _EXCEPTION_LINE.match(line) or _BARE_EXCEPTION_LINE.match(line)
        if match is None or match.group("error") in _LOG_LEVELS:
            continue
        message = match.groupdict().get("message") or ""
        return match.group("error"), message
    return None


def _shorten_paths(message: str, root: object) -> str:
    """``message`` with every absolute path under ``root`` made repo-relative."""
    root_text = str(root).rstrip("/\\")
    if not root_text:
        return message
    roots = [root_text]
    if not _looks_windows_shaped(root_text):
        resolved = _resolved(root_text)
        if resolved is not None and resolved.rstrip("/") not in roots:
            roots.append(resolved.rstrip("/"))
    # Longest first, so a root that contains another is shortened whole.
    for prefix in sorted(roots, key=len, reverse=True):
        for separator in ("/", "\\"):
            message = message.replace(prefix + separator, "")
    return message


def _mask(value: str) -> str:
    return KEY_SHAPE.sub(copy_text("verify.key_masked"), value)


def _shown(source: str, caret: str | None) -> tuple[str, str | None]:
    """The source line and its caret line as they are shown: a source line
    over ``SOURCE_MAX_CHARS`` is cut short with an ellipsis, and its caret
    line with it (gone if nothing it marks is left)."""
    if len(source) <= SOURCE_MAX_CHARS:
        return source, caret
    kept = SOURCE_MAX_CHARS - 1
    source = source[:kept] + copy_text("verify.ellipsis")
    if caret is not None:
        caret = caret[:kept].rstrip() or None
    return source, caret


def _hint(error: str, message: str) -> str | None:
    name = error.rsplit(".", 1)[-1]
    if name in _INDENT_ERRORS:
        return _HINT_INDENT
    if name in _IMPORT_ERRORS and _CLIENT_MODULE in message:
        return _HINT_IMPORT
    return None


def boot_failure(text: str, root: object) -> BootFailure | None:
    """Why a boot failed, read from its log slice ``text``; None when unknown.

    The location is the last frame under ``root/app`` (as for
    ``failure_location``); ``source`` is the line Python printed under that
    frame, exactly as printed (cut short past ``SOURCE_MAX_CHARS``), and
    ``caret`` the marker line under it when there is one. The exception is
    the one the traceback ends with, read forward from that frame. Returns
    None when no frame is under ``root/app`` or no exception line follows it.
    """
    frame = _last_app_frame(text, root)
    if frame is None:
        return None
    location, end = frame
    after = text[end + 1 :].splitlines() if end < len(text) else []
    exception = _exception_after(after)
    if exception is None:
        return None
    error, message = exception

    # Python prints the source line indented under its frame; a frame
    # without one is followed by the next frame or the exception instead.
    printed = bool(after) and after[0][:1].isspace()
    source = caret = None
    if printed and not _FRAME_LINE.search(after[0]) and not _CARET_LINE.match(after[0]):
        if len(after) > 1 and _CARET_LINE.match(after[1]):
            caret = after[1]
        source, caret = _shown(_mask(after[0]), caret)

    message = _mask(_shorten_paths(message, root))
    return BootFailure(
        error=error,
        message=message,
        location=location,
        source=source,
        caret=caret,
        hint=_hint(error, message),
    )


__all__ = ["SOURCE_MAX_CHARS", "bind_failed", "boot_failure", "failure_location"]
