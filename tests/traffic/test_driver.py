"""Tests for the offline driver against a real stub of the sample app."""

from __future__ import annotations

import asyncio

from playground.traffic.driver import run_offline
from playground.traffic.plan import Visit, build_offline_plan
from tests.traffic._stub_app import CONTROL_TOKEN, StubApp

# n=100 -> 27 real, 73 attack, 20 real successes (round(100/3.7), round(0.74*27)).
COUNTS_PLAN_SIZE = 100

# n=250 -> checkpoints at done=100 and done=200, plus the final call at 250.
CADENCE_PLAN_SIZE = 250


def test_driver_tallies_every_visit_correctly():
    asyncio.run(_run_full_plan_and_check_counts())


async def _run_full_plan_and_check_counts():
    plan = build_offline_plan(n=COUNTS_PLAN_SIZE, seed=0)
    async with StubApp() as base_url:
        result = await run_offline(
            plan,
            base_url,
            CONTROL_TOKEN,
            on_progress=lambda _progress: None,
            stop=asyncio.Event(),
        )

    assert result.total == COUNTS_PLAN_SIZE
    assert result.done == COUNTS_PLAN_SIZE
    assert result.actions == COUNTS_PLAN_SIZE
    assert result.successes == 20
    assert result.attack == 73
    assert result.real == 27
    assert result.stopped is False
    assert len(result.statuses) == COUNTS_PLAN_SIZE


def test_progress_callback_fires_every_100_and_once_more_at_the_end():
    asyncio.run(_run_full_plan_and_check_progress_cadence())


async def _run_full_plan_and_check_progress_cadence():
    plan = build_offline_plan(n=CADENCE_PLAN_SIZE, seed=0)
    seen: list = []
    async with StubApp() as base_url:
        result = await run_offline(
            plan,
            base_url,
            CONTROL_TOKEN,
            on_progress=seen.append,
            stop=asyncio.Event(),
        )

    dones = [progress.done for progress in seen]
    for checkpoint in (100, 200, CADENCE_PLAN_SIZE):
        assert checkpoint in dones
    assert dones[-1] == CADENCE_PLAN_SIZE
    assert seen[-1].total == CADENCE_PLAN_SIZE
    assert result.done == CADENCE_PLAN_SIZE


def test_stop_mid_run_finishes_in_flight_visits_and_starts_no_more():
    asyncio.run(_run_and_stop_partway())


async def _run_and_stop_partway():
    # A per-action delay widens the window in which the run is still
    # admitting new visitors, so stopping after the first checkpoint
    # reliably leaves work undone.
    plan = build_offline_plan(n=200, seed=0)
    stop = asyncio.Event()
    first_checkpoint = asyncio.Event()

    def on_progress(progress) -> None:
        if progress.done >= 100:
            first_checkpoint.set()

    async with StubApp(action_delay_s=0.05) as base_url:
        run_task = asyncio.ensure_future(
            run_offline(plan, base_url, CONTROL_TOKEN, on_progress=on_progress, stop=stop)
        )
        await first_checkpoint.wait()
        stop.set()
        result = await run_task

    assert result.stopped is True
    assert 0 < result.done < result.total
    # The stub app never fails a well-formed request, so every visit that
    # actually started completed its action call -- nothing is left half
    # recorded (no orphaned in-flight count).
    assert result.actions == result.done
    assert len(result.statuses) == result.done
    assert all(
        status in {"success", "no_success", "confirm_failed"} for status in result.statuses.values()
    )


# -- Per-request headers -------------------------------------------------------


def _one_successful_visit() -> Visit:
    return Visit(
        ref="ref-headers-test",
        cls="real",
        user_id="user-headers-test",
        client_ip="203.0.113.5",
        success=True,
        success_after_ms=0,
    )


def _header(headers: dict, name: str) -> str | None:
    lowered = name.lower()
    for key, value in headers.items():
        if key.lower() == lowered:
            return value
    return None


def test_action_and_confirm_carry_ref_run_and_xff_with_a_run_id():
    asyncio.run(_action_and_confirm_carry_ref_run_and_xff_with_a_run_id())


async def _action_and_confirm_carry_ref_run_and_xff_with_a_run_id():
    visit = _one_successful_visit()
    run_id = "run-abc123"
    stub = StubApp()
    async with stub as base_url:
        result = await run_offline(
            [visit],
            base_url,
            CONTROL_TOKEN,
            on_progress=lambda _progress: None,
            stop=asyncio.Event(),
            run_id=run_id,
        )
        assert result.successes == 1

        action_headers = await stub.requests_for("/api/action")
        confirm_headers = await stub.requests_for("/api/confirm")
        inbox_headers = await stub.requests_for("/__playground/inbox")

    assert len(action_headers) == 1
    assert _header(action_headers[0], "X-Playground-Ref") == visit.ref
    assert _header(action_headers[0], "X-Playground-Run") == run_id
    assert _header(action_headers[0], "X-Forwarded-For") == visit.client_ip

    assert len(confirm_headers) == 1
    assert _header(confirm_headers[0], "X-Playground-Ref") == visit.ref
    assert _header(confirm_headers[0], "X-Playground-Run") == run_id
    assert _header(confirm_headers[0], "X-Forwarded-For") == visit.client_ip

    assert len(inbox_headers) == 1
    assert _header(inbox_headers[0], "Authorization") == f"Bearer {CONTROL_TOKEN}"
    assert _header(inbox_headers[0], "X-Playground-Run") == run_id
    assert _header(inbox_headers[0], "X-Forwarded-For") is None


def test_without_a_run_id_no_run_header_is_sent_anywhere():
    asyncio.run(_without_a_run_id_no_run_header_is_sent_anywhere())


async def _without_a_run_id_no_run_header_is_sent_anywhere():
    visit = _one_successful_visit()
    stub = StubApp()
    async with stub as base_url:
        result = await run_offline(
            [visit],
            base_url,
            CONTROL_TOKEN,
            on_progress=lambda _progress: None,
            stop=asyncio.Event(),
        )
        assert result.successes == 1

        action_headers = await stub.requests_for("/api/action")
        confirm_headers = await stub.requests_for("/api/confirm")
        inbox_headers = await stub.requests_for("/__playground/inbox")

    for headers in (*action_headers, *confirm_headers, *inbox_headers):
        assert _header(headers, "X-Playground-Run") is None
