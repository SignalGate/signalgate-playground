"""Tests for the deterministic offline visitor plan."""

from __future__ import annotations

from playground.traffic.plan import VISITS_PER_BLOCK, Visit, build_offline_plan


def test_plan_counts_match_the_modelled_ratio():
    plan = build_offline_plan(n=10_000, seed=0)
    assert len(plan) == 10_000

    attack = [v for v in plan if v.cls == "attack"]
    real = [v for v in plan if v.cls == "real"]
    assert len(attack) == 7_297
    assert len(real) == 2_703

    real_successes = [v for v in real if v.success]
    attack_successes = [v for v in attack if v.success]
    assert len(real_successes) == 2_000
    assert len(attack_successes) == 0


def test_plan_counts_are_stable_across_repeated_calls():
    # The same seed must give the same shape every time, not just once.
    for _ in range(10):
        plan = build_offline_plan(n=10_000, seed=0)
        real = sum(1 for v in plan if v.cls == "real")
        attack = sum(1 for v in plan if v.cls == "attack")
        successes = sum(1 for v in plan if v.success)
        attack_successes = sum(1 for v in plan if v.cls == "attack" and v.success)
        assert (real, attack, successes, attack_successes) == (2_703, 7_297, 2_000, 0)


def test_every_block_of_100_has_25_to_29_real_visitors():
    plan = build_offline_plan(n=10_000, seed=0)
    for start in range(0, len(plan), VISITS_PER_BLOCK):
        block = plan[start : start + VISITS_PER_BLOCK]
        real_in_block = sum(1 for v in block if v.cls == "real")
        assert 25 <= real_in_block <= 29, (start, real_in_block)


def test_success_after_ms_is_within_the_offline_delay_cap():
    plan = build_offline_plan(n=1_000, seed=0)
    for visit in plan:
        assert 0 <= visit.success_after_ms <= 800


def test_refs_are_unique_within_a_plan():
    plan = build_offline_plan(n=1_000, seed=0)
    refs = {v.ref for v in plan}
    assert len(refs) == len(plan)


def test_plan_is_deterministic_for_a_fixed_seed():
    first = build_offline_plan(n=1_000, seed=7)
    second = build_offline_plan(n=1_000, seed=7)
    assert first == second


def test_plan_differs_for_a_different_seed():
    first = build_offline_plan(n=1_000, seed=7)
    second = build_offline_plan(n=1_000, seed=8)
    assert first != second


def test_visit_field_order_and_class_field_name():
    plan = build_offline_plan(n=10, seed=0)
    visit = plan[0]
    # Positional construction must follow ref, cls, user_id, client_ip,
    # success, success_after_ms, envelopes -- the visit shape the console relies on.
    rebuilt = Visit(
        visit.ref,
        visit.cls,
        visit.user_id,
        visit.client_ip,
        visit.success,
        visit.success_after_ms,
    )
    assert rebuilt == visit
    assert rebuilt.envelopes is None
    assert hasattr(visit, "cls")
