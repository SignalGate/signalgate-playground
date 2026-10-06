"""Canary nonces the sample app answers on the laptop, without the network.

The console registers a nonce here, through `POST /__playground/canary`,
together with the answer the kit transport should give when a request
carries that nonce: one of the public verdicts, or a 422 rejection. The
answer may come after a short local delay. A registration lasts for the
app's boot and can be answered any number of times; registering the same
nonce again replaces its answer.
"""

from __future__ import annotations

import threading
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

# Looked up in this module at call time, so a test can patch them small here.
from app.kit.local_constants import CANARY_DELAY_MAX_MS, CANARY_NONCE_MAX_CHARS, PAGE_MAX

ANSWERS = ("block", "dry_run_block", "admin_alert", "422")

_ITEM_KEYS = frozenset({"nonce", "answer", "delay_ms"})
_REQUIRED_KEYS = frozenset({"nonce", "answer"})


@dataclass(frozen=True)
class Canary:
    """How to answer a registered nonce: ``answer`` after ``delay_ms``."""

    answer: str
    delay_ms: int


def _validated(item: Any) -> tuple[str, Canary]:
    # Every invalid input is a ValueError, whatever its kind: the control route
    # answers any of them with a 400 and registers nothing.
    if not isinstance(item, Mapping):
        raise ValueError("each canary must be an object")  # noqa: TRY004
    keys = set(item)
    if not keys <= _ITEM_KEYS or not _REQUIRED_KEYS <= keys:
        raise ValueError("a canary has exactly nonce, answer and an optional delay_ms")
    nonce = item["nonce"]
    if not isinstance(nonce, str) or not 1 <= len(nonce) <= int(CANARY_NONCE_MAX_CHARS):
        raise ValueError("nonce must be a non-empty string of bounded length")
    answer = item["answer"]
    if not isinstance(answer, str) or answer not in ANSWERS:
        raise ValueError("answer is not one of the allowed answers")
    delay_ms = item.get("delay_ms", 0)
    if (
        not isinstance(delay_ms, int)
        or isinstance(delay_ms, bool)
        or not 0 <= delay_ms <= int(CANARY_DELAY_MAX_MS)
    ):
        raise ValueError("delay_ms must be a whole number of milliseconds within the limit")
    return nonce, Canary(answer=answer, delay_ms=delay_ms)


class CanaryRegistry:
    """Registered canary nonces for this boot. Thread-safe."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._by_nonce: dict[str, Canary] = {}

    def register(self, items: Iterable[Mapping[str, Any]]) -> int:
        """Register every item, or none of them.

        Each item is ``{nonce, answer, delay_ms?}``; ``delay_ms`` defaults to
        0. Every item is checked before any is applied, so one invalid item
        raises ``ValueError`` and registers nothing. Items apply in order, so
        a nonce listed twice keeps its last answer. Returns how many items
        were registered.
        """
        if isinstance(items, (str, bytes, Mapping)) or not isinstance(items, Iterable):
            raise ValueError("canaries must be a list")  # noqa: TRY004
        items = list(items)
        if not 1 <= len(items) <= int(PAGE_MAX):
            raise ValueError("the number of canaries is out of range")
        entries = [_validated(item) for item in items]
        with self._lock:
            for nonce, canary in entries:
                self._by_nonce[nonce] = canary
        return len(entries)

    def lookup(self, nonce: Any) -> Canary | None:
        """The canary registered for ``nonce``, or None."""
        if not isinstance(nonce, str):
            return None
        with self._lock:
            return self._by_nonce.get(nonce)

    def clear(self) -> None:
        """Forget every registration."""
        with self._lock:
            self._by_nonce.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._by_nonce)


canaries = CanaryRegistry()

__all__ = ["ANSWERS", "Canary", "CanaryRegistry", "canaries"]
