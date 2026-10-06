"""Times chapter 1's offline run against a real, freshly launched sample app.

This measures ``run_offline`` itself (plan build time is negligible and left
out), not the console's startup or shutdown overhead: that's the number the
28 s budget is about. The app runs from a temp copy of the tracked tree, the
same as the other end-to-end tests, even though the driver code under test
here is the checkout's own (there's nothing that would tell the two apart).
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest

from playground.supervisor.launcher import AppProcess
from playground.traffic.driver import run_offline
from playground.traffic.plan import build_offline_plan

VISITORS = 10_000
BUDGET_S = 28.0
EXPECTED_SUCCESSES = 2_000


@pytest.mark.e2e
@pytest.mark.perf
def test_chapter1_10000_visitors_finishes_within_budget(tracked_copy):
    elapsed = asyncio.run(_time_chapter1_run(tracked_copy))
    print(f"chapter 1, {VISITORS:,} visitors: {elapsed:.2f}s (budget {BUDGET_S:.0f}s)")
    assert elapsed <= BUDGET_S


async def _time_chapter1_run(root: Path) -> float:
    app = AppProcess(root=root, log_path=root / ".playground" / "app.log")
    base_url, control_token, _boot_id = await app.start()
    try:
        plan = build_offline_plan(VISITORS, seed=0)
        started = time.monotonic()
        result = await run_offline(
            plan,
            base_url,
            control_token,
            on_progress=lambda _progress: None,
            stop=asyncio.Event(),
        )
        elapsed = time.monotonic() - started
    finally:
        await app.stop()

    assert result.total == VISITORS
    assert result.actions == VISITORS
    assert result.successes == EXPECTED_SUCCESSES
    return elapsed
