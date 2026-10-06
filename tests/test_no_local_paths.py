"""Guards against local-machine paths or internal-only file names leaking in.

Scans every tracked file's text content (binary files are skipped) for a
handful of forbidden substrings. Every tracked file is scanned, including
this file itself, so none of the needles below may appear contiguously in
this file's own source -- each is assembled from fragments at runtime.
"""

from __future__ import annotations

import subprocess


def _tracked_files() -> list[str]:
    result = subprocess.run(
        ["git", "ls-files", "-z"],
        check=True,
        capture_output=True,
    )
    entries = result.stdout.decode("utf-8").split("\0")
    return [entry for entry in entries if entry]


def _read_text_or_none(path: str) -> str | None:
    try:
        with open(path, encoding="utf-8") as handle:
            return handle.read()
    except (UnicodeDecodeError, OSError):
        return None


def _forbidden_needles() -> list[str]:
    # Built from fragments, deliberately, so this file's own source never
    # contains any of these substrings contiguously -- otherwise the
    # self-scan below would trivially fail against this very file.
    home_mac = "".join(["/", "Users", "/"])  # noqa: FLY002
    home_linux = "".join(["/", "home", "/"])  # noqa: FLY002
    internal_spec_filename = "".join(  # noqa: FLY002
        ["signalgate-playground", "-spec", ".md"]
    )
    return [home_mac, home_linux, internal_spec_filename]


def test_no_tracked_file_leaks_a_local_path_or_the_internal_spec_name():
    needles = _forbidden_needles()
    offenders = {}

    for path in _tracked_files():
        content = _read_text_or_none(path)
        if content is None:
            continue
        hits = [needle for needle in needles if needle in content]
        if hits:
            offenders[path] = hits

    assert offenders == {}
