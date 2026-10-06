"""The offline visitor plan for chapter 1.

``build_offline_plan`` produces a deterministic list of visitors: mostly the
attack, a minority real, with a fixed share of the real visitors going on to
succeed. Nothing here talks to a network; the plan is pure data that the
driver later sends to the sample app.
"""

from __future__ import annotations

import random
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

from playground.traffic.local_constants import OFFLINE_SUCCESS_DELAY_MAX_S
from playground.traffic.sampler import Sampler

Cls = Literal["real", "attack"]

# Modelled public /demo constants: about 1 real visitor for every 2.7 attack
# visitors, and 74% of real visitors go on to succeed.
REAL_TO_TOTAL_RATIO = 3.7
REAL_SUCCESS_SHARE = 0.74

# Every consecutive run of this many visitors gets its own share of real
# visitors (see ``_block_real_counts``), so the mix never drifts far from the
# overall ratio even early in a run.
VISITS_PER_BLOCK = 100

REF_HEX_LEN = 16
_HEX_DIGITS = "0123456789abcdef"


@dataclass(frozen=True)
class Visit:
    """One offline visitor sent through the sample app for chapter 1."""

    ref: str
    cls: Cls
    user_id: str
    client_ip: str
    success: bool
    success_after_ms: int
    envelopes: Mapping | None = None


def _block_sizes(n: int) -> list[int]:
    sizes = []
    remaining = n
    while remaining > 0:
        size = min(VISITS_PER_BLOCK, remaining)
        sizes.append(size)
        remaining -= size
    return sizes


def _block_real_counts(n: int, real_total: int) -> list[int]:
    """Split ``real_total`` across blocks using largest-remainder allocation.

    Every block gets its floor share of the overall ratio; whatever is left
    over goes to the blocks whose share was rounded down the most, so the
    per-block counts stay close to proportional without ever needing to
    touch which visitors land in which block (that comes later, by shuffle).
    """
    sizes = _block_sizes(n)
    ideal = [real_total * size / n for size in sizes] if n else []
    base = [int(x) for x in ideal]
    remainder = real_total - sum(base)

    order = sorted(range(len(sizes)), key=lambda i: ideal[i] - base[i], reverse=True)
    for i in order[:remainder]:
        base[i] += 1

    return base


def _unique_ref(rng: random.Random, seen: set[str]) -> str:
    while True:
        ref = "".join(rng.choices(_HEX_DIGITS, k=REF_HEX_LEN))
        if ref not in seen:
            seen.add(ref)
            return ref


def build_offline_plan(n: int = 10_000, seed: int = 0) -> list[Visit]:
    """Build ``n`` deterministic offline visitors for the given ``seed``."""
    rng = random.Random(seed)

    real_total = round(n / REAL_TO_TOTAL_RATIO)
    real_success_total = round(REAL_SUCCESS_SHARE * real_total)

    classes: list[Cls] = []
    for size, real_count in zip(_block_sizes(n), _block_real_counts(n, real_total)):
        block: list[Cls] = ["real"] * real_count + ["attack"] * (size - real_count)
        rng.shuffle(block)
        classes.extend(block)

    real_indices = [i for i, cls in enumerate(classes) if cls == "real"]
    success_indices = set(rng.sample(real_indices, real_success_total))

    sampler = Sampler(rng)
    delay_cap_ms = OFFLINE_SUCCESS_DELAY_MAX_S * 1000
    seen_refs: set[str] = set()

    visits = []
    for i, cls in enumerate(classes):
        user_id, client_ip = sampler.sample()
        visits.append(
            Visit(
                ref=_unique_ref(rng, seen_refs),
                cls=cls,
                user_id=user_id,
                client_ip=client_ip,
                success=i in success_indices,
                success_after_ms=round(rng.uniform(0, delay_cap_ms)),
            )
        )
    return visits


__all__ = ["REAL_SUCCESS_SHARE", "REAL_TO_TOTAL_RATIO", "Visit", "build_offline_plan"]
