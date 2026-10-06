"""The console's waits, /copy, saves, the delivery line and a bad paste, on a real terminal.

The console runs in a pseudo-terminal, as it does for a person. Most tests
start from saved progress at the chapter 3 code step, with a made-up session
(the steps here never reach SignalGate); ``/continue`` picks it up. There
the test types what a user types and edits ``app/routes.py`` as a user
would: pressing Enter or typing yes, ``/copy`` into a clipboard tool that is
a stand-in on ``PATH`` (the real one never runs), three saves in a row, the
task's code read off the screen and pasted with four spaces too many, then
pasted right.

The wait for the app's queued log events is shown after a chapter 1 run of
an app that logs, with its deliveries held (``gated_seam``): one line on
screen, redrawn in place, that the release or Ctrl-C ends.
"""

from __future__ import annotations

import ast
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from playground import statefile
from playground.copy.en import TASKS, snippet_text, text
from playground.machine.events import (
    AppReady,
    CheckKey,
    Gate,
    OpenSession,
    Prediction,
    ReadStage,
    RunFinished,
    RunGate,
    RunReadiness,
    RunStats,
    RunTraffic,
    StepDone,
    WaitWorkflow,
)
from playground.machine.machine import Machine
from playground.traffic.local_constants import DRAIN_REPORT_EVERY_S
from tests.e2e import conftest
from tests.e2e.conftest import (
    BYE,
    CLOSE_QUESTION,
    CTRL_C,
    CTRL_D,
    EXIT_WITHIN_S,
    HELP_LAST_COMMAND,
    OPENING,
    PROMPT,
    READY_WITHIN_S,
    REPLY_WITHIN_S,
    RUN_WITHIN_S,
    Terminal,
    append_comment,
    ended_cleanly,
    flatten,
    use_logging_app,
)

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(sys.platform == "win32", reason="needs a POSIX pseudo-terminal"),
]

WELCOME_BACK = "Welcome back. Type /continue"
SAVED_LINE = text("app.reloaded")
SENDING = text("drain.sending")
SOLUTIONS = Path(__file__).resolve().parents[2] / "solutions"

SIZE = 1_100
# A made-up session: its limits have the shape the console checks, and no
# step here talks to SignalGate.
SESSION = {
    "session_id": "e2e-session-not-real",
    "size": SIZE,
    "limits": {
        "max_size": 5_000,
        "lease_max": 50,
        "use_within_s": 45,
        "max_queue_age_s": 90,
        "clock_warn_s": 7,
        "clock_block_s": 33,
        "readiness_retry_s": 3,
        "readiness_max_attempts": 2,
        "ingest_wait_s": 4,
        "protect_diagnose_after_s": 9,
    },
}
RUN_FINISHED = RunFinished(total=10_000, actions=10_000, successes=2_000, attack=7_297, real=2_703)


# -- saved progress ------------------------------------------------------------------------


def _at_code3() -> Machine:
    """A console that finished chapter 2: the chapter 3 code step is open."""
    machine = Machine(size=SIZE)
    machine.start()
    machine.on_event(AppReady())
    machine.handle("/start")
    machine.on_event(RUN_FINISHED)
    machine.handle("yes")
    assert machine.handle("") == [CheckKey()]
    machine.on_event(StepDone(True))
    assert machine.handle("yes") == [OpenSession(SIZE)]
    machine.on_event(StepDone(True, session=SESSION))
    assert machine.to_dict()["step"] == "CODE3"
    return machine


