#!/usr/bin/env python3
"""Fail if the repo tracks a file outside its approved layout.

Stdlib only: this script must run with a bare `python3` and no dependencies
installed, since the CI job that calls it installs nothing.
"""

from __future__ import annotations

import fnmatch
import subprocess
import sys

# Exact top-level filenames that are always allowed.
ALLOWED_EXACT = {
    "pyproject.toml",
    "uv.lock",
    ".python-version",
    "README.md",
    "LICENSE",
    "NOTICE",
    "TRADEMARK.md",
    ".gitignore",
}

# Glob patterns (matched case-sensitively) for the allowed directory trees
# and file families. `*` matches across path separators here, so a single
# star behaves like a recursive glob for our purposes.
ALLOWED_GLOBS = (
    "playground/**",
    "app/**",
    "solutions/**",
    "contracts/*.json",
    "tests/**",
    "docs/**",
    "scripts/*.py",
    ".github/workflows/ci.yml",
    ".github/workflows/disclosure-collect.yml",
    ".github/workflows/disclosure-scan.yml",
    ".github/CODEOWNERS",
    ".github/ISSUE_TEMPLATE/**",
)

# Path segments that are never allowed to be tracked, wherever they occur.
DENIED_SEGMENTS = {".claude", ".dreamteam", ".playground", ".venv", "__pycache__"}
DENIED_COMPONENT_GLOB = "*architecture*"
DENIED_BASENAME_GLOB = "*spec*.md"
ALLOWED_ENV_BASENAME = ".env.example"


def _looks_like_env_file(basename: str) -> bool:
    if basename == ".env":
        return True
    if fnmatch.fnmatchcase(basename, "*.env"):
        return True
    return basename.startswith(".env.")


def _is_denied(path: str) -> bool:
    """Deny overrides apply anywhere in the path, case-insensitively."""
    lowered = path.lower()
    segments = lowered.split("/")
    basename = segments[-1]

    if basename == "claude.md":
        return True
    if any(segment in DENIED_SEGMENTS for segment in segments):
        return True
    if any(fnmatch.fnmatchcase(segment, DENIED_COMPONENT_GLOB) for segment in segments):
        return True
    if fnmatch.fnmatchcase(basename, DENIED_BASENAME_GLOB):
        return True
    return _looks_like_env_file(basename) and basename != ALLOWED_ENV_BASENAME


def _is_allowed(path: str) -> bool:
    if path in ALLOWED_EXACT:
        return True
    return any(fnmatch.fnmatchcase(path, pattern) for pattern in ALLOWED_GLOBS)


def find_violations(paths: list[str]) -> list[str]:
    """Return the subset of `paths` that are NOT allowed to be tracked."""
    violations = []
    for path in paths:
        if _is_denied(path) or not _is_allowed(path):
            violations.append(path)
    return violations


def _tracked_paths() -> list[str]:
    result = subprocess.run(
        ["git", "ls-files", "-z"],
        check=True,
        capture_output=True,
    )
    entries = result.stdout.decode("utf-8").split("\0")
    return [entry for entry in entries if entry]


def main() -> int:
    violations = find_violations(_tracked_paths())
    if violations:
        for path in violations:
            print(path)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
