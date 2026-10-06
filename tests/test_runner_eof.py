"""Edge case: Ctrl-D (EOF) at the interactive prompt.

``prompt_toolkit``'s ``PromptSession.prompt_async`` raises ``EOFError`` when
the user presses Ctrl-D on an empty input line -- the standard way a terminal
signals end-of-input. It must end the session the way Ctrl-C or ``/quit`` does, never with a
traceback. That holds at the wait that offers to carry on after the
session closed too, and the progress stays saved, with the old session.
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx

from playground import runner, statefile
from playground.api import key as api_key
from playground.api.client import CONSENT_VERSION, PlaygroundApi
from playground.machine.events import AwaitInput
from playground.machine.machine import Machine
from playground.machine.steps import question
from playground.runner import _next_answer
from tests.fake_server import FakeServer
from tests.machine.test_online_steps import at_dry_run
from tests.test_runner_reload import WITHIN_S, FakeApp, FakeWatcher, Recorder


async def _eof_read_line(_expect):
    raise EOFError()


def test_eof_at_the_prompt_does_not_crash_the_session():
    async def scenario() -> str | None:
        quit_now = asyncio.Event()
        return await _next_answer(
            "yes_no",
            read_line=_eof_read_line,
            yes=False,
            until=None,
            quit_now=quit_now,
        )

    answer = asyncio.run(scenario())
    assert answer is None, "EOF should behave like the user quit, not crash"


# -- Ctrl-D at the wait that offers to carry on after the session closed ----------------------

KEY = api_key.KEY_PREFIX + "Eof1" * 8
SIZE = 1100


class _AtTheDryRun:
    """The real machine, asked chapter 4's question when the session starts.

    Everything but the start is the machine's own.
    """

    def __init__(self, machine: Machine) -> None:
        self._machine = machine

    def start(self) -> list[Any]:
        return [question("ch4.ask"), AwaitInput("continue")]

    def __getattr__(self, name: str) -> Any:
        return getattr(self._machine, name)


def test_eof_at_the_carry_on_wait_quits_and_keeps_the_progress(tmp_path, monkeypatch):
    monkeypatch.delenv(api_key.KEY_NAME, raising=False)
    root = tmp_path / "repo"
    (root / "app").mkdir(parents=True)
    (root / "app" / "main.py").write_text("")
    (root / "app" / ".env").write_text(f"SIGNALGATE_API_KEY={KEY}\n")
    state_path = root / runner.STATE_RELATIVE
    fake = FakeServer()
    transport = httpx.ASGITransport(app=fake.app)
    renderer = Recorder()
    reads: list[str] = []

    async def read_line(expect: str) -> str:
        reads.append(expect)
        if len(reads) == 1:
            return ""  # Enter at chapter 4's question: the dry run wait starts
        raise EOFError  # Ctrl-D at the wait that offers to carry on

    async def scenario() -> int:
        async with PlaygroundApi(KEY, transport=transport) as api:
            opened = await api.create_session(SIZE, CONSENT_VERSION)
            # A newer session from the same key closes the console's.
            await api.create_session(SIZE, CONSENT_VERSION)
        snapshot = at_dry_run().to_dict()
        session = {"session_id": opened["session_id"], "size": SIZE, "limits": opened["limits"]}
        machine = Machine.from_dict({**snapshot, "session": session})
        return await asyncio.wait_for(
            runner.run(
                _AtTheDryRun(machine),  # type: ignore[arg-type]
                renderer,
                read_line,
                root=root,
                state_path=state_path,
                interactive=True,
                app=FakeApp(),  # type: ignore[arg-type]
                watcher=FakeWatcher(),  # type: ignore[arg-type]
                api_transport=transport,
            ),
            WITHIN_S,
        )

    assert asyncio.run(scenario()) == 0
    assert renderer.say_keys() == ["ch4.ask", "online.session_gone"]
    assert reads == ["continue", "continue"]
    saved, notice = statefile.load(state_path)
    assert notice is None and saved is not None
    assert saved["step"] == "DRY_RUN" and saved["awaiting"] == "continue"
    assert saved["running"] is False
    first, newer = fake.sessions()
    assert saved["session"]["session_id"] == first.sid
    assert (first.ended_by, newer.ended_by) == ("replaced", None)
