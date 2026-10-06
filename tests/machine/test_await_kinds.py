"""Which kind of answer each question waits for, and what Enter does there.

A "continue" wait goes on with Enter (an empty line) or ``yes``; a yes/no
question takes only ``yes`` or ``no``, and an empty line asks it again,
with no default. Every step has a kind, and picking saved progress up at a
step asks its question with that kind.

Scripted drivers read the question table (``_PROMPTS``, ``_PROMPTS_AGAIN``
and the top-up question) from ``steps.py`` itself, so its shape is checked
here as well.
"""

from __future__ import annotations

import ast
import inspect
import typing

import pytest

from playground.machine import steps
from playground.machine.events import (
    EFFECTS,
    AppReady,
    AwaitInput,
    Expect,
    ReadStage,
    Say,
    StepDone,
)
from playground.machine.machine import Machine
from playground.machine.steps import _KINDS, _PROMPTS, Step
from tests.machine._scenarios import (
    GARBAGE_INPUTS,
    RUN_FINISHED,
    assert_say_renders,
    close_machine,
    opening_machine,
    say,
    stopped_machine,
)
from tests.machine.test_online_steps import at_results

CONTINUE_STEPS = [step for step in Step if _KINDS[step] == "continue"]
YES_NO_STEPS = [step for step in Step if _KINDS[step] == "yes_no"]

# The shortfall a resumed top-up asks about.
SHORTFALL = 40


def machine_at(step: Step) -> tuple[Machine, list]:
    """A console that picked saved progress up at ``step``, and what it said doing so."""
    snapshot = {**at_results().to_dict(), "step": step.name, "topup": SHORTFALL}
    machine = Machine(saved=snapshot)
    machine.on_event(AppReady())
    return machine, machine.handle("/continue")


def question_of(step: Step) -> Say:
    if step is Step.TOPUP:
        return say("ch3.topup", shortfall=SHORTFALL)
    return say(_PROMPTS[step])


# -- the table -----------------------------------------------------------------------------------


def test_every_step_has_a_kind():
    assert set(_KINDS) == set(Step)
    assert set(_KINDS.values()) <= set(typing.get_args(Expect))


def test_the_kinds():
    assert set(YES_NO_STEPS) == {
        Step.CONSENT,
        Step.TOPUP,
        Step.RERUN_OFFER,
        Step.RERUN_ANYWAY,
        Step.KEEP,
    }
    assert {step for step in Step if _KINDS[step] == "any"} == {Step.RERUN_LATER, Step.DONE}


@pytest.mark.parametrize("step", list(Step), ids=lambda step: step.name)
def test_picking_a_step_up_asks_its_question_with_its_kind(step):
    machine, events = machine_at(step)
    assert_say_renders(events)
    if step is Step.RERUN_LATER:
        # It waits for /continue, which it has just been given: the re-run starts.
        assert events == [say("continue.back"), ReadStage()]
        return
    assert events[-2:] == [question_of(step), AwaitInput(_KINDS[step])]
    assert events[-2].block == "ask"
    assert machine.to_dict()["awaiting"] == _KINDS[step]


def test_chapter_1_waits_for_start_then_asks_a_yes_no_question():
    assert opening_machine().to_dict()["awaiting"] == "start"
    assert stopped_machine().to_dict()["awaiting"] == "start"
    machine = opening_machine()
    machine.handle("/start")
    events = machine.on_event(RUN_FINISHED)
    assert events[-1] == AwaitInput("yes_no")
    assert events[-2].key == "ch1.close" and events[-2].block == "ask"


# -- an empty line --------------------------------------------------------------------------------


@pytest.mark.parametrize("step", YES_NO_STEPS, ids=lambda step: step.name)
def test_an_empty_line_asks_a_yes_no_question_again(step):
    machine, _ = machine_at(step)
    before = machine.to_dict()
    for blank in ("", "   "):
        events = machine.handle(blank)
        assert events == [question_of(step), AwaitInput("yes_no")]
        assert events[0].block == "ask"
        assert not any(isinstance(event, EFFECTS) for event in events)
        assert machine.to_dict() == before


def test_an_empty_line_asks_the_chapter_1_close_again():
    machine = close_machine()
    before = machine.to_dict()
    events = machine.handle("")
    assert [event.key for event in events if isinstance(event, Say)] == ["ch1.close"]
    assert events[-1] == AwaitInput("yes_no")
    assert machine.to_dict() == before


