"""Saved progress for ``/continue``: ``.playground/state.json``.

The file is ``{"v": 2, "machine": <Machine.to_dict()>}``. It holds no
secrets, no paths and no timestamps: the time spent since ``/start`` is kept
as a duration. Saves are atomic: a new file is written next to the old one
and moved over it, so a crash never leaves half a file.

Older saves still load, as this version would have saved them. A version 1
file (chapter 1 only) gains, through ``_from_v1``, what version 2 keeps for
the later chapters; its end state, "chapter 1 done, nothing more in this
version", resumes at chapter 2. A version 2 file saved before the
achievements and the time were kept gains the achievements its progress has
earned, and no time.

The console holds ``console.lock`` while it reads or writes this file.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from pathlib import Path
from tempfile import mkstemp
from typing import Any

from playground.machine.machine import Machine

_VERSION = 2
_V1 = 1
_VERSION_KEY = "v"
_MACHINE_KEY = "machine"
_BAD_SUFFIX = ".bad"
_TMP_PREFIX = "."
_TMP_SUFFIX = ".tmp"


def _stale_temp_glob(path: Path) -> str:
    """The ``mkstemp`` pattern this module's own saves use for ``path``."""
    return f"{_TMP_PREFIX}{path.name}.*{_TMP_SUFFIX}"


def _sweep_stale_temp_files(path: Path) -> None:
    """Remove leftover ``mkstemp`` files a prior crash never got to rename."""
    try:
        stale_files = list(path.parent.glob(_stale_temp_glob(path)))
    except OSError:
        return
    for stale in stale_files:
        try:
            stale.unlink()
        except OSError:
            pass


def _set_aside(path: Path) -> tuple[None, str | None]:
    """Move an unreadable or invalid file out of the way and report it.

    Returns the notice only when the move actually happened: a caller that
    can't move the file (for example a read-only directory) gets
    ``(None, None)`` instead, rather than telling the user about a move that
    never took place.
    """
    try:
        os.replace(path, path.with_name(path.name + _BAD_SUFFIX))
    except OSError:
        return None, None
    return None, "state.set_aside"


def _from_v1(machine: Any) -> dict[str, Any]:
    """A version 1 machine snapshot, as version 2 would have saved it."""
    if not isinstance(machine, dict):
        raise ValueError("bad state file shape")  # noqa: TRY004 - one "set it aside" signal
    snapshot = dict(machine)
    at_the_old_end = snapshot.get("chapter1_done") is True and snapshot.get("awaiting") == "any"
    snapshot.update(
        {
            "size": Machine().to_dict()["size"],
            "step": "KEY" if at_the_old_end else None,
            "session": None,
            "learn": None,
            "prediction": None,
            "rounds": [],
            "stage": None,
            "topup": 0,
        }
    )
    if at_the_old_end:
        snapshot["awaiting"] = "continue"
    return snapshot


def load(path: Path) -> tuple[Mapping[str, Any] | None, str | None]:
    """Read saved progress from ``path``.

    Returns ``(snapshot, notice_key)``: ``(None, None)`` when there is no
    file, the saved machine snapshot when it reads cleanly, or
    ``(None, "state.set_aside")`` after moving an unreadable file aside to
    ``state.json.bad``.
    """
    path = Path(path)
    _sweep_stale_temp_files(path)

    try:
        raw_bytes = path.read_bytes()
    except FileNotFoundError:
        return None, None
    except OSError:
        return _set_aside(path)

    try:
        raw = raw_bytes.decode("utf-8")
        data = json.loads(raw)
        # Exactly the two keys save() writes, and a version that is the
        # integer itself: ``true`` and ``2.0`` compare equal to integers in
        # Python but are not what save() writes.
        if (
            not isinstance(data, dict)
            or set(data) != {_VERSION_KEY, _MACHINE_KEY}
            or type(data[_VERSION_KEY]) is not int
            or data[_VERSION_KEY] not in (_V1, _VERSION)
        ):
            raise ValueError("bad state file shape")
        snapshot = data[_MACHINE_KEY]
        if data[_VERSION_KEY] == _V1:
            snapshot = _from_v1(snapshot)
        # The snapshot is only handed back once it is proven loadable, and
        # as this version saves it: an older save gains the fields added
        # since, so a machine offered it serialises back to exactly it.
        snapshot = Machine.from_dict(snapshot).to_dict()
    except (
        UnicodeDecodeError,
        json.JSONDecodeError,
        ValueError,
        KeyError,
        TypeError,
        OverflowError,
        RecursionError,
    ):
        return _set_aside(path)

    return snapshot, None


def save(path: Path, snapshot: Mapping[str, Any]) -> bool:
    """Atomically write ``snapshot`` to ``path``.

    Returns True when the file was written, False when it couldn't be (the
    old file, if any, is left as it was). Never raises.
    """
    path = Path(path)
    payload = {_VERSION_KEY: _VERSION, _MACHINE_KEY: dict(snapshot)}

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        return False

    fd: int | None = None
    tmp_name: str | None = None
    try:
        fd, tmp_name = mkstemp(
            prefix=f"{_TMP_PREFIX}{path.name}.", suffix=_TMP_SUFFIX, dir=str(path.parent)
        )
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            fd = None  # fdopen now owns the descriptor.
            json.dump(payload, handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
        tmp_name = None
        return True
    except OSError:
        return False
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        if tmp_name is not None:
            try:
                os.remove(tmp_name)
            except OSError:
                pass


__all__ = ["load", "save"]
