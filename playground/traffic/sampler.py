"""One class-independent sampler for offline visitor identifiers.

The same distribution of user ids and client IPs serves every visitor,
whether it turns out to be real or an attack: the sampler never takes a
class argument, and nothing about its output depends on one.

Client IPs come from the documentation ranges reserved for examples
(RFC 5737 / RFC 3849 style ranges), never a real address.
"""

from __future__ import annotations

import random

USER_ID_PREFIX = "pg-u-"
USER_ID_HEX_LEN = 12

# Documentation-only IPv4 ranges (RFC 5737): safe to print anywhere.
DOC_RANGE_PREFIXES = ("192.0.2", "198.51.100", "203.0.113")
HOST_OCTET_MIN = 1
HOST_OCTET_MAX = 254

_HEX_DIGITS = "0123456789abcdef"


class Sampler:
    """Produces user ids and client IPs; carries no notion of visitor class."""

    def __init__(self, rng: random.Random) -> None:
        self._rng = rng

    def user_id(self) -> str:
        digits = "".join(self._rng.choices(_HEX_DIGITS, k=USER_ID_HEX_LEN))
        return f"{USER_ID_PREFIX}{digits}"

    def client_ip(self) -> str:
        prefix = self._rng.choice(DOC_RANGE_PREFIXES)
        host = self._rng.randint(HOST_OCTET_MIN, HOST_OCTET_MAX)
        return f"{prefix}.{host}"

    def sample(self) -> tuple[str, str]:
        """Return one ``(user_id, client_ip)`` pair."""
        return self.user_id(), self.client_ip()


__all__ = ["Sampler"]