def test_asking_again_keeps_the_hint_level():
    machine, _ = machine_at(Step.CONSENT)
    machine.handle("/hint")
    machine.handle("")
    assert machine.handle("/hint")[0] == say("hint.ch2.consent")


@pytest.mark.parametrize("step", CONTINUE_STEPS, ids=lambda step: step.name)
def test_enter_goes_on_at_a_continue_wait_exactly_as_yes_does(step):
    entered, _ = machine_at(step)
    typed, _ = machine_at(step)
    by_enter = entered.handle("")
    by_yes = typed.handle("yes")
    assert by_enter == by_yes
    assert isinstance(by_enter[-1], EFFECTS), by_enter
    assert entered.to_dict() == typed.to_dict()


@pytest.mark.parametrize("step", CONTINUE_STEPS, ids=lambda step: step.name)
def test_no_at_a_continue_wait_waits(step):
    machine, _ = machine_at(step)
    before = machine.to_dict()
    assert machine.handle("no") == [say("reply.no"), AwaitInput("continue")]
    assert machine.to_dict() == before


@pytest.mark.parametrize("step", [*CONTINUE_STEPS, *YES_NO_STEPS], ids=lambda step: step.name)
def test_anything_else_is_not_understood(step):
    machine, _ = machine_at(step)
    for garbage in GARBAGE_INPUTS:
        assert machine.handle(garbage) == [say("reply.unknown"), AwaitInput(_KINDS[step])]


def test_yes_and_no_are_case_insensitive_at_a_continue_wait():
    for answer in ("YES", "Yes"):
        machine, _ = machine_at(Step.CODE3)
        assert isinstance(machine.handle(answer)[-1], EFFECTS)
    machine, _ = machine_at(Step.CODE3)
    assert machine.handle("No") == [say("reply.no"), AwaitInput("continue")]


def test_a_free_step_takes_neither_enter_nor_yes():
    machine, _ = machine_at(Step.DONE)
    for answer in ("", "yes", "no"):
        assert machine.handle(answer) == [say("reply.unknown"), AwaitInput("any")]


def test_after_an_outcome_the_question_comes_back_with_its_kind():
    machine, _ = machine_at(Step.CODE3)
    machine.handle("")
    events = machine.on_event(StepDone(False, (say("verify.line", line="x"),)))
    assert events[-2:] == [say("ch3.again"), AwaitInput("continue")]


# -- the question table, as scripted drivers read it ---------------------------------------------


def _module_level_dict(tree: ast.Module, name: str) -> ast.Dict:
    for node in tree.body:
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            target, value = node.target.id, node.value
        elif (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
        ):
            target, value = node.targets[0].id, node.value
        else:
            continue
        if target == name:
            assert isinstance(value, ast.Dict), f"{name} must stay a literal dict"
            return value
    raise AssertionError(f"{name} is not assigned at module level")


def test_scripted_drivers_read_the_question_table_from_this_file():
    """Scripted drivers read the question table from ``steps.py`` with ``ast``:
    ``_PROMPTS`` and ``_PROMPTS_AGAIN`` stay module-level literal dicts of
    ``Step.X`` -> copy key, and the top-up question stays a literal."""
    source = inspect.getsource(steps)
    tree = ast.parse(source)
    read: dict[str, dict[str, str]] = {}
    for name in ("_PROMPTS", "_PROMPTS_AGAIN"):
        table = _module_level_dict(tree, name)
        read[name] = {}
        for key, value in zip(table.keys, table.values, strict=True):
            assert isinstance(key, ast.Attribute), name
            assert isinstance(key.value, ast.Name) and key.value.id == "Step", name
            assert isinstance(value, ast.Constant) and isinstance(value.value, str), name
            read[name][key.attr] = value.value
    assert '"ch3.topup"' in source
    assert read["_PROMPTS"] == {step.name: key for step, key in steps._PROMPTS.items()}
    assert read["_PROMPTS_AGAIN"] == {step.name: key for step, key in steps._PROMPTS_AGAIN.items()}
    assert set(read["_PROMPTS"]) | {"TOPUP"} == {step.name for step in Step}
