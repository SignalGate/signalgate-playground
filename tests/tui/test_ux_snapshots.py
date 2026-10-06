"""Snapshots of the console's scenes, at every colour level and in ASCII.

One real ``Machine`` is driven through the whole tutorial, the way a first
run goes: the opening, chapter 1 and its close, the "Your turn" block of
chapter 3 with a save, a check that fails and one that passes, the chapters
after it, and the results with the completion card. Its effects get made-up
outcomes (``StepDone``), and ``tests.tui.play.Play`` draws it as a terminal
shows it, typed answers included. Each scene is cut out of that one
transcript, so a scene starts where the one before it left the screen.

The wait for the app's queued log events is drawn from the renderer's pure
functions instead: its line is redrawn in place on a terminal, so a
recorded console could only show its last state.

Snapshots live under ``snapshots_ux/`` and follow the same convention as
the others: regenerate only with ``PLAYGROUND_UPDATE_SNAPSHOTS=1``.
"""

from __future__ import annotations

import io
import os
import re
from dataclasses import dataclass, replace
from pathlib import Path

import pytest
from rich.console import Console, Group
from rich.constrain import Constrain

from playground.copy.en import text
from playground.machine.events import (
    AppReady,
    AppReloaded,
    CheckKey,
    Complete,
    DeliverProgress,
    Gate,
    OpenSession,
    Phase,
    Prediction,
    ReadStage,
    RunChapter1,
    RunFinished,
    RunGate,
    RunProgress,
    RunReadiness,
    RunStats,
    RunTraffic,
    Say,
    StartApp,
    StepDone,
    WaitWorkflow,
)
from playground.machine.steps import _ACHIEVEMENTS
from playground.tui.colour import Level, style, to_ascii
from playground.tui.console import InteractiveRenderer, render_drain_line, render_panel
from playground.tui.render import copy_text, paragraph_columns
from playground.verify.messages import gate_lines
from playground.verify.model import Check, Finding, GateResult, Site
from tests.tui.play import COLOUR_SYSTEMS, Play

SNAPSHOT_DIR = Path(__file__).parent / "snapshots_ux"

# Each mode: its colour level and whether ASCII mode is on.
MODES: dict[str, tuple[Level, bool]] = {
    "truecolor": ("truecolor", False),
    "256": ("256", False),
    "16": ("16", False),
    "none": ("none", False),
    "ascii": ("none", True),
}

SCENES = (
    "opening",
    "chapter1",
    "ch3_edit",
    "ch3_fail",
    "ch3_pass",
    "ch5",
    "results",
)

WIDTH = 100
SIZE = 2_000
LIMITS = {
    "max_size": 3000,
    "lease_max": 50,
    "use_within_s": 45,
    "max_queue_age_s": 90,
    "clock_warn_s": 7,
    "clock_block_s": 33,
    "readiness_retry_s": 3,
    "readiness_max_attempts": 2,
    "ingest_wait_s": 4,
    "protect_diagnose_after_s": 9,
}
SESSION = {"session_id": "sid-ux", "size": SIZE, "limits": LIMITS}
RUN_FINISHED = RunFinished(total=10_000, actions=10_000, successes=2_000, attack=7_297, real=2_703)
LEARN = RunStats(total=SIZE, actions=SIZE, successes=400)
ROUND = RunStats(
    total=SIZE,
    actions=541,
    successes=400,
    real_blocked=0,
    real_total=541,
    check_ms=12.0,
    inside_ms=0.0,
)
PREDICTION = Prediction(0.2, 0.669, 0.7)
# The active time the runner stamps on the results: the card says 25 min.
RESULTS_AT_S = 1_500.0

# The mistake the chapter 3 check names: confirm() reads the action's envelope.
_WRONG_LINE = '    ev = sg_event(request, body.user, body.signalgate_log, "playground.success")'
_ERROR = "AttributeError: 'ConfirmBody' object has no attribute 'signalgate_log'"

_ANSI = re.compile(r"\x1b\[[0-9;]*m")


def _check_snapshot(name: str, actual: str) -> None:
    if os.environ.get("PLAYGROUND_SKIP_SNAPSHOTS") == "1":
        pytest.skip("snapshot comparison skipped (PLAYGROUND_SKIP_SNAPSHOTS=1)")
    path = SNAPSHOT_DIR / f"{name}.txt"
    if os.environ.get("PLAYGROUND_UPDATE_SNAPSHOTS") == "1":
        SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
        path.write_text(actual)
        return
    assert path.exists(), f"missing snapshot {path}; rerun with PLAYGROUND_UPDATE_SNAPSHOTS=1"
    assert actual == path.read_text(), f"{name} snapshot mismatch"


def _plain(output: str) -> str:
    return _ANSI.sub("", output)


# -- what the checks say ------------------------------------------------------------------


def _lines(result: GateResult) -> tuple[Say, ...]:
    return tuple(Say("verify.line", {"line": line}) for line in gate_lines(result))


def _passed(gate: str, names: tuple[str, ...], **fields: object) -> tuple[Say, ...]:
    return _lines(GateResult(gate, tuple(Check.judged(name, []) for name in names), **fields))