def _at_rerun_offer() -> Machine:
    """A console that showed the results at protection stage 1: the re-run is offered."""
    machine = _at_code3()
    assert machine.handle("") == [RunGate(3)]
    machine.on_event(StepDone(True))
    machine.on_event(StepDone(True, stats=RunStats(total=SIZE, actions=SIZE, successes=220)))
    assert machine.handle("") == [WaitWorkflow(Gate.G2)]
    machine.on_event(StepDone(True, prediction=Prediction(0.2, 0.6, 0.7)))
    assert machine.handle("") == [WaitWorkflow(Gate.G3)]
    machine.on_event(StepDone(True))  # chapter 5 opens, waiting for protection
    machine.on_event(StepDone(True, stage=1))
    assert machine.handle("") == [RunGate(5)]
    assert machine.on_event(StepDone(True))[-1] == RunReadiness()
    assert isinstance(machine.on_event(StepDone(True))[-1], RunTraffic)
    protected = RunStats(
        total=SIZE, actions=300, successes=220, real_blocked=0, real_total=300, check_ms=9.0
    )
    machine.on_event(StepDone(True, stats=protected))
    assert machine.handle("") == [ReadStage()]
    machine.on_event(StepDone(True, stage=1))
    assert machine.to_dict()["step"] == "RERUN_OFFER"
    return machine


def _save(tree: Path, machine: Machine) -> None:
    assert statefile.save(tree / ".playground" / "state.json", machine.to_dict())


def _resume(terminal: Terminal, question: str) -> None:
    terminal.expect(WELCOME_BACK, READY_WITHIN_S)
    terminal.type("/continue")
    terminal.expect(question, REPLY_WITHIN_S)


# -- a stand-in clipboard ----------------------------------------------------------------------

# Every clipboard tool the console may try, all played by one script: it
# keeps what it is given and succeeds, or, once told to fail, fails.
_CLIPBOARD_TOOLS = ("pbcopy", "wl-copy", "xclip")


def _fake_clipboard(directory: Path) -> Path:
    directory.mkdir()
    copied = directory / "copied.txt"
    script = (
        "#!/bin/sh\n"
        f'if [ -e "{directory / "fail"}" ]; then cat > /dev/null; exit 1; fi\n'
        f'cat > "{copied}"\n'
    )
    for name in _CLIPBOARD_TOOLS:
        tool = directory / name
        tool.write_text(script)
        tool.chmod(0o755)
    return copied


def _env_with_clipboard(env: dict[str, str], directory: Path) -> dict[str, str]:
    env = dict(env)
    env["PATH"] = str(directory) + os.pathsep + env.get("PATH", "")
    env.pop("WAYLAND_DISPLAY", None)  # the one tool tried on Linux is xclip
    return env


# -- the task, read off the screen ------------------------------------------------------------


def _code_from_screen(screen: str, task: str) -> list[list[str]]:
    """Each edit's code as a user copies it from the terminal: the lines after
    its location line and one blank line, up to the next blank line."""
    lines = screen.split("\n")
    _title, edits, _footer = TASKS[task]
    blocks = []
    for location, _snippet in edits:
        found = [at for at, line in enumerate(lines) if line.strip() == text(location)]
        assert found, text(location)
        at = found[-1] + 1
        assert not lines[at].strip()
        code = []
        for line in lines[at + 1 :]:
            if not line.strip():
                break
            code.append(line.rstrip())
        blocks.append(code)
    return blocks


def _last_import(lines: list[str]) -> int:
    tree = ast.parse("\n".join(lines))
    imports = [n for n in tree.body if isinstance(n, (ast.Import, ast.ImportFrom))]
    return max(n.end_lineno or n.lineno for n in imports) - 1


def _index(lines: list[str], wanted) -> int:
    (at,) = [i for i, line in enumerate(lines) if wanted(line)]
    return at


def _paste(source: str, blocks: list[list[str]], *, extra: str = "") -> str:
    """The chapter 3 edits at the spots the task names; ``extra`` goes in
    front of every line of the action log."""
    imports, action_log, success_log = blocks
    lines = source.split("\n")
    at = _last_import(lines)
    lines[at + 1 : at + 1] = imports
    at = _index(lines, lambda line: line == "    perform_action(body.user)")
    lines[at + 1 : at + 1] = [extra + line for line in action_log]
    at = _index(lines, lambda line: line.strip().startswith("raise HTTPException(status_code=400,"))
    lines[at + 1 : at + 1] = success_log
    return "\n".join(lines)


