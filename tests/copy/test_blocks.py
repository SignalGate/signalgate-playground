"""Which block each copy entry belongs to (``playground.copy.blocks``)."""

from __future__ import annotations

import subprocess
import sys
import typing
from pathlib import Path

import pytest

from playground.copy.blocks import Block, block_of
from playground.copy.en import TASKS, TEXT
from playground.machine import steps
from playground.machine.events import Say

REPO_ROOT = Path(__file__).resolve().parents[2]
BLOCKS = set(typing.get_args(Block))


def test_the_block_names():
    assert BLOCKS == {
        "heading",
        "info",
        "task",
        "code",
        "result",
        "achievement",
        "note",
        "ask",
        "card",
    }


def test_every_copy_key_and_task_has_a_block():
    for key in [*TEXT, *TASKS]:
        assert block_of(key) in BLOCKS, key


def test_every_question_the_machine_asks_is_an_ask():
    questions = {*steps._PROMPTS.values(), *steps._PROMPTS_AGAIN.values(), "ch3.topup"}
    for key in questions:
        assert block_of(key) == "ask", key
    for key in ("ch1.close", "ch2.consent", "run.again", "continue.offer", "ch1.stopped"):
        assert block_of(key) == "ask", key


@pytest.mark.parametrize(
    ("key", "block"),
    [
        ("chapter.heading", "heading"),
        ("ch3.task", "task"),
        ("ch5.task", "task"),
        ("app.down.code", "code"),
        ("achievement.ch1", "achievement"),
        ("achievement.results", "achievement"),
        ("complete.card", "card"),
        ("complete.card.untimed", "card"),
        ("ch2.session.retry", "ask"),
        ("wf.paused", "ask"),
        ("ch2.session.ok", "result"),
        ("ch2.key.ok", "result"),
        ("ch2.git.env_tracked", "result"),
        ("ch2.clock.warn", "result"),
        ("wf.draft", "result"),
        ("results.frame", "result"),
        ("stage.partial", "result"),
        ("online.unreachable", "result"),
        ("app.down", "result"),
        ("app.down_at", "result"),
        ("app.down.error", "result"),
        ("drain.done", "result"),
        ("drain.skipped", "result"),
        ("copy.done", "result"),
        ("report.written", "result"),
        ("ch5.readiness.live", "result"),
        ("ch3.lesson", "note"),
        ("ch5.lesson", "note"),
        ("app.reloaded", "note"),
        ("app.restarted_key", "note"),
        ("app.down.hint.indent", "note"),
        ("app.down.fix", "note"),
        ("drain.note", "note"),
        ("drain.note_quit", "note"),
        ("reply.no", "note"),
        ("state.set_aside", "note"),
        ("console.saved", "note"),
        ("ch1.intro", "info"),
        ("ch3.intro", "info"),
        ("ch5.readiness.intro", "info"),
        ("drain.waiting", "info"),
        ("status.chapter", "info"),
        ("opening", "info"),
        ("help", "info"),
    ],
)
def test_block_of(key, block):
    assert block_of(key) == block


def test_a_say_takes_its_block_from_its_key_unless_given_one():
    assert Say("ch3.lesson").block == "note"
    assert Say("ch3.lesson", {}, block="ask").block == "ask"
    for key in TEXT:
        assert Say(key).block == block_of(key)


def test_the_block_table_imports_nothing_from_the_machine():
    code = (
        "import sys\n"
        "import playground.copy.blocks\n"
        "print(sorted(name for name in sys.modules if name.startswith('playground.machine')))\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], cwd=REPO_ROOT, capture_output=True, text=True, check=True
    )
    assert result.stdout.strip() == "[]"
