"""Edge case: a chapter-1 run that finishes with zero actions.

If the sample app dies before it accepts a single ``/api/action`` call --
killed the instant chapter 1 starts, or crashed before the console noticed --
``run_offline`` still returns a completed (non-stopped) ``RunResult`` with
``actions == 0`` and ``successes == 0`` (see ``playground.traffic.driver``:
every visitor's request fails fast with ``httpx.HTTPError`` and is tallied as
"done" without ever incrementing ``actions``). The runner feeds that result to
the machine as ``RunFinished``, exactly like a normal completed run.

``Machine.on_event`` must handle that case without raising: a crashed app is
an expected failure mode, not a Python-level crash that dumps a traceback
(with local file paths) to the user's terminal. Interactively, the console
stays open and offers ``/start`` again; unattended (``--yes``), the session
exits with the "integration check failed" code.
"""

from __future__ import annotations

from playground.machine.events import AppReady, AwaitInput, Exit, RunFinished, Say
from playground.machine.machine import Machine
from tests.machine._scenarios import running_machine

ZERO_ACTION_RUN = RunFinished(total=10_000, actions=0, successes=0, attack=7_297, real=2_703)


def test_run_finished_with_zero_actions_does_not_raise():
    machine = running_machine()

    events = machine.on_event(ZERO_ACTION_RUN)

    assert [e.key for e in events if isinstance(e, Say)] == ["app.crashed", "run.again"]
    assert events[-1] == AwaitInput("start")
    assert machine.to_dict()["running"] is False


def test_run_finished_with_zero_actions_unattended_exits_2():
    machine = Machine(unattended=True)
    machine.on_event(AppReady())
    machine.handle("/start")

    events = machine.on_event(ZERO_ACTION_RUN)

    assert [e.key for e in events if isinstance(e, Say)] == ["app.crashed"]
    assert events[-1] == Exit(2)
