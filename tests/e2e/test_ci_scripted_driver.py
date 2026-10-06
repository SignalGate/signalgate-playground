"""A scripted driver takes the whole tutorial in ``--ci``, as scripts do, against the test-only fake.

Scripted drivers read the question table from ``playground/machine/steps.py``
(the literal ``_PROMPTS`` and ``_PROMPTS_AGAIN`` dicts, and the top-up
question), and take the step of each ``await`` from the last question the
console said. This one does the same, without ``--yes``: it answers
``/start`` at the opening and ``yes`` at every other question, except ``no``
to the re-run offer, ``/report`` and then ``yes`` at the keep question, and
``/quit`` at the end. At each code step it applies the reference solution
with ``git apply`` and waits for the console to say the change is running
before it answers; a question arriving meanwhile fails the run, as it would
for any such driver.

The fake's script plays the dashboard, as in ``tests/e2e/test_online.py``,
and protection turns on (stage 2 of 3) once chapter 5 opens.
"""

from __future__ import annotations

import ast
import collections
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from playground.machine.steps import _ACHIEVEMENTS
from playground.traffic.local_constants import (
    BACKLOG_FLOOR,
    LOGS_PER_VISIT_MAX,
    ONLINE_CONCURRENCY,
    PACER_POLL_S,
)
from playground.verify.workflow import G1_MIN_ACTIONS
from tests.e2e.conftest import EXIT_WITHIN_S, REPO_ROOT, apps_left_running
from tests.e2e.fake_host import E2E_KEY, serve_fake
from tests.e2e.seam import Seam
from tests.e2e.test_online import DASHBOARD, PROTECTING
from tests.fake_server import FakeServer, Script

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(sys.platform == "win32", reason="POSIX subprocess handling"),
]

SIZE = 1100
# The whole session's budget: as for the --yes run, plus two reloads.
SESSION_WITHIN_S = 300.0
# The longest one answer may take to bring the next line.
LINE_WITHIN_S = 120.0

# Chapter 1 has no step of its own: its questions come from the machine.
CHAPTER_1_PROMPTS = {"opening": "OPENING", "ch1.close": "CH1_CLOSE"}
KINDS = {"start", "yes_no", "continue", "any"}
# A window at least this long between two run progress records measures
# the app's delivery rate.
RATE_WINDOW_S = 2.0


def read_prompts(steps_py: Path) -> dict[str, str]:
    """Question copy key -> step name, read from ``steps.py`` without importing it."""
    source = steps_py.read_text(encoding="utf-8")
    tree = ast.parse(source)
    steps: set[str] = set()
    prompts: dict[str, str] = {}
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == "Step":
            for item in node.body:
                if isinstance(item, ast.Assign):
                    steps.update(t.id for t in item.targets if isinstance(t, ast.Name))
        target = value = None
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            target, value = node.target.id, node.value
        elif (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
        ):
            target, value = node.targets[0].id, node.value
        if target not in ("_PROMPTS", "_PROMPTS_AGAIN") or not isinstance(value, ast.Dict):
            continue
        for key, item in zip(value.keys, value.values, strict=True):
            assert isinstance(key, ast.Attribute) and isinstance(key.value, ast.Name)
            assert key.value.id == "Step"
            assert isinstance(item, ast.Constant) and isinstance(item.value, str)
            prompts[item.value] = key.attr
    assert '"ch3.topup"' in source
    prompts["ch3.topup"] = "TOPUP"
    assert steps and prompts and set(prompts.values()) <= steps
    return prompts


