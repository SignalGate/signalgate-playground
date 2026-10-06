"""The "Your turn" tasks: their plain text, what ``/copy`` copies, and that
their code is exactly the reference solution's."""

from __future__ import annotations

from pathlib import Path

import pytest

from playground.copy.en import TASKS, TEXT, segments, snippet_text, task_plain, text

REPO_ROOT = Path(__file__).resolve().parents[2]
SOLUTIONS = REPO_ROOT / "solutions"


def _added_lines(diff: Path) -> list[str]:
    """The lines a unified diff adds, without their ``+``."""
    return [
        line[1:]
        for line in diff.read_text(encoding="utf-8").splitlines()
        if line.startswith("+") and not line.startswith("+++")
    ]


def _snippet_lines(task: str) -> list[str]:
    _title, edits, _footer = TASKS[task]
    return [line for _location, snippet in edits for line in TEXT[snippet].split("\n")]


@pytest.mark.parametrize(
    ("task", "diff"), [("ch3.task", "chapter3.diff"), ("ch5.task", "chapter5.diff")]
)
def test_the_snippets_are_the_reference_solutions_lines(task, diff):
    assert _snippet_lines(task) == _added_lines(SOLUTIONS / diff)


def test_the_chapter_3_task_has_the_import_and_both_logs():
    _title, edits, _footer = TASKS["ch3.task"]
    assert [snippet for _location, snippet in edits] == [
        "snippet.ch3.import",
        "snippet.ch3.action_log",
        "snippet.ch3.success_log",
    ]
    assert "body.signalgate_log" in TEXT["snippet.ch3.action_log"]
    assert '"playground.action"' in TEXT["snippet.ch3.action_log"]
    assert "body.signalgate," in TEXT["snippet.ch3.success_log"]
    assert '"playground.success"' in TEXT["snippet.ch3.success_log"]


@pytest.mark.parametrize("task", list(TASKS))
def test_snippets_end_without_a_newline_and_body_code_is_indented_four(task):
    _title, edits, _footer = TASKS[task]
    for number, (location, snippet) in enumerate(edits, start=1):
        code = TEXT[snippet]
        assert not code.endswith("\n")
        assert code == code.strip("\n")
        for line in code.split("\n"):
            assert line == line.rstrip(), (snippet, line)
        if number > 1:  # the function-body edits
            assert code.startswith("    ") and not code.startswith("     ")
        spans = segments(location)
        assert spans[0][1] is True
        assert spans[0][0].startswith(f"{number}. ")


@pytest.mark.parametrize("task", list(TASKS))
def test_the_plain_task_is_title_edits_and_footer(task):
    title, edits, footer = TASKS[task]
    expected = [text(title)]
    for location, snippet in edits:
        expected += [text(location), "", *TEXT[snippet].split("\n"), ""]
    expected.append(text(footer))
    assert text(task) == "\n".join(expected)
    assert task_plain(task) == text(task)
    assert "**" not in text(task)
    assert "".join(piece for piece, _bold in segments(task)) == text(task)


@pytest.mark.parametrize("task", list(TASKS))
def test_every_code_line_is_a_whole_line_of_the_plain_task(task):
    lines = text(task).split("\n")
    at = 0
    for line in _snippet_lines(task):
        at = lines.index(line, at) + 1  # in order, byte for byte


def test_the_plain_chapter_3_task_reads_as_the_block():
    assert text("ch3.task").split("\n")[:4] == [
        "Your turn · edit app/routes.py",
        "1. At the top of app/routes.py, with the other imports:",
        "",
        "from app.signalgate_client import sg, sg_event",
    ]
    assert text("ch3.task").endswith("\n\n/copy copies all three · /hint if you get stuck")


def test_copy_takes_every_snippet_under_a_comment_naming_its_place():
    copied = snippet_text("ch3.task")
    parts = copied.split("\n\n")
    assert len(parts) == 3
    _title, edits, _footer = TASKS["ch3.task"]
    for part, (location, snippet) in zip(parts, edits):
        assert part == f"# {text(location)}\n{TEXT[snippet]}"
    assert [line for line in copied.split("\n") if line.startswith("# ")] == [
        f"# {text(location)}" for location, _snippet in edits
    ]
    assert "**" not in copied


def test_copy_n_takes_that_snippet_alone():
    assert snippet_text("ch3.task", 2) == TEXT["snippet.ch3.action_log"]
    assert snippet_text("ch3.task", 2) == (
        '    ev = sg_event(request, body.user, body.signalgate_log, "playground.action")\n'
        "    if ev:\n"
        "        sg().log(ev)"
    )
    assert snippet_text("ch5.task", 1) == "from signalgate import SignalGateError"


@pytest.mark.parametrize("index", [0, -1, 4, 99])
def test_copy_of_a_snippet_the_task_doesnt_have_raises_index_error(index):
    with pytest.raises(IndexError):
        snippet_text("ch3.task", index)


def test_the_chapter_5_gate_has_its_long_lines_unbroken():
    gate = TEXT["snippet.ch5.gate"].split("\n")
    assert len(gate) == 7
    assert max(len(line) for line in gate) > 90
    assert gate[4].endswith('detail="Request could not be completed")')
