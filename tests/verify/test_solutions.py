"""The reference diffs in solutions/ apply to the untouched app/routes.py and pass their gates.

Chapter 3's diff goes on the file as it ships; chapter 5's goes on top of
chapter 3's. Each result is checked by the real gates, in a temp copy.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from playground.verify import Verdict, gate_lines
from tests.verify.harness import ROOT, code_gate, rehearsal

SOLUTIONS = ROOT / "solutions"


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
def tree(tmp_path: Path) -> Path:
    if shutil.which("git") is None:
        pytest.skip("git is needed to apply the reference diffs")
    root = tmp_path / "tree"
    (root / "app").mkdir(parents=True)
    shutil.copy(ROOT / "app" / "routes.py", root / "app" / "routes.py")
    return root


def test_the_solutions_are_exactly_the_two_chapter_diffs():
    assert sorted(path.name for path in SOLUTIONS.iterdir()) == ["chapter3.diff", "chapter5.diff"]


def test_the_chapter_3_solution_passes_the_chapter_3_code_gate(tree, fake):
    _apply(tree, "chapter3.diff")
    routes = tree / "app" / "routes.py"
    result = code_gate(routes, fake, 3)
    assert result.verdict is Verdict.PASS, gate_lines(result)
    assert result.findings == ()


def test_the_chapter_5_solution_passes_the_code_gate_and_the_rehearsal(tree, fake):
    _apply(tree, "chapter3.diff")
    _apply(tree, "chapter5.diff")
    routes = tree / "app" / "routes.py"
    code = code_gate(routes, fake, 5)
    assert code.verdict is Verdict.PASS, gate_lines(code)
    rehearsed = rehearsal(routes)
    assert rehearsed.verdict is Verdict.PASS, gate_lines(rehearsed)
    assert code.findings == rehearsed.findings == ()


def test_the_chapter_3_solution_is_not_enough_for_chapter_5(tree):
    _apply(tree, "chapter3.diff")
    result = rehearsal(tree / "app" / "routes.py")
    assert result.verdict is Verdict.FAIL
