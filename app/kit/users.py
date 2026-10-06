"""The one user-id pattern shared by validation and by anything that needs
to generate ids that always pass it.

A reserved prefix always fails on purpose, so a caller can exercise the
"bad user" path deterministically instead of guessing at an invalid string.
"""

from __future__ import annotations

import re

USER_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")

RESERVED_BAD_PREFIX = "pg-bad-"


def is_valid_user(user: str) -> bool:
    """Return True if `user` matches the pattern and isn't the reserved prefix."""
    if not isinstance(user, str) or user.startswith(RESERVED_BAD_PREFIX):
        return False
    return USER_PATTERN.match(user) is not None


__all__ = ["RESERVED_BAD_PREFIX", "USER_PATTERN", "is_valid_user"]
