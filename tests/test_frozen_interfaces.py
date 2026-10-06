"""The console-side names that parallel work builds on: the machine's events
and the saved-progress module's signatures."""

from __future__ import annotations

import dataclasses
import inspect
import typing

import pytest

from playground import statefile
from playground.machine import events
from playground.machine.events import (
    AppDown,
    AppReloaded,
    ChangeDetected,
    Effect,
    Result,
    RunInvalidated,
    SaveState,
    StateSaved,
)


def _field_names(cls) -> list[str]:
    return [f.name for f in dataclasses.fields(cls)]


def test_reload_causes_are_frozen():
    assert typing.get_args(events.ReloadCause) == ("change", "crash", "request")


@pytest.mark.parametrize(
    ("cls", "fields"),
    [
        (AppReloaded, ["boot", "cause"]),
        (AppDown, ["location", "failure"]),
        (ChangeDetected, []),
        (RunInvalidated, []),
        (StateSaved, ["ok"]),
        (SaveState, []),
    ],
)
def test_new_events_are_frozen_dataclasses_with_these_fields(cls, fields):
    assert dataclasses.is_dataclass(cls)
    assert cls.__dataclass_params__.frozen
    assert _field_names(cls) == fields
    assert cls.__name__ in events.__all__


def test_new_event_defaults():
    assert AppReloaded(2) == AppReloaded(boot=2, cause="change")
    assert AppDown().location is None
    assert AppDown().failure is None
    assert AppDown("app/routes.py:3").location == "app/routes.py:3"


def test_the_expect_kinds_are_frozen():
    assert typing.get_args(events.Expect) == ("start", "yes_no", "any", "continue")


@pytest.mark.parametrize(
    ("cls", "fields"),
    [
        (events.DeliverProgress, ["done", "total", "eta_s"]),
        (events.BootFailure, ["error", "message", "location", "source", "caret", "hint"]),
        (events.CopySnippets, ["task", "index"]),
        (events.Say, ["key", "params", "block"]),
    ],
)
def test_the_console_ux_names_are_frozen(cls, fields):
    assert dataclasses.is_dataclass(cls)
    assert cls.__dataclass_params__.frozen
    assert _field_names(cls) == fields
    assert cls.__name__ in events.__all__


def test_new_optional_fields_come_last_so_positional_calls_still_work():
    assert _field_names(events.RunProgress)[-1] == "eta_s"
    assert _field_names(events.RunFinished)[-1] == "elapsed_s"
    assert _field_names(events.StepDone)[-1] == "elapsed_s"
    assert events.RunFinished(10, 8, 2, 7, 3).elapsed_s is None
    assert events.StepDone(True).elapsed_s is None
    assert events.RunProgress(1, 2, 1, 0).eta_s is None


def test_a_says_block_comes_from_its_key_and_never_changes_equality():
    assert events.Say("ch3.lesson").block == "note"
    assert events.Say("ch3.lesson", {}, block="info").block == "info"
    assert events.Say("ch3.lesson") == events.Say("ch3.lesson", {}, block="ask")
    assert "Block" in events.__all__


def test_copy_snippets_is_an_effect():
    assert events.CopySnippets in typing.get_args(Effect)
    assert events.CopySnippets in events.EFFECTS
    assert events.DeliverProgress in typing.get_args(Result)


def test_new_results_and_the_new_effect_join_their_unions():
    results = set(typing.get_args(Result))
    assert {AppReloaded, AppDown, ChangeDetected, RunInvalidated, StateSaved} <= results
    assert SaveState in typing.get_args(Effect)
    assert SaveState not in results
    assert "ReloadCause" in events.__all__


def test_statefile_signatures_are_frozen():
    assert list(inspect.signature(statefile.load).parameters) == ["path"]
    save = inspect.signature(statefile.save)
    assert list(save.parameters) == ["path", "snapshot"]
    assert save.return_annotation in (bool, "bool")
