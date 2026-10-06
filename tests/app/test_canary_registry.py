"""Tests for the canary registry: all-or-nothing registration and lookups."""

from __future__ import annotations

import dataclasses
import threading

import pytest

from app.kit import canary as canary_module
from app.kit.canary import ANSWERS, Canary, CanaryRegistry, canaries
from app.kit.local_constants import CANARY_DELAY_MAX_MS, CANARY_NONCE_MAX_CHARS


def _item(nonce: str = "nonce-a", answer: str = "block", **extra) -> dict:
    return {"nonce": nonce, "answer": answer, **extra}


def test_the_answers_are_the_public_verdicts_and_a_rejection():
    assert ANSWERS == ("block", "dry_run_block", "admin_alert", "422")


def test_the_module_keeps_one_shared_registry():
    assert isinstance(canaries, CanaryRegistry)


@pytest.mark.parametrize("answer", ANSWERS)
def test_every_answer_registers_and_is_found(answer):
    registry = CanaryRegistry()
    assert registry.register([_item(answer=answer, delay_ms=0)]) == 1
    assert registry.lookup("nonce-a") == Canary(answer=answer, delay_ms=0)


def test_delay_defaults_to_zero():
    registry = CanaryRegistry()
    registry.register([_item()])
    assert registry.lookup("nonce-a") == Canary("block", 0)


@pytest.mark.parametrize("delay_ms", [0, CANARY_DELAY_MAX_MS // 2, CANARY_DELAY_MAX_MS])
def test_a_delay_within_the_limit_is_kept(delay_ms):
    registry = CanaryRegistry()
    registry.register([_item(delay_ms=delay_ms)])
    assert registry.lookup("nonce-a").delay_ms == delay_ms


def test_register_counts_every_item():
    registry = CanaryRegistry()
    items = [_item("nonce-a"), _item("nonce-b", "422"), _item("nonce-c", "admin_alert")]
    assert registry.register(items) == 3
    assert len(registry) == 3


def test_register_accepts_any_iterable_of_items():
    registry = CanaryRegistry()
    assert registry.register(item for item in (_item("nonce-a"), _item("nonce-b"))) == 2


@pytest.mark.parametrize(
    "item",
    [
        _item(answer="allow"),
        _item(answer="BLOCK"),
        {"nonce": "nonce-a", "answer": 422},
        _item(delay_ms=-1),
        _item(delay_ms=CANARY_DELAY_MAX_MS + 1),
        _item(delay_ms=True),
        _item(delay_ms=float(0)),
        _item(delay_ms="0"),
        _item(delay_ms=None),
        _item(nonce=""),
        _item(nonce="n" * (CANARY_NONCE_MAX_CHARS + 1)),
        {"nonce": 7, "answer": "block"},
        {"nonce": None, "answer": "block"},
        {"answer": "block"},
        {"nonce": "nonce-a"},
        _item(extra="not allowed"),
        "nonce-a",
        ["nonce-a", "block"],
        None,
    ],
    ids=[
        "allow-is-not-a-canary-answer",
        "answer-case",
        "answer-not-a-string",
        "negative-delay",
        "delay-over-the-limit",
        "bool-delay",
        "float-delay",
        "string-delay",
        "null-delay",
        "empty-nonce",
        "long-nonce",
        "int-nonce",
        "null-nonce",
        "missing-nonce",
        "missing-answer",
        "extra-key",
        "string-item",
        "list-item",
        "null-item",
    ],
)
def test_an_invalid_item_raises_and_registers_nothing(item):
    registry = CanaryRegistry()
    registry.register([_item("nonce-kept", "admin_alert")])
    with pytest.raises(ValueError):
        registry.register([_item("nonce-valid"), item])
    assert registry.lookup("nonce-valid") is None
    assert registry.lookup("nonce-kept") == Canary("admin_alert", 0)
    assert len(registry) == 1


def test_the_longest_nonce_is_accepted():
    registry = CanaryRegistry()
    nonce = "n" * CANARY_NONCE_MAX_CHARS
    registry.register([_item(nonce)])
    assert registry.lookup(nonce) is not None


@pytest.mark.parametrize("items", [[], {}, "nonce-a", b"nonce-a", _item(), None, 3])
def test_items_must_be_a_non_empty_list(items):
    with pytest.raises(ValueError):
        CanaryRegistry().register(items)


def test_too_many_items_are_refused(monkeypatch):
    monkeypatch.setattr(canary_module, "PAGE_MAX", 2)
    registry = CanaryRegistry()
    with pytest.raises(ValueError):
        registry.register([_item("nonce-a"), _item("nonce-b"), _item("nonce-c")])
    assert len(registry) == 0
    assert registry.register([_item("nonce-a"), _item("nonce-b")]) == 2


def test_the_delay_limit_is_read_at_call_time(monkeypatch):
    smaller = CANARY_DELAY_MAX_MS // 2
    monkeypatch.setattr(canary_module, "CANARY_DELAY_MAX_MS", smaller)
    registry = CanaryRegistry()
    with pytest.raises(ValueError):
        registry.register([_item(delay_ms=smaller + 1)])
    assert registry.register([_item(delay_ms=smaller)]) == 1


def test_registering_a_nonce_again_replaces_its_answer():
    registry = CanaryRegistry()
    registry.register([_item(answer="block")])
    registry.register([_item(answer="422", delay_ms=CANARY_DELAY_MAX_MS)])
    assert registry.lookup("nonce-a") == Canary("422", CANARY_DELAY_MAX_MS)
    assert len(registry) == 1


def test_a_nonce_listed_twice_keeps_its_last_answer():
    registry = CanaryRegistry()
    assert registry.register([_item(answer="block"), _item(answer="dry_run_block")]) == 2
    assert registry.lookup("nonce-a") == Canary("dry_run_block", 0)


def test_a_registration_answers_any_number_of_times():
    registry = CanaryRegistry()
    registry.register([_item()])
    assert registry.lookup("nonce-a") == registry.lookup("nonce-a") == Canary("block", 0)


@pytest.mark.parametrize("nonce", ["nonce-unknown", "", None, 7, b"nonce-a"])
def test_lookup_of_anything_unregistered_is_none(nonce):
    registry = CanaryRegistry()
    registry.register([_item()])
    assert registry.lookup(nonce) is None


def test_clear_forgets_every_registration():
    registry = CanaryRegistry()
    registry.register([_item("nonce-a"), _item("nonce-b")])
    registry.clear()
    assert len(registry) == 0
    assert registry.lookup("nonce-a") is None


def test_a_canary_is_frozen():
    with pytest.raises(dataclasses.FrozenInstanceError):
        Canary("block", 0).answer = "422"  # type: ignore[misc]


def test_concurrent_registrations_are_all_kept():
    registry = CanaryRegistry()
    workers, each = 4, 50

    def register_many(worker: int) -> None:
        for n in range(each):
            registry.register([_item(f"nonce-{worker}-{n}")])

    threads = [threading.Thread(target=register_many, args=(w,)) for w in range(workers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(registry) == workers * each