def _reference_solution(tmp_path: Path, shipped: str) -> bytes:
    tree = tmp_path / "reference"
    (tree / "app").mkdir(parents=True)
    (tree / "app" / "routes.py").write_text(shipped)
    env = {**os.environ, "GIT_CEILING_DIRECTORIES": str(tree.parent)}
    subprocess.run(
        ["git", "apply", str(SOLUTIONS / "chapter3.diff")],
        cwd=tree,
        env=env,
        check=True,
        capture_output=True,
    )
    return (tree / "app" / "routes.py").read_bytes()


# -- the tests ----------------------------------------------------------------------------------


def test_the_chapter_3_step_on_a_real_terminal(open_terminal, tracked_copy, seam, tmp_path):
    routes = tracked_copy / "app" / "routes.py"
    shipped = routes.read_text()
    _save(tracked_copy, _at_code3())
    clipboard = tmp_path / "clipboard"
    copied = _fake_clipboard(clipboard)
    terminal = open_terminal(_env_with_clipboard(seam.env(tracked_copy), clipboard))
    _resume(terminal, text("ch3.ask"))
    # Picked up at the code step: the "Your turn" block is shown again, above
    # the question.
    resumed = terminal.text
    title = "╭ " + text("ch3.task.title")
    assert resumed.count(title) == 1
    assert resumed.index(title) < resumed.rindex(text("ch3.task.footer"))
    flat = terminal.flat
    assert flat.index(text("ch3.task.footer")) < flat.rindex(flatten(text("ch3.ask")))

    # Enter and yes both run the check (here it stops at the missing key).
    terminal.type("")
    terminal.expect(text("ch3.again"), REPLY_WITHIN_S)
    terminal.type("yes")
    terminal.expect(text("ch3.again"), REPLY_WITHIN_S)
    assert terminal.count(text("ch3.again")) == 2
    assert terminal.count(text("ch2.key.missing").split("\n")[0]) == 2

    # /copy: onto the clipboard, then (with no working tool) into a file.
    terminal.type("/copy")
    terminal.expect(text("copy.done", count=3), REPLY_WITHIN_S)
    assert copied.read_text() == snippet_text("ch3.task")
    (clipboard / "fail").touch()
    terminal.type("/copy")
    written = ".playground/snippets/chapter3.py"
    terminal.expect(text("copy.file", path=written), REPLY_WITHIN_S)
    assert (tracked_copy / written).read_text() == snippet_text("ch3.task")
    terminal.type("/copy 2")
    terminal.expect(text("copy.file", path=".playground/snippets/chapter3-2.py"), REPLY_WITHIN_S)
    assert (tracked_copy / ".playground/snippets/chapter3-2.py").read_text() == snippet_text(
        "ch3.task", 2
    )

    # Three saves, each once the one before it is running: one "Saved" line.
    assert terminal.count(SAVED_LINE) == 0
    for boots in (2, 3, 4):
        append_comment(routes)
        seam.wait_for_boots(boots, READY_WITHIN_S)
    terminal.expect(SAVED_LINE, READY_WITHIN_S)
    terminal.drain_output(DRAIN_REPORT_EVERY_S * 4)
    assert terminal.count(SAVED_LINE) == 1

    # The task block, from the last hint level, read off the screen.
    for level in (1, 2, 3):
        terminal.type("/hint")
        terminal.expect(text(f"hint.ch3.{level}").split("\n")[0], REPLY_WITHIN_S)
    terminal.expect(text("ch3.task.footer"), REPLY_WITHIN_S)
    screen = terminal.text
    assert screen.count(title) == 2  # once when picked up, once for the hint
    blocks = _code_from_screen(screen, "ch3.task")
    assert ["\n".join(block) for block in blocks] == [
        text(snippet) for _location, snippet in TASKS["ch3.task"][1]
    ]
    task_lines = screen[screen.index("╭ ") : screen.index("╯")].split("\n")
    assert not any(line.startswith(("│", "|")) for line in task_lines)

    # Pasted with four spaces too many: the app doesn't start, and says why.
    source = shipped.rstrip("\n") + "\n"
    pasted = _paste(source, blocks, extra="    ")
    # Python names the first line that is indented too far.
    first = pasted.split("\n").index("    " + blocks[1][0]) + 1
    routes.write_text(pasted)
    terminal.expect(
        text("app.down.error", error="IndentationError", location=f"app/routes.py:{first}"),
        READY_WITHIN_S,
    )
    terminal.expect(text("app.down.hint.indent"), REPLY_WITHIN_S)
    terminal.expect(text("app.down.fix"), REPLY_WITHIN_S)

    # Pasted as shown: the app runs the change, which is the reference solution.
    routes.write_text(_paste(source, blocks))
    terminal.expect(SAVED_LINE, READY_WITHIN_S)
    assert routes.read_bytes() == _reference_solution(tmp_path, shipped)

    terminal.press(CTRL_D)
    terminal.expect(BYE, REPLY_WITHIN_S)
    assert terminal.wait_exit(EXIT_WITHIN_S) == 0
    ended_cleanly(terminal, tracked_copy)


