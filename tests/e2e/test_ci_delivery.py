"""The wait for the app's queued log events, as ``--ci`` JSON lines.

A scripted ``--ci --yes --until 1`` session runs chapter 1 against a sample
app that logs one action in ten through the SDK (answered locally by the
start-up hook, which holds the deliveries until the test lets them
through). After the run the console waits for those logs, and its JSON
lines say so:

- first the ``drain.waiting`` line, as a ``say`` record, and right after
  it a ``progress`` record with ``"phase": "deliver"`` counting what was
  sent of what was queued;
- once the deliveries go through, more deliver records whose count only
  grows, the last one whole (every queued log sent);
- then ``drain.done`` ends the wait.

The run's own ``progress`` records carry no ``phase``.
"""

from __future__ import annotations

import itertools
import sys

import pytest

from playground.copy.en import text
from tests.e2e.conftest import (
    EXIT_WITHIN_S,
    READY_WITHIN_S,
    REPLY_WITHIN_S,
    RUN_WITHIN_S,
    apps_left_running,
    use_logging_app,
)

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(sys.platform == "win32", reason="POSIX subprocess handling"),
]

DELIVER = {"type": "progress", "phase": "deliver"}


def _is_deliver(line: dict) -> bool:
    return all(line.get(key) == value for key, value in DELIVER.items())


def test_the_wait_after_a_run_counts_up_to_every_log_sent(
    tracked_copy, open_ci_console, gated_seam
):
    use_logging_app(tracked_copy)
    console = open_ci_console(gated_seam.env(tracked_copy), "--yes", "--until", "1")

    # The run: its progress records count visitors and carry no phase.
    console.expect(READY_WITHIN_S, type="progress")
    waiting = console.expect(RUN_WITHIN_S, type="say", key="drain.waiting")
    assert "Ctrl-C to skip" in waiting["text"]
    run_records = [line for line in console.lines if line.get("type") == "progress"]
    assert run_records and all("phase" not in line for line in run_records)

    # Right after the first line, the first count: nothing sent yet (held).
    first = console.next_line(REPLY_WITHIN_S)
    assert first is not None and _is_deliver(first), first
    assert set(first) == {"v", "type", "phase", "done", "total"}
    assert first["v"] == 1 and first["done"] == 0 and first["total"] > 0

    gated_seam.release()

    # Deliver records only, until the line that ends the wait.
    counts = [first]
    while True:
        line = console.next_line(RUN_WITHIN_S)
        assert line is not None, console.lines[-5:]
        if line.get("type") == "say":
            break
        assert _is_deliver(line), line
        counts.append(line)
    assert line["key"] == "drain.done", line
    assert line["text"] == text("drain.done")
    assert len(counts) >= 2, counts
    for before, after in itertools.pairwise(counts):
        assert after["done"] >= before["done"] and after["total"] >= before["total"]
    assert all(0 <= count["done"] <= count["total"] for count in counts)
    assert counts[-1]["done"] == counts[-1]["total"] >= first["total"]

    console.expect(RUN_WITHIN_S, type="say", key="ch1.close")
    assert console.wait_exit(RUN_WITHIN_S + EXIT_WITHIN_S) == 0, console.stderr
    assert apps_left_running(tracked_copy) == set()

    keys = console.say_keys()
    assert keys.count("drain.waiting") == 1  # one line, then counts: never repeated
    assert "drain.skipped" not in keys and "drain.gave_up" not in keys
    assert not [line for line in console.lines if line.get("type") == "wait"]
    assert "Traceback" not in console.stderr