class Driver:
    """Answers each ``await`` from the step whose question came last."""

    def __init__(self, console: Any, tree: Path, script: Script) -> None:
        self.console = console
        self.tree = tree
        self.script = script
        self.prompts = {**CHAPTER_1_PROMPTS, **read_prompts(tree / "playground/machine/steps.py")}
        self.step: str | None = None
        self.asks: collections.Counter[str] = collections.Counter()
        self.answers: list[tuple[str | None, str, str]] = []  # (step, expect, answer)
        self.done: set[str] = set()
        self.arrived: list[tuple[float, dict[str, Any]]] = []
        self.deadline = time.monotonic() + SESSION_WITHIN_S

    def next(self) -> dict[str, Any] | None:
        within = min(LINE_WITHIN_S, max(0.0, self.deadline - time.monotonic()))
        line = self.console.next_line(within)
        if line is not None:
            self.arrived.append((time.monotonic(), line))
            self.on_line(line)
        return line

    def on_line(self, line: dict[str, Any]) -> None:
        if line.get("type") != "say":
            return
        key = line["key"]
        if key == "chapter.heading" and line["text"].split()[1] == "5":
            self.script.set_workflow(PROTECTING)
        if key in self.prompts:
            self.step = self.prompts[key]

    def answer(self, expect: str, reply: str) -> None:
        self.answers.append((self.step, expect, reply))
        self.console.send(reply)

    def apply_solution(self, chapter: int) -> None:
        """Edit ``app/routes.py`` with the reference solution, then wait for the notice."""
        env = {**os.environ, "GIT_CEILING_DIRECTORIES": str(self.tree.parent)}
        subprocess.run(
            ["git", "apply", f"solutions/chapter{chapter}.diff"],
            cwd=self.tree,
            env=env,
            check=True,
            capture_output=True,
        )
        while True:
            line = self.next()
            assert line is not None, "the console closed while a change was loading"
            assert line.get("type") != "await", "a question came before the change was running"
            if line.get("type") == "say" and line["key"] == "app.reloaded":
                return

    def on_await(self, expect: str) -> None:
        assert expect in KINDS, expect
        if expect == "start":
            assert "started" not in self.done, "/start asked for twice"
            self.done.add("started")
            self.step = "OPENING"
            self.answer(expect, "/start")
            return
        step = self.step
        assert step is not None, f"a {expect!r} question before any known step"
        self.asks[step] += 1
        if step in ("CODE3", "CODE5") and step not in self.done:
            self.done.add(step)
            self.apply_solution(3 if step == "CODE3" else 5)
        if step == "RERUN_OFFER":
            self.answer(expect, "no")
        elif step == "KEEP" and "report" not in self.done:
            self.done.add("report")
            self.answer(expect, "/report")
        elif step == "DONE":
            self.answer(expect, "/quit")
        else:
            self.answer(expect, "yes")

    def drive(self) -> int:
        while True:
            line = self.next()
            if line is None:
                break
            if line.get("type") == "await":
                self.on_await(str(line["expect"]))
        return self.console.wait_exit(EXIT_WITHIN_S)


def _runs(arrived: list[tuple[float, dict[str, Any]]], after: str, until: str) -> list:
    """The run progress records between the ``after`` say and the ``until`` say."""
    keys = [line.get("key") for _t, line in arrived]
    start, end = keys.index(after), keys.index(until)
    return [
        (t, line)
        for t, line in arrived[start:end]
        if line.get("type") == "progress" and "phase" not in line and "delivered" in line
    ]


def keys_at(lines: list[dict[str, Any]], key: str) -> int:
    """Where the first ``say`` of ``key`` is in ``lines``."""
    return next(at for at, line in enumerate(lines) if line.get("key") == key)


def _highest_rate(records: list[tuple[float, dict[str, Any]]]) -> float:
    """The fastest delivery measured over any window of at least ``RATE_WINDOW_S``."""
    best = 0.0
    for i, (t0, first) in enumerate(records):
        for t1, later in records[i + 1 :]:
            if t1 - t0 >= RATE_WINDOW_S:
                best = max(best, (later["delivered"] - first["delivered"]) / (t1 - t0))
    return best