def test_enters_typed_while_a_check_runs_start_no_more_checks(open_terminal, tracked_copy, seam):
    _save(tracked_copy, _at_code3())
    terminal = open_terminal(seam.env(tracked_copy))
    _resume(terminal, text("ch3.ask"))

    terminal.type("")  # Enter: the check starts (it stops at the missing key)
    terminal.press(b"\r" * 3)  # three more, impatiently, while it runs
    terminal.expect(text("ch3.again"), REPLY_WITHIN_S)
    # Typed once the outcome shows, this is kept and answered next; the
    # stray Enters, had they been kept, would each have run a check first.
    terminal.type("/help")
    terminal.expect(HELP_LAST_COMMAND, REPLY_WITHIN_S)
    assert terminal.count(text("ch3.again")) == 1
    assert terminal.count(text("ch2.key.missing").split("\n")[0]) == 1

    # Wait for the prompt itself: a Ctrl-D sent before the prompt takes the
    # terminal is swallowed by the line discipline as end of input.
    terminal.expect(PROMPT, REPLY_WITHIN_S)
    terminal.press(CTRL_D)
    terminal.expect(BYE, REPLY_WITHIN_S)
    assert terminal.wait_exit(EXIT_WITHIN_S) == 0
    ended_cleanly(terminal, tracked_copy)


def test_a_yes_no_question_asks_again_on_enter_and_takes_yes(open_terminal, tracked_copy, seam):
    _save(tracked_copy, _at_rerun_offer())
    terminal = open_terminal(seam.env(tracked_copy))
    offer = text("ch6.rerun.offer")
    _resume(terminal, offer)
    terminal.expect("? ›", REPLY_WITHIN_S)

    terminal.type("")
    terminal.expect(offer, REPLY_WITHIN_S)
    assert terminal.count(offer) == 2
    terminal.type("yes")
    terminal.expect(text("ch6.rerun.later").split("\n")[0], REPLY_WITHIN_S)
    assert terminal.count(offer) == 2

    terminal.press(CTRL_C)
    assert terminal.wait_exit(EXIT_WITHIN_S) == 0
    ended_cleanly(terminal, tracked_copy)


def _drain_lines(terminal: Terminal) -> list[str]:
    return [line for line in terminal.text.split("\n") if SENDING in line]


def _wait_for_redraws(terminal: Terminal, times: int) -> str:
    """Wait until the delivery line was drawn ``times`` times; return its row."""
    deadline = time.monotonic() + REPLY_WITHIN_S
    while True:
        rows = _drain_lines(terminal)
        if rows and rows[0].count(SENDING) >= times:
            return rows[0]
        if time.monotonic() > deadline:
            raise AssertionError(f"delivery line not redrawn {times} times: {rows}")
        terminal.drain_output(DRAIN_REPORT_EVERY_S)


