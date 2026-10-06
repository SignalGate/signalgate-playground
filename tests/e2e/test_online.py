"""The whole tutorial, chapters 1-6, in ``--ci --yes`` against the test-only fake.

The console and the sample app run as subprocesses on a copy of the tracked
tree; the start-up hook points the console's API client and the app's SDK at
the fake, served on loopback by this test. The fake's script plays the
dashboard: once the learning run is stored, each status call moves the
workflow one step (created, dry run, ready, started). The test plays the
code edits: the chapter 3 logs are in place from the start, and when
chapter 5 opens it saves the check() gate, then lets protection turn on
(stage 2 of 3). So chapter 6 offers a re-run for full protection, which
``--yes`` declines: the session goes on to the keep question and completes.

The key in ``app/.env`` never shows up in the console's output or in
``.playground/``.
"""

from __future__ import annotations

import re
import shutil
import sys
import time
from pathlib import Path

import pytest

from playground.copy.en import text
from playground.traffic.local_constants import RELOAD_DEBOUNCE_S
from playground.verify.workflow import G1_MIN_ACTIONS
from tests.e2e.conftest import EXIT_WITHIN_S, apps_left_running
from tests.e2e.fake_host import E2E_KEY, serve_fake
from tests.e2e.seam import Seam
from tests.fake_server import FakeServer, Script, no_workflow, playground_flow
from tests.fake_server.script import PREDICTION

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(sys.platform == "win32", reason="POSIX subprocess handling"),
]

RIGHT = Path(__file__).resolve().parents[1] / "verify" / "fixtures" / "right"
CHAPTER_3_ROUTES = RIGHT / "ch3_plain.py"
CHAPTER_5_ROUTES = RIGHT / "ch5_plain.py"

SIZE = 1100
# The whole run's budget: chapter 1 offline, then two runs of SIZE visitors
# through the app and the fake, and the waits in between.
ONLINE_WITHIN_S = 240.0

# What the dashboard does once the learning run is in: New flow, Create,
# ▶ Dry Run (queued, three stages), the prediction, ▶ Start.
DASHBOARD = [
    no_workflow(),
    playground_flow("draft"),
    playground_flow("queued"),
    *(playground_flow("dry_running", stage=stage) for stage in (1, 2, 3)),
    playground_flow("ready", prediction=PREDICTION),
    playground_flow("active", prediction=PREDICTION),
]
# Protection on, short of full: chapter 6 offers the re-run.
PROTECTING = playground_flow("active", prediction=PREDICTION, protecting=True, protection_stage=2)


def heading(chapter: int) -> str:
    return text("chapter.heading", chapter=chapter, title=text(f"chapter.{chapter}"))


def _frame_rows(frame: str) -> dict[str, list[str]]:
    """The results frame's rows: label -> the cells after it."""
    rows = {}
    for match in re.finditer(r"^ (\S.{29})(.*)$", frame, re.MULTILINE):
        rows[match.group(1).strip()] = match.group(2).split()
    return rows


def _no_key_anywhere(console, tree: Path) -> None:
    assert E2E_KEY not in "\n".join(str(line) for line in console.lines)
    assert E2E_KEY not in console.stderr
    for path in (tree / ".playground").rglob("*"):
        if path.is_file():
            assert E2E_KEY not in path.read_text(encoding="utf-8", errors="replace"), path