def test_a_scripted_driver_answering_yes_completes_every_chapter(
    tracked_copy, tmp_path, open_ci_console
):
    script = Script(DASHBOARD, advance_on_poll=True, hold_until_actions=G1_MIN_ACTIONS)
    fake = FakeServer(script)
    (tracked_copy / "app" / ".env").write_text(f"SIGNALGATE_API_KEY={E2E_KEY}\n")
    # The copy has no repository of its own: git apply works on it as patch does.
    assert not (tracked_copy / ".git").exists()
    assert (tracked_copy / "playground/machine/steps.py").read_bytes() == (
        REPO_ROOT / "playground/machine/steps.py"
    ).read_bytes()

    with serve_fake(fake) as url:
        seam = Seam.create(tmp_path / "seam", fake_url=url)
        console = open_ci_console(seam.env(tracked_copy), "--size", str(SIZE))
        driver = Driver(console, tracked_copy, script)
        started = time.monotonic()
        code = driver.drive()
    print(f"scripted driver, chapters 1-6: {time.monotonic() - started:.1f}s")

    assert code == 0, (console.lines[-10:], console.stderr[-3000:])
    lines = console.lines
    says = console.says()
    keys = [say["key"] for say in says]

    # Every chapter, once, in order; the session completed and was kept.
    headings = [say["text"] for say in says if say["key"] == "chapter.heading"]
    assert [int(re.search(r"\d", heading).group()) for heading in headings] == [1, 2, 3, 4, 5, 6]
    # /quit at the end saves the finished progress, then says goodbye.
    assert keys[-3:] == ["ch6.done", "console.saved", "console.bye"]
    assert [c["keep_workflow"] for c in fake.completions] == [True]
    assert (tracked_copy / ".playground" / "report.json").is_file()

    # Only the four kinds of question, and yes went on at every continue wait.
    awaits = [line["expect"] for line in lines if line.get("type") == "await"]
    assert set(awaits) <= KINDS and "continue" in awaits and "yes_no" in awaits
    continued = [step for step, expect, reply in driver.answers if expect == "continue"]
    assert {"KEY", "CODE3", "DRY_RUN", "START", "CODE5", "RESULTS"} <= set(continued)
    assert all(reply == "yes" for _step, expect, reply in driver.answers if expect == "continue")
    assert "reply.unknown" not in keys
    # The happy path asks each question once (the keep question stays open
    # after /report, so it takes one more answer).
    assert all(times == 1 for step, times in driver.asks.items() if step != "KEEP"), driver.asks
    assert driver.asks["KEEP"] == 2
    # Each code step's change was running before it was answered.
    assert keys.count("app.reloaded") >= 2

    # The say records: a block each, no restart counts, each achievement
    # once, and a card with the minutes measured.
    assert all("block" in say for say in says)
    assert not [say["text"] for say in says if "boot" in say["text"].lower()]
    assert sorted(key for key in keys if key.startswith("achievement.")) == sorted(_ACHIEVEMENTS)
    (card,) = [say for say in says if say["key"].startswith("complete.card")]
    assert card["key"] == "complete.card"
    assert re.search(r"\d+ min", card["text"])

    # Each wait for queued logs reports how far it got.
    for at, line in enumerate(lines):
        if line.get("type") == "say" and line["key"] in ("drain.waiting", "drain.waiting_quit"):
            following = lines[at + 1]
            assert following.get("type") == "progress" and following.get("phase") == "deliver"

    # The learning run tapers: what is left to send when its visitors are
    # done is a short tail.
    learning = _runs(driver.arrived, "ch3.learn.intro", "ch3.done")
    start, end = keys_at(lines, "ch3.learn.intro"), keys_at(lines, "ch3.done")
    drained = [lines[at + 1] for at in range(start, end) if lines[at].get("key") == "drain.waiting"]
    if not drained:
        print("the learning run left nothing to send")
    else:
        highest = _highest_rate(learning)
        allowance = max(BACKLOG_FLOOR, PACER_POLL_S * highest)
        tail = drained[0]["total"]
        print(f"learning run tail {tail}, fastest delivery {highest:.0f}/s")
        assert tail <= 2 * allowance + LOGS_PER_VISIT_MAX * ONLINE_CONCURRENCY

    assert apps_left_running(tracked_copy) == set()
