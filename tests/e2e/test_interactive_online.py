"""Chapters 2-6 on a real terminal, against the test-only fake.

The console runs in a pseudo-terminal, as it does for a person; the start-up
hook points its API client and the sample app's SDK at the fake, served on
loopback by this test. The test types what a user types and edits
``app/routes.py`` the way a user would: a first try with a mistake, which
the code gate names, then the fix. The fake's script plays the dashboard.

It starts from saved progress written by an older version of the console
(chapter 1 done), so it also covers picking that up with ``/continue``.
"""

from __future__ import annotations

import json
import shutil
import sys

import filelock
import pytest

from playground.copy.en import text
from playground.verify.workflow import G1_MIN_ACTIONS
from tests.e2e.conftest import EXIT_WITHIN_S, READY_WITHIN_S, REPLY_WITHIN_S, apps_left_running
from tests.e2e.fake_host import E2E_KEY, serve_fake
from tests.e2e.seam import Seam
from tests.e2e.test_online import CHAPTER_3_ROUTES, CHAPTER_5_ROUTES, DASHBOARD
from tests.fake_server import FakeServer, Script, playground_flow
from tests.fake_server.script import PREDICTION

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(sys.platform == "win32", reason="needs a POSIX pseudo-terminal"),
]

SIZE = 1100
# One step that talks to the fake and the app: a gate, a wait, a run.
STEP_WITHIN_S = 90.0

PROTECTING_STAGE_1 = playground_flow(
    "active", prediction=PREDICTION, protecting=True, protection_stage=1
)

# Chapter 1 done, saved by the version before chapters 2-6 existed.
OLDER_SAVE = {
    "v": 1,
    "machine": {
        "offline_total": 10_000,
        "running": False,
        "awaiting": "any",
        "chapter1_done": True,
        "last_conversion": 0.2,
        "last_attack_in_ten": 7,
    },
}

# The success is logged before the token check: probe 4 catches it.
_SUCCESS_BEFORE_CHECK = (
    '    ev = sg_event(request, body.user, body.signalgate, "playground.success")\n'
    "    if ev:\n"
    "        sg().log(ev)\n"
    "    if not tokens.verify(body.user, body.token):\n"
    '        raise HTTPException(status_code=400, detail="invalid_token")\n'
)


def _wrong_routes() -> str:
    right = CHAPTER_3_ROUTES.read_text()
    confirm_body = right[right.index("    if not tokens.verify") :]
    confirm_body = confirm_body[: confirm_body.index('    return {"ok": True}')]
    assert "sg().log(ev)" in confirm_body
    return right.replace(confirm_body, _SUCCESS_BEFORE_CHECK)


def _line(key: str, **params: object) -> str:
    """The first line of a copy entry, as the terminal shows it."""
    return text(key, **params).splitlines()[0]