def test_the_delivery_line_redraws_in_place_and_ends_whole(open_terminal, tracked_copy, gated_seam):
    use_logging_app(tracked_copy)
    terminal = open_terminal(gated_seam.env(tracked_copy))
    terminal.expect(OPENING, READY_WITHIN_S)

    terminal.type("/start")
    terminal.expect(SENDING, RUN_WITHIN_S)
    row = _wait_for_redraws(terminal, 2)
    assert text("drain.skip_hint") in row
    gated_seam.release()
    terminal.expect(text("drain.done"), RUN_WITHIN_S)
    terminal.expect(CLOSE_QUESTION, REPLY_WITHIN_S)
    # However often it was redrawn, the line took one row of the screen.
    assert len(_drain_lines(terminal)) == 1

    terminal.expect(PROMPT, REPLY_WITHIN_S)
    terminal.press(CTRL_D)
    terminal.expect(BYE, REPLY_WITHIN_S)
    assert terminal.wait_exit(EXIT_WITHIN_S) == 0
    ended_cleanly(terminal, tracked_copy)


@pytest.mark.parametrize("columns", [80, 60])
def test_on_a_narrow_terminal_the_delivery_line_keeps_its_count_and_ctrl_c(
    open_terminal, tracked_copy, gated_seam, monkeypatch, columns
):
    monkeypatch.setattr(conftest, "TERMINAL_COLUMNS", columns)
    use_logging_app(tracked_copy)
    terminal = open_terminal(gated_seam.env(tracked_copy))
    terminal.expect(OPENING, READY_WITHIN_S)

    terminal.type("/start")
    skip = text("drain.skip_hint")
    terminal.expect(skip, RUN_WITHIN_S)
    deadline = time.monotonic() + REPLY_WITHIN_S
    while True:
        rows = [line for line in terminal.text.split("\n") if skip in line]
        if rows and rows[0].count(skip) >= 2:
            break
        assert time.monotonic() < deadline, rows
        terminal.drain_output(DRAIN_REPORT_EVERY_S)
    # Redrawn in place on one row: the last draw is whole and fits.
    (row,) = rows
    last = row[: -len(skip)].rsplit(skip, 1)[-1] + skip
    assert len(last) <= columns
    assert "▕" in last and " / " in last and last.endswith(skip)

    gated_seam.release()
    terminal.expect(text("drain.done"), RUN_WITHIN_S)
    terminal.expect(CLOSE_QUESTION, REPLY_WITHIN_S)
    assert len([line for line in terminal.text.split("\n") if skip in line]) == 1

    terminal.expect(PROMPT, REPLY_WITHIN_S)  # a Ctrl-D before the prompt is swallowed
    terminal.press(CTRL_D)
    terminal.expect(BYE, REPLY_WITHIN_S)
    assert terminal.wait_exit(EXIT_WITHIN_S) == 0
    ended_cleanly(terminal, tracked_copy)


def test_ctrl_c_during_the_delivery_line_skips_the_wait(open_terminal, tracked_copy, gated_seam):
    use_logging_app(tracked_copy)
    terminal = open_terminal(gated_seam.env(tracked_copy))
    terminal.expect(OPENING, READY_WITHIN_S)

    terminal.type("/start")
    terminal.expect(SENDING, RUN_WITHIN_S)
    _wait_for_redraws(terminal, 2)
    terminal.press(CTRL_C)
    terminal.expect("Skipped the wait;", REPLY_WITHIN_S)
    terminal.expect(CLOSE_QUESTION, REPLY_WITHIN_S)
    assert len(_drain_lines(terminal)) == 1
    assert terminal.count(text("drain.done")) == 0

    terminal.press(CTRL_C)
    assert terminal.wait_exit(EXIT_WITHIN_S) == 0
    ended_cleanly(terminal, tracked_copy)


def test_the_saved_progress_here_is_what_this_version_writes(tmp_path):
    """The saved states above load back unchanged (so the tests start where they say)."""
    for machine in (_at_code3(), _at_rerun_offer()):
        _save(tmp_path, machine)
        loaded, problem = statefile.load(tmp_path / ".playground" / "state.json")
        assert problem is None and loaded == machine.to_dict()