def test_ci_yes_runs_all_six_chapters_against_the_fake(tracked_copy, tmp_path, open_ci_console):
    script = Script(DASHBOARD, advance_on_poll=True, hold_until_actions=G1_MIN_ACTIONS)
    fake = FakeServer(script)
    routes = tracked_copy / "app" / "routes.py"
    (tracked_copy / "app" / ".env").write_text(f"SIGNALGATE_API_KEY={E2E_KEY}\n")
    shutil.copyfile(CHAPTER_3_ROUTES, routes)

    with serve_fake(fake) as url:
        seam = Seam.create(tmp_path / "seam", fake_url=url)
        started = time.monotonic()
        console = open_ci_console(seam.env(tracked_copy), "--yes", "--size", str(SIZE))

        console.expect(ONLINE_WITHIN_S, type="say", key="chapter.heading", text=heading(5))
        shutil.copyfile(CHAPTER_5_ROUTES, routes)
        # Let the file watcher see the save before protection turns on.
        time.sleep(RELOAD_DEBOUNCE_S * 5)
        script.set_workflow(PROTECTING)

        code = console.wait_exit(ONLINE_WITHIN_S - (time.monotonic() - started) + EXIT_WITHIN_S)
    print(f"--ci --yes, chapters 1-6: {time.monotonic() - started:.1f}s")

    assert code == 0, (console.lines[-10:], console.stderr[-3000:])
    keys = console.say_keys()
    headings = [line["text"] for line in console.says() if line["key"] == "chapter.heading"]
    assert headings == [heading(n) for n in range(1, 7)]
    for key in (
        "ch2.key.ok",
        "ch2.git.no_git",
        "ch2.session.ok",
        "app.restarted_key",
        "ch3.done",
        "ch4.prediction",
        "ch5.live",
        "ch5.readiness.live",
        "results.frame",
        "ch6.kept",
        "ch6.handoff",
    ):
        assert key in keys, key
    assert keys[-2:] == ["ch6.done", "console.bye"]
    # Each achievement once, the results' after the frame, then the card.
    achievements = [key for key in keys if key.startswith("achievement.")]
    assert sorted(achievements) == sorted(
        f"achievement.{name}"
        for name in ("ch1", "connected", "ch3", "learned", "prediction", "ch5", "results")
    )
    assert keys.index("results.frame") < keys.index("achievement.results")
    cards = [key for key in keys if key.startswith("complete.card")]
    assert len(cards) == 1
    assert keys.index("ch6.kept") < keys.index(cards[0]) < keys.index("ch6.handoff")
    # The re-run offer is declined, so the keep question and /complete follow.
    offered = keys.index("ch6.rerun.offer")
    assert keys[offered + 1] == "ch6.keep"
    assert keys[offered:].index("ch6.kept") < keys[offered:].index("ch6.done")
    assert "ch6.rerun.later" not in keys
    waits = [line["text"] for line in console.lines if line.get("type") == "wait"]
    assert "dry run queued" in waits and "dry run: stage 2 of 3" in waits

    # The results frame: 20.0% before. After: the fake blocks the whole
    # attack, so exactly the real users' actions happen, none of them is
    # blocked, and every success still happens.
    (frame,) = [line["text"] for line in console.says() if line["key"] == "results.frame"]
    rows = _frame_rows(frame)
    assert rows["Conversion"][0] == "20.0%"
    blocked, _of, real_total = rows["Real users blocked"][1:4]
    assert blocked == "0" and int(real_total.replace(",", "")) > 0
    actions_before, actions_after = (int(n.replace(",", "")) for n in rows["Actions performed"])
    successes_before, successes_after = (int(n.replace(",", "")) for n in rows["Successes"])
    assert actions_before == SIZE
    assert actions_after == int(real_total.replace(",", ""))
    assert successes_after == successes_before
    conversion_after = successes_after / actions_after
    assert rows["Conversion"][1:] == [f"{conversion_after:.1%}", "▲"]
    assert text("stage.partial", stage=2) in [line["text"].strip() for line in console.says()]

    # What SignalGate saw: one session, completed and kept, no failed checks.
    assert [c["keep_workflow"] for c in fake.completions] == [True]
    results = fake.completions[0]["results"]
    assert results["conv_before"] == pytest.approx(0.2, abs=0.005)
    assert results["conv_after"] == pytest.approx(conversion_after)
    assert fake.telemetry == []
    assert {d.method for d in fake.delivered("log")} == {"playground.action", "playground.success"}

    _no_key_anywhere(console, tracked_copy)
    assert apps_left_running(tracked_copy) == set()