def test_chapters_2_to_6_on_a_real_terminal(open_terminal, tracked_copy, tmp_path):
    script = Script(DASHBOARD, advance_on_poll=True, hold_until_actions=G1_MIN_ACTIONS)
    fake = FakeServer(script)
    routes = tracked_copy / "app" / "routes.py"
    (tracked_copy / "app" / ".env").write_text(f"SIGNALGATE_API_KEY={E2E_KEY}\n")
    state = tracked_copy / ".playground" / "state.json"
    state.parent.mkdir(parents=True, exist_ok=True)
    state.write_text(json.dumps(OLDER_SAVE))

    with serve_fake(fake) as url:
        seam = Seam.create(tmp_path / "seam", fake_url=url)
        terminal = open_terminal(seam.env(tracked_copy), "--size", str(SIZE))
        terminal.expect("Welcome back. Type /continue", READY_WITHIN_S)

        # Chapter 2: the key, consent (no, then yes), the session.
        terminal.type("/continue")
        terminal.expect(text("ch2.key.ask"), REPLY_WITHIN_S)
        terminal.type("/hint")
        terminal.expect("replace PASTE_YOUR_API_KEY_HERE", REPLY_WITHIN_S)
        terminal.type("yes")
        terminal.expect(text("ch2.key.ok"), STEP_WITHIN_S)
        terminal.expect(text("ch2.git.no_git"), REPLY_WITHIN_S)
        terminal.expect("Do you agree? (yes/no)", REPLY_WITHIN_S)
        terminal.type("no")
        terminal.expect(text("reply.no"), REPLY_WITHIN_S)
        terminal.type("yes")
        terminal.expect(text("ch2.session.ok"), STEP_WITHIN_S)
        terminal.expect(text("app.restarted_key"), READY_WITHIN_S)
        terminal.expect(text("achievement.connected"), REPLY_WITHIN_S)

        # Chapter 3: status and hints, a mistake the gate names, then the fix.
        terminal.expect(text("ch3.ask"), REPLY_WITHIN_S)
        terminal.type("/status")
        terminal.expect(text("status.chapter", chapter=3, title=text("chapter.3")), REPLY_WITHIN_S)
        terminal.expect(text("status.done_ch2"), REPLY_WITHIN_S)
        for level in (1, 2, 3):
            terminal.type("/hint")
            terminal.expect(_line(f"hint.ch3.{level}"), REPLY_WITHIN_S)

        routes.write_text(_wrong_routes())
        terminal.expect(text("app.reloaded"), READY_WITHIN_S)
        terminal.type("yes")
        terminal.expect("✗ " + text("finding.SUCCESS_LOG_ON_FAILURE.title"), STEP_WITHIN_S)
        terminal.expect("Why: " + text("finding.SUCCESS_LOG_ON_FAILURE.why"), REPLY_WITHIN_S)
        terminal.expect(text("ch3.again"), REPLY_WITHIN_S)

        shutil.copyfile(CHAPTER_3_ROUTES, routes)
        terminal.expect(text("app.reloaded"), READY_WITHIN_S)
        terminal.type("yes")
        terminal.expect("proven by", STEP_WITHIN_S)
        terminal.expect(text("achievement.ch3"), REPLY_WITHIN_S)
        terminal.expect(text("ch3.done"), STEP_WITHIN_S)
        terminal.expect(text("achievement.learned"), REPLY_WITHIN_S)

        # Chapter 4: the dry run (Enter goes on, as yes does), the frozen
        # prediction, Start.
        terminal.expect(text("ch4.ask"), REPLY_WITHIN_S)
        terminal.type("")
        terminal.expect("conv now 20.0% → conv pred 60.0%", STEP_WITHIN_S)
        terminal.expect(text("achievement.prediction"), REPLY_WITHIN_S)
        terminal.expect(text("ch4.start"), REPLY_WITHIN_S)
        terminal.type("yes")

        # Chapter 5: protection turns on while the check() gate goes in.
        terminal.expect(text("chapter.heading", chapter=5, title=text("chapter.5")), STEP_WITHIN_S)
        script.set_workflow(PROTECTING_STAGE_1)
        terminal.expect(text("ch5.live"), STEP_WITHIN_S)
        terminal.expect(_line("ch5.ask"), REPLY_WITHIN_S)
        terminal.type("/status")
        terminal.expect(text("status.workflow", state="active"), STEP_WITHIN_S)
        terminal.expect(text("stage.partial", stage=1), REPLY_WITHIN_S)
        shutil.copyfile(CHAPTER_5_ROUTES, routes)
        terminal.expect(text("app.reloaded"), READY_WITHIN_S)
        terminal.type("yes")
        terminal.expect(text("achievement.ch5"), STEP_WITHIN_S)
        terminal.expect(text("ch5.readiness.live"), STEP_WITHIN_S)
        terminal.expect(text("ch5.done"), STEP_WITHIN_S)

        # Chapter 6: the results at stage 1, no re-run, don't keep the workflow.
        terminal.expect(text("ch6.ask"), REPLY_WITHIN_S)
        terminal.type("yes")
        terminal.expect("Real users blocked", STEP_WITHIN_S)
        terminal.expect(text("stage.partial", stage=1), REPLY_WITHIN_S)
        terminal.expect(text("results.framing"), REPLY_WITHIN_S)
        terminal.expect(text("achievement.results"), REPLY_WITHIN_S)
        terminal.expect(text("ch6.rerun.offer"), REPLY_WITHIN_S)
        terminal.type("no")
        terminal.expect(_line("ch6.keep"), REPLY_WITHIN_S)
        terminal.type("no")
        terminal.expect(text("ch6.paused"), STEP_WITHIN_S)
        terminal.expect(_line("complete.card.untimed"), REPLY_WITHIN_S)  # the card's title
        terminal.expect(_line("ch6.handoff"), REPLY_WITHIN_S)
        terminal.expect(text("ch6.done"), REPLY_WITHIN_S)
        terminal.type("/quit")
        terminal.expect("Bye.", REPLY_WITHIN_S)
        assert terminal.wait_exit(EXIT_WITHIN_S) == 0

    assert "Traceback" not in terminal.text
    assert E2E_KEY not in terminal.raw
    for path in (tracked_copy / ".playground").rglob("*"):
        if path.is_file():
            assert E2E_KEY not in path.read_text(encoding="utf-8", errors="replace"), path
    assert [c["keep_workflow"] for c in fake.completions] == [False]
    assert fake.telemetry == [
        {
            "session_id": fake.completions[0]["session_id"],
            "chapter": 3,
            "finding_ids": ["SUCCESS_LOG_ON_FAILURE"],
        }
    ]
    assert apps_left_running(tracked_copy) == set()
    lock = filelock.FileLock(str(tracked_copy / ".playground" / "console.lock"), timeout=0)
    lock.acquire()
    lock.release()
    saved = json.loads(state.read_text())
    assert saved["v"] == 2 and saved["machine"]["step"] == "DONE"
