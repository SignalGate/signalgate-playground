"""A session closed mid-chapter carries on with Enter, against the test-only fake.

While the console runs chapter 3, the test opens a session with the
console's own key from outside, as a second console with the same key
would. The fake then closes the console's session, as the playground API
does, so the console's next call finds it closed. The console says so and
waits for Enter (or ``yes``): then it opens a new session, with no new
consent, and runs the step that found the old one closed again. Progress
stays where it was.

Three ways: on a real terminal, during the learning run; in ``--ci`` with a
scripted answer, at the code gate; and in ``--ci --yes``, which never opens
a session by itself again: it says the session closed and exits 2.
"""

from __future__ import annotations

import json
import shutil
import sys
import time

import httpx
import pytest

from playground import CLIENT_VERSION, statefile
from playground.api.client import CONSENT_VERSION, PROTOCOL_HEADER, PROTOCOL_VERSION
from playground.copy.en import text
from tests.contract.playground_api import SESSIONS_PATH
from tests.e2e.conftest import (
    EXIT_WITHIN_S,
    PROMPT,
    READY_WITHIN_S,
    REPLY_WITHIN_S,
    apps_left_running,
)
from tests.e2e.fake_host import E2E_KEY, serve_fake
from tests.e2e.seam import Seam
from tests.e2e.test_interactive_online import OLDER_SAVE, STEP_WITHIN_S
from tests.e2e.test_online import CHAPTER_3_ROUTES
from tests.fake_server import FakeServer

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(sys.platform == "win32", reason="needs a POSIX pseudo-terminal"),
]

# Visitors per run for the unattended run, which starts afresh.
SIZE = 1100
# Chapter 1 offline, then chapter 2 and chapter 3's code gate.
UNATTENDED_WITHIN_S = 120.0


def _line(key: str, **params: object) -> str:
    """The first line of a copy entry, as the console shows it."""
    return text(key, **params).splitlines()[0]


def _open_a_session_with_the_consoles_key(url: str) -> None:
    """Open a session with the console's key, from outside the console."""
    body = {"size": SIZE, "client_version": CLIENT_VERSION, "consent_version": CONSENT_VERSION}
    headers = {PROTOCOL_HEADER: PROTOCOL_VERSION, "Authorization": f"Bearer {E2E_KEY}"}
    with httpx.Client(base_url=url, trust_env=False, timeout=REPLY_WITHIN_S) as client:
        response = client.post(SESSIONS_PATH, json=body, headers=headers)
    assert response.status_code == 201, response.status_code


def _start_from_chapter_1_done(tree) -> int:
    """The key in place and chapter 1 saved as done; the visitors per run it resumes with.

    Saved progress keeps its own run size, whatever ``--size`` says.
    """
    (tree / "app" / ".env").write_text(f"SIGNALGATE_API_KEY={E2E_KEY}\n")
    state = tree / ".playground" / "state.json"
    state.parent.mkdir(parents=True, exist_ok=True)
    state.write_text(json.dumps(OLDER_SAVE))
    saved, notice = statefile.load(state)
    assert saved is not None and notice is None
    return saved["size"]


def _no_key_in_saved_files(tree) -> None:
    for path in (tree / ".playground").rglob("*"):
        if path.is_file():
            assert E2E_KEY not in path.read_text(encoding="utf-8", errors="replace"), path


def test_a_session_closed_during_the_learning_run_carries_on_with_enter(
    open_terminal, tracked_copy, tmp_path
):
    fake = FakeServer()
    routes = tracked_copy / "app" / "routes.py"
    size = _start_from_chapter_1_done(tracked_copy)

    with serve_fake(fake) as url:
        seam = Seam.create(tmp_path / "seam", fake_url=url)
        terminal = open_terminal(seam.env(tracked_copy))
        terminal.expect("Welcome back. Type /continue", READY_WITHIN_S)

        # Chapter 2: the key, consent, the session.
        terminal.type("/continue")
        terminal.expect(text("ch2.key.ask"), REPLY_WITHIN_S)
        terminal.type("yes")
        terminal.expect(text("ch2.git.no_git"), STEP_WITHIN_S)
        terminal.expect("Do you agree? (yes/no)", REPLY_WITHIN_S)
        terminal.type("yes")
        terminal.expect(text("ch2.session.ok"), STEP_WITHIN_S)
        terminal.expect(text("achievement.connected"), READY_WITHIN_S)

        # Chapter 3: the code is in, the gate passes, the learning run starts.
        terminal.expect(text("ch3.ask"), REPLY_WITHIN_S)
        shutil.copyfile(CHAPTER_3_ROUTES, routes)
        terminal.expect(text("app.reloaded"), READY_WITHIN_S)
        terminal.type("yes")
        terminal.expect(_line("ch3.learn.intro", size=size), STEP_WITHIN_S)

        # Someone opens a session with the same key: the run finds its own closed.
        _open_a_session_with_the_consoles_key(url)
        terminal.expect(text("online.session_gone"), STEP_WITHIN_S)
        terminal.expect(PROMPT, REPLY_WITHIN_S)
        terminal.type("")
        terminal.expect(text("ch2.session.ok"), STEP_WITHIN_S)
        terminal.expect(text("ch3.done"), STEP_WITHIN_S)

        terminal.expect(text("ch4.ask"), REPLY_WITHIN_S)
        terminal.type("/quit")
        terminal.expect("Bye.", REPLY_WITHIN_S)
        assert terminal.wait_exit(EXIT_WITHIN_S) == 0

    assert "Traceback" not in terminal.text
    assert E2E_KEY not in terminal.raw
    _no_key_in_saved_files(tracked_copy)
    # The console's first session, the one opened from outside, then the
    # console's second: open, and the whole learning run ran on it again.
    first, outside, second = fake.sessions()
    assert (first.open, first.ended_by) == (False, "replaced")
    assert (outside.open, outside.ended_by) == (False, "replaced")
    assert (second.open, second.ended_by) == (True, None)
    assert second.leased["learn"] >= size
    assert terminal.count(text("ch2.consent").splitlines()[0]) == 1
    saved = json.loads((tracked_copy / ".playground" / "state.json").read_text())
    assert saved["machine"]["step"] == "DRY_RUN"
    assert saved["machine"]["session"]["session_id"] == second.sid
    assert apps_left_running(tracked_copy) == set()