CH3_FAILED = _lines(
    GateResult(
        "code",
        (
            Check.judged("action", []),
            Check.judged(
                "confirm",
                [
                    Finding(
                        "CONFIRM_READS_LOG_FIELD",
                        site=Site("app/routes.py", 56, _WRONG_LINE.strip()),
                        params={"user": "u-1042", "status": 500, "error": _ERROR},
                    )
                ],
            ),
        ),
        needs_proof=True,
    )
)
CH3_PASSED = _passed(
    "code", ("static", "action", "confirm", "failure", "shape"), accepted=4, needs_proof=True
)
CH5_PASSED = (
    *_passed(
        "code", ("static", "action", "confirm", "gate", "shape"), accepted=6, needs_proof=True
    ),
    *_passed("rehearsal", ("allow", "block", "outage")),
)


# -- the tutorial ---------------------------------------------------------------------------


@dataclass
class Transcript:
    output: str
    scenes: dict[str, str]
    says: list[Say]


def _done(passed: bool = True, **fields: object) -> StepDone:
    return StepDone(passed, **fields)  # type: ignore[arg-type]


def _run_panel(play: Play, *, total: int, progress: RunProgress, title: str | None) -> None:
    """A run's live panel: on a recorded console only its final state stays."""
    play.renderer.run_started(total, title=title)
    play.renderer.progress(progress)
    play.renderer.run_ended()


def _tutorial(monkeypatch: pytest.MonkeyPatch, level: Level, ascii: bool) -> Transcript:
    play = Play(monkeypatch, level=level, ascii=ascii, width=WIDTH)
    scenes: dict[str, str] = {}
    at = 0

    def cut(name: str) -> None:
        nonlocal at
        scenes[name] = play.output[at:]
        at = len(play.output)

    def skip() -> None:
        nonlocal at
        at = len(play.output)

    # The opening.
    assert play.start() == [StartApp()]
    play.feed(AppReady())
    cut("opening")

    # Chapter 1: the run, its end, the close question (Enter asks it again).
    assert play.answer("/start") == [RunChapter1()]
    _run_panel(play, total=10_000, progress=RunProgress(10_000, 10_000, 10_000, 2_000), title=None)
    play.feed(replace(RUN_FINISHED, elapsed_s=60.0))
    play.answer("")
    play.answer("yes")
    cut("chapter1")

    # Chapter 2, off screen here.
    assert play.answer("") == [CheckKey()]
    play.feed(_done(says=(Say("ch2.key.ok"), Say("ch2.git.no_git")), elapsed_s=120.0))
    assert play.answer("yes") == [OpenSession(SIZE)]
    play.feed(_done(says=(Say("ch2.session.ok"),), session=SESSION, elapsed_s=180.0))
    # The scene starts with the blank line before chapter 3's heading.
    heading_at = play.output.rindex("\n", 0, play.output.index("Chapter 3 of 6", at))
    at = play.output.rindex("\n", 0, heading_at) + 1
    # Chapter 3: the edits are saved while the question is open.
    play.feed(AppReloaded(2, "change"))
    cut("ch3_edit")

    # The check names the mistake.
    assert play.answer("") == [RunGate(3)]
    play.feed(_done(False, says=CH3_FAILED, elapsed_s=300.0))
    cut("ch3_fail")

    # Fixed and saved: the check passes and the learning run starts.
    play.feed(AppReloaded(3, "change"))
    assert play.answer("") == [RunGate(3)]
    effects = play.feed(_done(says=CH3_PASSED, elapsed_s=360.0))
    assert effects == [RunTraffic(Phase.LEARN, SIZE)]
    cut("ch3_pass")

    # The learning run, chapter 4, off screen here.
    learning = RunProgress(SIZE, SIZE, SIZE, 400, posted=SIZE + 400, delivered=SIZE + 400)
    _run_panel(play, total=SIZE, progress=learning, title="panel.learn.title")
    play.feed(_done(stats=LEARN, elapsed_s=480.0))
    assert play.answer("") == [WaitWorkflow(Gate.G2)]
    play.feed(_done(prediction=PREDICTION, elapsed_s=600.0))
    assert play.answer("") == [WaitWorkflow(Gate.G3)]
    skip()

    # Chapter 5: its entry, protection on, the gate saved, its check passes.
    assert play.feed(_done(elapsed_s=720.0)) == [WaitWorkflow(Gate.G4)]
    play.feed(_done(stage=1, elapsed_s=780.0))
    play.feed(AppReloaded(4, "change"))
    assert play.answer("") == [RunGate(5)]
    assert play.feed(_done(says=CH5_PASSED, elapsed_s=900.0)) == [RunReadiness()]
    cut("ch5")

    # The readiness round and the protected round, off screen here.
    effects = play.feed(_done(says=(Say("ch5.readiness.live"),), elapsed_s=960.0))
    assert effects == [RunTraffic(Phase.PROTECT, SIZE, 1)]
    protected = RunProgress(SIZE, SIZE, 541, 400, posted=941, delivered=941, accepted=941)
    _run_panel(play, total=SIZE, progress=protected, title="panel.round.title")
    play.feed(_done(stats=ROUND, elapsed_s=1_200.0))
    skip()

    # Chapter 6: the results, keep the workflow, the card and what's next.
    assert play.answer("") == [ReadStage()]
    play.feed(_done(stage=3, elapsed_s=RESULTS_AT_S))
    assert play.answer("yes") == [Complete(True, 0.2, 400 / 541)]
    play.feed(_done(says=(Say("ch6.kept"),), elapsed_s=1_800.0))
    assert play.expect == "any"
    cut("results")
    return Transcript(play.output, scenes, play.says())


