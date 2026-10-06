"""End-to-end tests for the whole console in ``--ci`` mode, run as a real
subprocess against a temp copy of the tracked tree (see ``conftest.py``):

- a scripted chapter 1 run that touches no real network, leaves nothing
  running and never touches saved progress;
- a scripted run without ``--yes``, which reads the sample app's health at
  the close question: the app never built an SDK client, and its boot
  self-test passed;
- SIGTERM while the console waits for an answer on an open stdin;
- the lock that refuses a second console.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time

import filelock
import pytest

from playground.copy.en import text
from tests.e2e.conftest import (
    EXIT_WITHIN_S,
    READY_WITHIN_S,
    RUN_WITHIN_S,
    apps_left_running,
    plain_env,
)
from tests.e2e.seam import Seam, read_health

DEAD_PROXY = "http://127.0.0.1:9"  # nothing listens here; a poisoned proxy

# What a scripted --yes run to the end of chapter 1 says, in order, and
# nothing else: the automatic yes at the close earns chapter 1's achievement.
SCRIPTED_SAYS = [
    "console.env_created",
    "app.starting",
    "app.ready",
    "opening",
    "chapter.heading",
    "ch1.intro",
    "ch1.close",
    "achievement.ch1",
]

SELF_TEST_CHECKS = (
    "sdk_version",
    "transport_import",
    "transport_signature",
    "transport_protocol",
    "facade",
    "client_hook",
    "probe_hooks",
    "exit_hook",
)

# A saved session a --yes run must neither offer nor overwrite.
_SAVED_SESSION = {
    "v": 1,
    "machine": {
        "offline_total": 10_000,
        "running": False,
        "awaiting": "yes_no",
        "chapter1_done": False,
        "last_conversion": 0.2,
        "last_attack_in_ten": 8,
    },
}


def _parse_json_lines(output: str) -> list[dict]:
    lines = [line for line in output.splitlines() if line.strip()]
    parsed = [json.loads(line) for line in lines]
    assert parsed, "the console printed no JSON lines"
    return parsed


@pytest.mark.e2e
def test_scripted_chapter1_run_touches_no_network_and_leaves_nothing_running(
    tracked_copy, no_network_hook_dir
):
    env_path = tracked_copy / "app" / ".env"
    assert not env_path.exists()
    state_path = tracked_copy / ".playground" / "state.json"
    state_path.parent.mkdir(parents=True)
    state_path.write_text(json.dumps(_SAVED_SESSION))
    saved_before = state_path.read_bytes()

    env = plain_env(
        tracked_copy,
        no_network_hook_dir,
        {"HTTP_PROXY": DEAD_PROXY, "HTTPS_PROXY": DEAD_PROXY, "ALL_PROXY": DEAD_PROXY},
    )
    started = time.monotonic()
    result = subprocess.run(
        [sys.executable, "-m", "playground", "--ci", "--yes", "--until", "1"],
        cwd=tracked_copy,
        env=env,
        capture_output=True,
        text=True,
        timeout=RUN_WITHIN_S,
        check=False,
    )
    elapsed = time.monotonic() - started
    print(f"--ci --yes --until 1: {elapsed:.2f}s (budget {RUN_WITHIN_S:.0f}s)")

    assert result.returncode == 0, result.stderr
    assert elapsed <= RUN_WITHIN_S

    lines = _parse_json_lines(result.stdout)
    says = [line for line in lines if line.get("type") == "say"]
    assert [line["key"] for line in says] == SCRIPTED_SAYS
    # Every line says which block it belongs to, and the intro names the
    # sample app the visitors go to.
    assert all(isinstance(line.get("block"), str) for line in says), says
    (intro,) = [line for line in says if line["key"] == "ch1.intro"]
    assert intro["text"] == text("ch1.intro", total=10_000)
    (close,) = [line for line in says if line["key"] == "ch1.close"]
    assert "20.0%" in close["text"]
    assert close["block"] == "ask"
    assert says[-1]["text"] == text("achievement.ch1")
    assert [line["expect"] for line in lines if line.get("type") == "await"] == [
        "start",
        "yes_no",
    ]

    assert env_path.exists()
    assert apps_left_running(tracked_copy) == set()
    # An unattended run never reads or writes saved progress.
    assert state_path.read_bytes() == saved_before
    assert sorted(path.name for path in state_path.parent.glob("state.json*")) == ["state.json"]


@pytest.mark.e2e
def test_a_scripted_run_without_yes_finds_the_app_never_built_a_client(
    tracked_copy, open_ci_console, tmp_path
):
    seam = Seam.create(tmp_path / "seam", stub=False)
    console = open_ci_console(seam.env(tracked_copy))

    console.expect(READY_WITHIN_S, type="await", expect="start")
    console.send("/start")
    close = console.expect(RUN_WITHIN_S, type="say", key="ch1.close")
    assert "20.0%" in close["text"]
    console.expect(RUN_WITHIN_S, type="await", expect="yes_no")

    # Parked at the close question: the sample app answered every visitor
    # without ever building a SignalGate client, and its self-test passed.
    (boot,) = seam.wait_for_boots(1, READY_WITHIN_S)
    health = read_health(boot)
    assert health["clients_created"] == 0
    assert health["client_ready"] is False
    assert health["self_test"] == {
        "ok": True,
        "failed": [],
        "checks": dict.fromkeys(SELF_TEST_CHECKS, "pass"),
    }

    console.send("yes")
    console.expect(RUN_WITHIN_S, type="say", key="ch2.key.ask")
    console.expect(RUN_WITHIN_S, type="await", expect="continue")
    console.send("/quit")
    assert console.wait_exit(EXIT_WITHIN_S) == 0, console.stderr
    keys = console.say_keys()
    assert keys[-2:] == ["console.saved", "console.bye"]
    assert (tracked_copy / ".playground" / "state.json").exists()
    assert "Traceback" not in console.stderr
    assert apps_left_running(tracked_copy) == set()


@pytest.mark.e2e
def test_sigterm_while_waiting_for_an_answer_exits_cleanly(tracked_copy, open_ci_console, seam):
    # No --yes: the console waits at the opening with its stdin held open.
    console = open_ci_console(seam.env(tracked_copy))
    console.expect(READY_WITHIN_S, type="await", expect="start")
    (boot,) = seam.wait_for_boots(1, READY_WITHIN_S)

    console.process.send_signal(signal.SIGTERM)
    started = time.monotonic()
    assert console.wait_exit(EXIT_WITHIN_S) == 0, console.stderr
    assert time.monotonic() - started <= EXIT_WITHIN_S
    assert "Traceback" not in console.stderr
    # The app it started is gone (the fixture's teardown checks again).
    assert apps_left_running(tracked_copy) == set()
    with pytest.raises(ProcessLookupError):
        os.kill(boot["pid"], 0)


@pytest.mark.e2e
def test_second_console_is_refused_while_the_first_holds_the_lock(tracked_copy):
    lock_path = tracked_copy / ".playground" / "console.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    held = filelock.FileLock(str(lock_path))
    held.acquire()
    try:
        result = subprocess.run(
            [sys.executable, "-m", "playground", "--ci"],
            cwd=tracked_copy,
            env=dict(os.environ),
            capture_output=True,
            text=True,
            timeout=READY_WITHIN_S,
            check=False,
        )
    finally:
        held.release()

    assert result.returncode == 3
    assert text("console.lock_held") in result.stderr