def test_a_scripted_answer_carries_on_at_the_code_gate(tracked_copy, tmp_path, open_ci_console):
    fake = FakeServer()
    shutil.copyfile(CHAPTER_3_ROUTES, tracked_copy / "app" / "routes.py")
    size = _start_from_chapter_1_done(tracked_copy)

    with serve_fake(fake) as url:
        seam = Seam.create(tmp_path / "seam", fake_url=url)
        console = open_ci_console(seam.env(tracked_copy))
        console.expect(READY_WITHIN_S, type="await", expect="start")
        console.send("/continue")
        console.expect(REPLY_WITHIN_S, type="await", expect="continue")
        console.send("yes")
        console.expect(STEP_WITHIN_S, type="await", expect="yes_no")
        console.send("yes")

        # The code step's question is out: a session opens with the same key.
        console.expect(STEP_WITHIN_S, type="say", key="ch3.ask")
        _open_a_session_with_the_consoles_key(url)
        console.expect(REPLY_WITHIN_S, type="await", expect="continue")
        console.send("yes")
        gone = console.expect(STEP_WITHIN_S, type="say", key="online.session_gone")
        assert gone["block"] == "ask"
        assert gone["text"] == text("online.session_gone")
        waiting = console.next_line(REPLY_WITHIN_S)
        assert waiting is not None and (waiting["type"], waiting["expect"]) == ("await", "continue")
        carry_on_at = len(console.lines)
        console.send("")

        # The new session, then the code gate again, and on through the learning run.
        console.expect(STEP_WITHIN_S, type="say", key="ch2.session.ok")
        console.expect(STEP_WITHIN_S, type="say", key="verify.line")
        console.expect(STEP_WITHIN_S, type="say", key="ch3.done")
        console.expect(STEP_WITHIN_S, type="await", expect="continue")
        console.send("/quit")
        code = console.wait_exit(EXIT_WITHIN_S)

    assert code == 0, (console.lines[-10:], console.stderr[-3000:])
    after = [line.get("key") for line in console.lines[carry_on_at:] if line["type"] == "say"]
    assert after.index("ch2.session.ok") < after.index("verify.line") < after.index("ch3.done")
    assert after[-2:] == ["console.saved", "console.bye"]
    keys = console.say_keys()
    assert keys.count("ch2.consent") == 1 and keys.count("online.session_gone") == 1
    first, outside, second = fake.sessions()
    assert [first.ended_by, outside.ended_by, second.ended_by] == ["replaced", "replaced", None]
    assert second.leased["probe"] > 0 and second.leased["learn"] >= size
    assert E2E_KEY not in "\n".join(str(line) for line in console.lines)
    assert E2E_KEY not in console.stderr
    _no_key_in_saved_files(tracked_copy)
    assert apps_left_running(tracked_copy) == set()


def test_an_unattended_run_stops_when_its_session_closed(tracked_copy, tmp_path, open_ci_console):
    fake = FakeServer()
    (tracked_copy / "app" / ".env").write_text(f"SIGNALGATE_API_KEY={E2E_KEY}\n")
    # Chapter 3's code gate asks for visitors first: the session is closed by then.
    fake.script.fail_next("lease", 404, "SESSION_NOT_FOUND")

    with serve_fake(fake) as url:
        seam = Seam.create(tmp_path / "seam", fake_url=url)
        started = time.monotonic()
        console = open_ci_console(seam.env(tracked_copy), "--yes", "--size", str(SIZE))
        console.expect(UNATTENDED_WITHIN_S, type="say", key="online.session_gone")
        code = console.wait_exit(UNATTENDED_WITHIN_S - (time.monotonic() - started))

    assert code == 2, (console.lines[-10:], console.stderr[-3000:])
    keys = console.say_keys()
    assert keys[-1] == "online.session_gone"
    assert keys.count("ch2.session.ok") == 1
    (only,) = fake.sessions()
    assert only.leased["probe"] == 0
    assert "Traceback" not in console.stderr
    assert E2E_KEY not in "\n".join(str(line) for line in console.lines)
    assert apps_left_running(tracked_copy) == set()
