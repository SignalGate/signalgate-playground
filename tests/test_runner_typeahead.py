"""Keys typed while the console was busy never answer the next question.

On a terminal, an Enter pressed while a check or a run is going on waits in
the input queue, and would answer the question that follows by itself. The
runner is handed a way to drop such keys (``discard_typeahead``) and calls
it each time an effect or a run ends, before any of its outcome is on
screen, so whatever is typed once the outcome shows is kept. Answers that
start nothing drop nothing, and without the hook (``--ci``, a pipe) every
line is an answer in order.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from playground import runner
from playground.machine.events import AwaitInput, CopySnippets, Exit, Say, StepDone
from tests.test_runner_reload import WITHIN_S, FakeApp, FakeWatcher, Recorder, Session


def _run(scenario: Any) -> None:
    asyncio.run(scenario())


def test_keys_typed_during_an_effect_are_dropped_before_its_outcome_shows(tmp_path, monkeypatch):
    async def scenario() -> None:
        session = Session(tmp_path, monkeypatch)
        # What was on screen each time the keys typed meanwhile were dropped.
        dropped_at: list[list[str]] = []
        session.discard_typeahead = lambda: dropped_at.append(session.renderer.say_keys())
        session.start()

        await session.opened()
        # The app started: dropped before the opening asks for /start.
        assert dropped_at == [["app.starting"]]

        session.keyboard.type("/help")  # an answer that starts nothing
        await session.said("help")
        assert len(dropped_at) == 1

        session.keyboard.type("/start")
        await session.said("ch1.close")
        # The run and its drain are over: dropped before the tally and its question.
        assert len(dropped_at) == 2
        assert dropped_at[1][-1] == "ch1.intro"
        assert "ch1.close" not in dropped_at[1]

        session.keyboard.type(None)
        assert await session.finish() == 0
        assert len(dropped_at) == 2  # the way out drops nothing

    _run(scenario)


class _CopyThenAsk:
    """Just enough of a machine: one effect, then a question, then the end."""

    unattended_no = False
    elapsed_s = None
    session = None

    def start(self) -> list[Any]:
        return [CopySnippets("ch3.task")]

    def handle(self, _answer: str) -> list[Any]:
        return [Exit(0)]

    def on_event(self, event: Any) -> list[Any]:
        assert isinstance(event, StepDone)
        return [*event.says, Say("ch3.ask"), AwaitInput("continue")]


def _copy_session(tmp_path: Path, *, hook: bool) -> tuple[Recorder, list[list[str]]]:
    """Run ``_CopyThenAsk`` with no clipboard tool (so the code goes to a
    file); with ``hook``, note what was on screen each time keys were dropped."""
    root = tmp_path / "repo"
    (root / "app").mkdir(parents=True)
    (root / "app" / "main.py").write_text("")
    renderer = Recorder()
    dropped_at: list[list[str]] = []

    async def read_line(_expect: str) -> str | None:
        return None  # Ctrl-C at the question that follows

    async def go() -> int:
        return await asyncio.wait_for(
            runner.run(
                _CopyThenAsk(),  # type: ignore[arg-type]
                renderer,
                read_line,
                root=root,
                interactive=True,
                app=FakeApp(),  # type: ignore[arg-type]
                watcher=FakeWatcher(),  # type: ignore[arg-type]
                discard_typeahead=(lambda: dropped_at.append(renderer.say_keys()))
                if hook
                else None,
            ),
            WITHIN_S,
        )

    assert asyncio.run(go()) == 0
    return renderer, dropped_at


def test_an_online_effect_drops_the_keys_before_its_answer_shows(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", str(tmp_path / "no-tools"))
    shown, dropped_at = _copy_session(tmp_path, hook=True)
    assert dropped_at == [[]]
    assert shown.say_keys() == ["copy.file", "ch3.ask"]


def test_without_the_hook_the_session_runs_as_before(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", str(tmp_path / "no-tools"))
    shown, dropped_at = _copy_session(tmp_path, hook=False)
    assert dropped_at == []
    assert shown.say_keys() == ["copy.file", "ch3.ask"]
