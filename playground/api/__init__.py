"""The console's client for the playground API, and where it finds your API key.

``PlaygroundApi`` talks to ``https://api.signalgate.ai/v0/playground/*``;
``decode`` holds the whitelisting decoders its answers go through; ``key``
reads the API key from ``app/.env``.
"""

from playground.api.client import (
    BASE_URL,
    PROTOCOL_HEADER,
    PROTOCOL_VERSION,
    ApiError,
    ApiUnreachable,
    PlaygroundApi,
    ProtocolOutdated,
)

__all__ = [
    "BASE_URL",
    "PROTOCOL_HEADER",
    "PROTOCOL_VERSION",
    "ApiError",
    "ApiUnreachable",
    "PlaygroundApi",
    "ProtocolOutdated",
]