def _drain(level: Level, ascii: bool) -> str:
    """The wait for the queued log events: its line mid-way (with each Ctrl-C
    hint) and the line that ends it, then the run panel with its delivery
    bar, time left and the note under it."""
    buffer = io.StringIO()
    console = Console(file=buffer, width=WIDTH, color_system=COLOUR_SYSTEMS[level])
    for quit_hint in (False, True):
        console.print(
            render_drain_line(
                DeliverProgress(480, 922, 30.0),
                quit_hint=quit_hint,
                level=level,
                ascii=ascii,
                width=WIDTH,
            )
        )
    renderer = InteractiveRenderer(console, level, ascii=ascii)
    renderer.show(Say("drain.done"))
    console.print()
    panel = render_panel(
        level=level,
        ascii=ascii,
        total=SIZE,
        done=SIZE,
        actions=SIZE,
        successes=400,
        title="panel.learn.title",
        event=RunProgress(
            SIZE, SIZE, SIZE, 400, posted=2_400, delivered=1_478, accepted=1_478, eta_s=47.0
        ),
    )
    note = copy_text("drain.note", level=level, ascii=ascii)
    note.stylize(style("dim", level))
    console.print(Group(panel, Constrain(note, width=paragraph_columns(WIDTH))))
    return buffer.getvalue()


# -- the snapshots -----------------------------------------------------------------------------


@pytest.mark.parametrize("mode", MODES)
def test_every_scene_of_a_first_run(monkeypatch, mode):
    level, ascii = MODES[mode]
    transcript = _tutorial(monkeypatch, level, ascii)
    assert list(transcript.scenes) == list(SCENES)
    for scene, output in transcript.scenes.items():
        _check_snapshot(f"{scene}_{mode}", output)


@pytest.mark.parametrize("mode", MODES)
def test_the_wait_for_queued_logs(mode):
    level, ascii = MODES[mode]
    _check_snapshot(f"drain_{mode}", _drain(level, ascii))


@pytest.mark.parametrize("mode", MODES)
def test_level_none_has_no_escapes_and_ascii_mode_is_ascii(monkeypatch, mode):
    level, ascii = MODES[mode]
    outputs = [_tutorial(monkeypatch, level, ascii).output, _drain(level, ascii)]
    for output in outputs:
        assert "**" not in output
        if level == "none":
            assert "\x1b[" not in output
        else:
            assert "\x1b[" in output
        if ascii:
            assert output.isascii()


@pytest.mark.parametrize("mode", MODES)
def test_a_first_run_shows_each_achievement_once_and_a_timed_card(monkeypatch, mode):
    level, ascii = MODES[mode]
    transcript = _tutorial(monkeypatch, level, ascii)
    plain = _plain(transcript.output)
    for key in _ACHIEVEMENTS:
        line = to_ascii(text(key)) if ascii else text(key)
        assert plain.count(line) == 1, key
    shown = [say.key for say in transcript.says if say.key.startswith("achievement.")]
    assert sorted(shown) == sorted(_ACHIEVEMENTS)
    cards = [say for say in transcript.says if say.key.startswith("complete.card")]
    assert [(say.key, dict(say.params)) for say in cards] == [("complete.card", {"minutes": 25})]


def test_the_scenes_read_like_the_console(monkeypatch):
    """The plain transcript keeps the console's layout: headings are rules with
    progress dots, no line says "boot", one blank line at most between
    lines, and the question sits right on the input line."""
    transcript = _tutorial(monkeypatch, "none", False)
    lines = transcript.output.split("\n")
    for at in range(1, len(lines)):
        assert lines[at] or lines[at - 1], f"two blank lines at {at}"
    assert "boot" not in transcript.output.lower()
    headings = [line for line in lines if "Chapter " in line and " of 6 · " in line]
    assert [line[:2] for line in headings] == ["━━"] * 6
    assert headings[2].startswith(
        "━━ " + text("chapter.heading", chapter=3, title=text("chapter.3"))
    )
    assert "●●●○○○" in headings[2]
    # A question sits right on its input line, where the typed answer stays.
    close = transcript.scenes["chapter1"].split("\n")
    asked = [at for at, line in enumerate(close) if line.endswith("(yes/no)")]
    assert [close[at + 1] for at in asked] == ["? › ", "? › yes"]
    # A save while the question is open is noted above the input line.
    edit = transcript.scenes["ch3_edit"].split("\n")
    ask = edit.index(text("ch3.ask"))
    assert edit[ask - 1] == ""
    assert edit[ask + 2] == text("app.reloaded")
