"""Code copied from the console's "Your turn" blocks, pasted as told, passes the gates.

Each task is rendered by the console's renderer on a recorded terminal, with
no colour and wide enough that no code line can wrap. Each edit's code is
then read back from the rendered text alone: the lines after its location
line and one blank line, up to the next blank line. Pasted into a copy of
``app/routes.py`` as it ships, at the spot each location line names, it must
pass the chapter 3 gate, and with the chapter 5 code on top, the chapter 5
gate and the rehearsal. The chapter 3 result is also byte for byte the
reference solution.
"""

from __future__ import annotations

import ast
import io
import os
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest
from rich.console import Console

from playground.copy.en import TASKS, text
from playground.machine.events import Say
from playground.tui.console import InteractiveRenderer
from playground.verify import Verdict, gate_lines
from tests.verify.harness import ROOT, code_gate, rehearsal

SHIPPED = ROOT / "app" / "routes.py"
SOLUTIONS = ROOT / "solutions"
# Wide enough for the longest code line, so the renderer never wraps one.
WIDTH = 120


def _rendered_lines(task: str) -> list[str]:
    buffer = io.StringIO()
    console = Console(record=True, file=buffer, width=WIDTH, color_system=None, force_terminal=True)
    InteractiveRenderer(console, "none").show(Say(task))
    output = buffer.getvalue()
    assert "\x1b[" not in output
    return output.split("\n")


def _code_blocks(task: str) -> list[list[str]]:
    """Each edit's code, read from the rendered task as a reader would copy it."""
    lines = _rendered_lines(task)
    _title, edits, _footer = TASKS[task]
    blocks: list[list[str]] = []
    for location, _snippet in edits:
        where = text(location)
        found = [i for i, line in enumerate(lines) if line.strip() == where]
        assert len(found) == 1, (where, lines)
        start = found[0] + 1
        assert lines[start].strip() == "", lines[start:]
        code: list[str] = []
        for line in lines[start + 1 :]:
            if not line.strip():
                break
            code.append(line)
        assert code, where
        blocks.append(code)
    return blocks


def _one(lines: list[str], wanted: Callable[[str], bool]) -> int:
    found = [i for i, line in enumerate(lines) if wanted(line)]
    assert len(found) == 1, found
    return found[0]


def _last_import(lines: list[str]) -> int:
    tree = ast.parse("\n".join(lines))
    imports = [n for n in tree.body if isinstance(n, (ast.Import, ast.ImportFrom))]
    end = max(n.end_lineno or n.lineno for n in imports)
    return end - 1


def _insert_after(lines: list[str], at: int, code: list[str]) -> list[str]:
    return [*lines[: at + 1], *code, *lines[at + 1 :]]


def paste_chapter_3(source: str) -> str:
    imports, action_log, success_log = _code_blocks("ch3.task")
    lines = source.split("\n")
    lines = _insert_after(lines, _last_import(lines), imports)
    at = _one(lines, lambda line: line == "    perform_action(body.user)")
    lines = _insert_after(lines, at, action_log)
    at = _one(lines, lambda line: line.strip().startswith("raise HTTPException(status_code=400,"))
    lines = _insert_after(lines, at, success_log)
    return "\n".join(lines)


def paste_chapter_5(source: str) -> str:
    imports, gate = _code_blocks("ch5.task")
    lines = source.split("\n")
    lines = _insert_after(lines, _last_import(lines), imports)
    at = _one(lines, lambda line: line.startswith("def action("))
    lines = _insert_after(lines, at, gate)
    return "\n".join(lines)


def _apply(tree: Path, name: str) -> None:
    # Outside any repository, git apply works like patch; the ceiling keeps it
    # from finding one above the temp directory.
    env = {**os.environ, "GIT_CEILING_DIRECTORIES": str(tree.parent)}
    subprocess.run(
        ["git", "apply", str(SOLUTIONS / name)],
        cwd=tree,
        env=env,
        check=True,
        capture_output=True,
    )


@pytest.fixture
def routes(tmp_path: Path) -> Path:
    path = tmp_path / "pasted" / "app" / "routes.py"
    path.parent.mkdir(parents=True)
    path.write_text(paste_chapter_3(SHIPPED.read_text(encoding="utf-8")), encoding="utf-8")
    return path


def test_each_edit_reads_back_as_its_snippet():
    for task, (_title, edits, _footer) in TASKS.items():
        for code, (_location, snippet) in zip(_code_blocks(task), edits):
            assert "\n".join(code) == text(snippet), task


def test_pasted_chapter_3_code_is_the_reference_solution(routes, tmp_path):
    if shutil.which("git") is None:
        pytest.skip("git is needed to apply the reference diff")
    tree = tmp_path / "reference"
    (tree / "app").mkdir(parents=True)
    shutil.copy(SHIPPED, tree / "app" / "routes.py")
    _apply(tree, "chapter3.diff")
    assert routes.read_bytes() == (tree / "app" / "routes.py").read_bytes()


def test_pasted_chapter_3_code_passes_the_chapter_3_gate(routes, fake):
    result = code_gate(routes, fake, 3)
    assert result.verdict is Verdict.PASS, gate_lines(result)
    assert result.findings == ()


def test_pasted_chapter_5_code_passes_the_code_gate_and_the_rehearsal(routes, fake):
    routes.write_text(paste_chapter_5(routes.read_text(encoding="utf-8")), encoding="utf-8")
    code = code_gate(routes, fake, 5)
    assert code.verdict is Verdict.PASS, gate_lines(code)
    rehearsed = rehearsal(routes)
    assert rehearsed.verdict is Verdict.PASS, gate_lines(rehearsed)
    assert code.findings == rehearsed.findings == ()
