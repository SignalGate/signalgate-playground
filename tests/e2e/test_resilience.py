"""Saving a file mid-run: the restart waits for the run, and no queued log is lost.

A scripted ``--ci --yes --until 1`` session runs chapter 1 against a sample
app that logs one action in ten through the SDK (answered locally by the
start-up hook, with deliveries held until the test lets them through). The
test saves ``app/routes.py`` once the run is under way and then checks that:

- the console says once that the reload waits, and the app keeps its
  process until the run's tally is out;
- after the run it waits for the app's queued logs (the line that offers
  to skip the wait), and they all go out once the test releases them;
- the restart comes after the close question, without a reload line (chapter
  1 isn't a step where the user edits the code), and the session still ends
  with exit code 0;
- the old app's SDK client closed with every queued log sent and none
  dropped, each delivered once;
- neither the dummy key nor the dummy ciphertext shows up in any output.
"""

from __future__ import annotations

import json
from collections import Counter

import pytest

from playground.runner import CHAPTER1_VISITORS
from playground.supervisor.control_client import (
    KEY_DROPPED_CLOSED,
    KEY_DROPPED_QUEUE_FULL,
    KEY_DROPPED_RETRY,
    KEY_ENQUEUED,
    KEY_SENT,
)
from tests.e2e.conftest import (
    DUMMY_KEY,
    EXIT_WITHIN_S,
    LOGGED_ONE_IN,
    LOGGING_ROUTES,
    READY_WITHIN_S,
    RUN_WITHIN_S,
    append_comment,
    apps_left_running,
    use_logging_app,
)

DUMMY_CIPHERTEXT = "e2e-dummy-ciphertext-marker"

pytestmark = pytest.mark.e2e


def test_a_save_mid_run_defers_the_restart_and_drops_no_log(
    tracked_copy, open_ci_console, gated_seam
):
    fixture_source = LOGGING_ROUTES.read_text()
    assert DUMMY_CIPHERTEXT in fixture_source
    assert f"LOG_EVERY = {LOGGED_ONE_IN}" in fixture_source
    use_logging_app(tracked_copy)
    routes = tracked_copy / "app" / "routes.py"
    console = open_ci_console(gated_seam.env(tracked_copy), "--yes", "--until", "1")

    console.expect(READY_WITHIN_S, type="progress")
    (boot1,) = gated_seam.wait_for_boots(1, READY_WITHIN_S)
    append_comment(routes)

    deferred = console.expect(RUN_WITHIN_S, type="say", key="reload.deferred")
    assert deferred["text"].startswith("change detected")
    assert gated_seam.pids() == [boot1["pid"]]

    # The run is over and its logs are still held: the console waits for
    # them, offering to skip the wait (no Ctrl-C has been pressed).
    waiting = console.expect(RUN_WITHIN_S, type="say")
    while waiting["key"] not in ("drain.waiting", "drain.waiting_quit", "ch1.close"):
        waiting = console.expect(RUN_WITHIN_S, type="say")
    assert waiting["key"] == "drain.waiting", waiting
    assert "Ctrl-C to skip" in waiting["text"]
    assert gated_seam.pids() == [boot1["pid"]]
    # Released at once: well inside the drain's own stall bound.
    gated_seam.release()

    close = console.expect(RUN_WITHIN_S, type="say", key="ch1.close")
    assert "20.0%" in close["text"]
    # The tally is out and the app it ran against is still the only one.
    assert gated_seam.pids() == [boot1["pid"]]

    assert console.wait_exit(RUN_WITHIN_S + EXIT_WITHIN_S) == 0, console.stderr
    assert apps_left_running(tracked_copy) == set()

    keys = console.say_keys()
    assert keys.count("reload.deferred") == 1
    # The run said the reload would wait; once it is in, that is said once.
    assert keys.count("app.reloaded") == 1
    assert keys.index("app.reloaded") > keys.index("ch1.close")
    assert "drain.skipped" not in keys
    assert "drain.gave_up" not in keys

    # The save restarted the app once, after the close question (above, the
    # app was still boot 1 when the close was said).
    boots = gated_seam.boots()
    assert boots[0]["pid"] == boot1["pid"]
    assert len(boots) == 2

    # Boot 1 closed its SDK client with every queued log sent.
    closed = gated_seam.closed(boot1["pid"])
    assert closed, "boot 1 never closed its SDK client"
    metrics = closed[0]["metrics"]
    enqueued = metrics.get(KEY_ENQUEUED, 0)
    # Every action of the run logged through boot 1: none went to boot 2.
    assert enqueued == CHAPTER1_VISITORS // LOGGED_ONE_IN
    assert enqueued == metrics.get(KEY_SENT, 0) + metrics.get(KEY_DROPPED_RETRY, 0)
    assert metrics.get(KEY_DROPPED_CLOSED, 0) == 0
    assert metrics.get(KEY_DROPPED_QUEUE_FULL, 0) == 0

    # Each logged action was delivered exactly once.
    deliveries = gated_seam.deliveries()
    keys_seen = Counter(line["idempotency_key"] for line in deliveries)
    assert len(deliveries) == enqueued
    assert all(count == 1 for count in keys_seen.values())
    assert len({line["nonce"] for line in deliveries}) == enqueued
    assert {line["method"] for line in deliveries} == {"playground.action"}

    # No secret, and no envelope contents, anywhere the console or app wrote.
    outputs = {
        "stdout": "\n".join(json.dumps(line) for line in console.lines),
        "stderr": console.stderr,
    }
    for path in (tracked_copy / ".playground").iterdir():
        if path.is_file():
            outputs[path.name] = path.read_text(errors="replace")
    assert "app.log" in outputs
    for name, content in outputs.items():
        assert DUMMY_KEY not in content, name
        assert DUMMY_CIPHERTEXT not in content, name

    # An unattended run never writes saved progress.
    assert not (tracked_copy / ".playground" / "state.json").exists()
