"""``/copy`` and ``/copy N``: the current chapter's code, for the clipboard.

In chapters 3 and 5 ``/copy`` asks the runner to copy every snippet of the
chapter's "Your turn" block, and ``/copy N`` only snippet N; the question
on screen stays open. Elsewhere there is nothing to copy. While a run is
going it waits, like the other commands that aren't always available.
"""

from __future__ import annotations

import pytest

from playground.copy.en import TASKS
from playground.machine.events import (
    COMMANDS,
    AppReady,
    AwaitInput,
    CopySnippets,
    RunGate,
    StepDone,
)
from playground.machine.machine import Machine
from playground.machine.steps import _KINDS, CHAPTER_OF, Step
from tests.machine._scenarios import (
    boot_machine,
    close_machine,
    opening_machine,
    running_machine,
    say,
)
from tests.machine.test_online_steps import at_code3, at_results


def machine_at(step: Step) -> Machine:
    machine = Machine(saved={**at_results().to_dict(), "step": step.name, "topup": 40})
    machine.on_event(AppReady())
    machine.handle("/continue")
    return machine


def test_copy_is_a_command_the_console_completes():
    assert COMMANDS[-1] == "/copy"


@pytest.mark.parametrize(
    "step", [step for step in Step if CHAPTER_OF[step] == 3], ids=lambda step: step.name
)
def test_copy_in_chapter_3_copies_its_three_snippets(step):
    machine = machine_at(step)
    before = machine.to_dict()
    assert machine.handle("/copy") == [CopySnippets("ch3.task", None)]
    assert machine.to_dict() == before
    # What the runner found comes back, and the same question stays open.
    events = machine.on_event(StepDone(True, (say("copy.done", count=3),)))
    assert events == [say("copy.done", count=3), AwaitInput(_KINDS[step])]
    assert machine.to_dict() == before


def test_copy_n_copies_one_snippet():
    machine = machine_at(Step.CODE3)
    assert machine.handle("/copy 2") == [CopySnippets("ch3.task", 2)]
    assert machine.on_event(StepDone(True, (say("copy.done_one", index=2),))) == [
        say("copy.done_one", index=2),
        AwaitInput("continue"),
    ]
    assert machine_at(Step.CODE3).handle("/copy   3") == [CopySnippets("ch3.task", 3)]
    assert machine_at(Step.CODE3).handle("  /copy 1  ") == [CopySnippets("ch3.task", 1)]


def test_copy_in_chapter_5_copies_its_two_snippets():
    assert len(TASKS["ch5.task"][1]) == 2
    assert machine_at(Step.CODE5).handle("/copy") == [CopySnippets("ch5.task", None)]
    assert machine_at(Step.READINESS).handle("/copy 1") == [CopySnippets("ch5.task", 1)]
    assert machine_at(Step.CODE5).handle("/copy 3") == [
        say("copy.bad_index", count=2),
        AwaitInput("continue"),
    ]


@pytest.mark.parametrize("index", ["9", "0", "-1", "two", "1.5", "²", "1 2", "+1"])
def test_copy_with_a_number_it_has_no_snippet_for_says_which_there_are(index):
    machine = machine_at(Step.CODE3)
    before = machine.to_dict()
    assert machine.handle(f"/copy {index}") == [
        say("copy.bad_index", count=3),
        AwaitInput("continue"),
    ]
    assert machine.to_dict() == before


@pytest.mark.parametrize("digits", [4_400, 50_000])
def test_copy_with_a_number_too_long_to_read_says_which_there_are(digits):
    machine = machine_at(Step.CODE3)
    before = machine.to_dict()
    assert machine.handle("/copy " + "9" * digits) == [
        say("copy.bad_index", count=3),
        AwaitInput("continue"),
    ]
    assert machine.to_dict() == before


def test_copy_with_leading_zeros_still_names_its_snippet():
    assert machine_at(Step.CODE3).handle("/copy 03") == [CopySnippets("ch3.task", 3)]


@pytest.mark.parametrize(
    "step",
    [step for step in Step if CHAPTER_OF[step] not in (3, 5) and step is not Step.RERUN_LATER],
    ids=lambda step: step.name,
)
def test_copy_where_there_is_no_code_says_so(step):
    machine = machine_at(step)
    assert machine.handle("/copy") == [say("copy.none"), AwaitInput(_KINDS[step])]
    assert machine.handle("/copy 2") == [say("copy.none"), AwaitInput(_KINDS[step])]


def test_copy_while_the_re_run_waits_has_nothing_to_copy():
    machine = at_results()
    machine.handle("yes")
    machine.on_event(StepDone(True, stage=1))
    assert machine.handle("yes") == [say("ch6.rerun.later"), AwaitInput("any")]
    assert machine.handle("/copy") == [say("copy.none"), AwaitInput("any")]


def test_copy_in_chapter_1_has_nothing_to_copy():
    assert boot_machine().handle("/copy") == [say("copy.none")]
    assert opening_machine().handle("/copy") == [say("copy.none"), AwaitInput("start")]
    assert close_machine().handle("/copy 1") == [say("copy.none"), AwaitInput("yes_no")]


def test_copy_waits_while_a_run_is_going():
    assert running_machine().handle("/copy") == [say("reply.busy")]
    machine = at_code3()
    assert machine.handle("yes") == [RunGate(3)]
    assert machine.handle("/copy") == [say("reply.busy")]
    assert machine.handle("/copy 2") == [say("reply.busy")]


def test_only_the_command_itself_copies():
    machine = machine_at(Step.CODE3)
    for typed in ("/copy2", "/COPY", "/copying"):
        assert machine.handle(typed) == [say("reply.unknown"), AwaitInput("continue")]
