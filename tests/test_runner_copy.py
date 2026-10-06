"""``/copy``: the runner carries out ``CopySnippets``, and ``Online`` copies the code.

The code goes on the clipboard when a clipboard tool takes it, else into a
file under ``.playground/snippets/``, and the answer says which. As in
``tests/test_clipboard.py``, no real clipboard tool ever runs: ``PATH``
holds only fake tools that record what they got.
"""

from __future__ import annotations

import asyncio
import stat
import sys
from pathlib import Path
from typing import Any

import pytest

from playground import runner
from playground.copy.en import TASKS, snippet_text
from playground.machine.events import AwaitInput, CopySnippets, Exit, Say, StepDone
from playground.online import Online
from tests.test_runner_reload import WITHIN_S, FakeApp, FakeWatcher, Recorder

_RECORDING = """\
import pathlib, sys
pathlib.Path(sys.argv[0]).with_suffix(".got").write_bytes(sys.stdin.buffer.read())
"""
_FAILING = "import sys\nsys.exit(1)\n"

# The clipboard tool each system tries first.
_TOOL = {"darwin": "pbcopy", "linux": "xclip"}


class _NoSession:
    """``/copy`` needs nothing from the runner's session."""


@pytest.fixture
def bin_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """An empty directory that is all of ``PATH``: no clipboard tool at all."""
    directory = tmp_path / "bin"
    directory.mkdir()
    monkeypatch.setenv("PATH", str(directory))
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    return directory


@pytest.fixture(params=sorted(_TOOL))
def platform(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> str:
    monkeypatch.setattr(sys, "platform", request.param)
    return request.param


def _tool(directory: Path, name: str, body: str = _RECORDING) -> Path:
    path = directory / name
    path.write_text(f"#!{sys.executable}\n{body}")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path.with_suffix(".got")


def _root(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / "app").mkdir(parents=True)
    return root


def _copy(root: Path, task: str, index: int | None = None) -> StepDone:
    online = Online(_NoSession(), root=root, api_transport=None, telemetry=False)  # type: ignore[arg-type]
    return asyncio.run(online.copy_snippets(task, index))


def _only(done: StepDone) -> Say:
    assert done.passed
    (say,) = done.says
    return say


# -- on the clipboard ----------------------------------------------------------------------


def test_all_chapter_3_snippets_go_on_the_clipboard(tmp_path, bin_dir, platform):
    got = _tool(bin_dir, _TOOL[platform])
    root = _root(tmp_path)
    say = _only(_copy(root, "ch3.task"))
    assert say == Say("copy.done", {"count": 3})
    assert got.read_bytes().decode("utf-8") == snippet_text("ch3.task")
    assert not (root / ".playground").exists()  # no file when the clipboard took it


def test_one_snippet_goes_on_the_clipboard_alone(tmp_path, bin_dir, platform):
    got = _tool(bin_dir, _TOOL[platform])
    say = _only(_copy(_root(tmp_path), "ch3.task", 2))
    assert say == Say("copy.done_one", {"index": 2})
    assert got.read_bytes().decode("utf-8") == snippet_text("ch3.task", 2)


def test_chapter_5_has_two_snippets(tmp_path, bin_dir, platform):
    got = _tool(bin_dir, _TOOL[platform])
    say = _only(_copy(_root(tmp_path), "ch5.task"))
    assert say == Say("copy.done", {"count": len(TASKS["ch5.task"][1])})
    assert say.params["count"] == 2
    assert got.read_bytes().decode("utf-8") == snippet_text("ch5.task")


# -- into a file ------------------------------------------------------------------------------


def test_without_a_clipboard_tool_the_code_goes_to_a_file(tmp_path, bin_dir, platform):
    root = _root(tmp_path)
    say = _only(_copy(root, "ch3.task"))
    assert say == Say("copy.file", {"path": ".playground/snippets/chapter3.py"})
    written = root / ".playground" / "snippets" / "chapter3.py"
    assert written.read_text(encoding="utf-8") == snippet_text("ch3.task")


def test_one_snippet_without_a_tool_gets_a_file_of_its_own(tmp_path, bin_dir, platform):
    root = _root(tmp_path)
    say = _only(_copy(root, "ch3.task", 2))
    assert say == Say("copy.file", {"path": ".playground/snippets/chapter3-2.py"})
    written = root / ".playground" / "snippets" / "chapter3-2.py"
    assert written.read_text(encoding="utf-8") == snippet_text("ch3.task", 2)


def test_chapter_5_without_a_tool(tmp_path, bin_dir, platform):
    root = _root(tmp_path)
    say = _only(_copy(root, "ch5.task"))
    assert say == Say("copy.file", {"path": ".playground/snippets/chapter5.py"})
    written = root / ".playground" / "snippets" / "chapter5.py"
    assert written.read_text(encoding="utf-8") == snippet_text("ch5.task")


def test_clipboard_tools_that_fail_mean_a_file_too(tmp_path, bin_dir, monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    for name in ("pbcopy", "wl-copy", "xclip"):
        _tool(bin_dir, name, _FAILING)
    root = _root(tmp_path)
    say = _only(_copy(root, "ch3.task"))
    assert say.key == "copy.file"
    assert (root / ".playground" / "snippets" / "chapter3.py").is_file()


def test_a_file_that_cannot_be_written_says_so(tmp_path, bin_dir, platform):
    root = _root(tmp_path)
    (root / ".playground").write_text("not a directory")
    say = _only(_copy(root, "ch3.task"))
    assert say == Say("copy.failed")


# -- what the machine asks for, defended anyway ----------------------------------------------


def test_an_index_the_task_does_not_have(tmp_path, bin_dir, platform):
    say = _only(_copy(_root(tmp_path), "ch3.task", 9))
    assert say == Say("copy.bad_index", {"count": 3})


def test_a_task_that_does_not_exist(tmp_path, bin_dir, platform):
    say = _only(_copy(_root(tmp_path), "ch4.task"))
    assert say == Say("copy.none")


# -- through the runner ------------------------------------------------------------------------


class CopyingMachine:
    """Just enough of a machine: it asks for ``/copy`` once, then ends."""

    unattended_no = False
    elapsed_s = None
    session = None

    def __init__(self, effect: CopySnippets) -> None:
        self._effect = effect
        self.fed: list[Any] = []

    def start(self) -> list[Any]:
        return [self._effect]

    def handle(self, _answer: str) -> list[Any]:
        return [Exit(0)]

    def on_event(self, event: Any) -> list[Any]:
        self.fed.append(event)
        if isinstance(event, StepDone):
            return [*event.says, AwaitInput("any")]
        return []


def _session(tmp_path: Path, machine: CopyingMachine) -> tuple[Recorder, int]:
    root = _root(tmp_path)
    (root / "app" / "main.py").write_text("")
    renderer = Recorder()

    async def read_line(_expect: str) -> str | None:
        return None  # Ctrl-C at the prompt that follows

    async def go() -> int:
        return await asyncio.wait_for(
            runner.run(
                machine,  # type: ignore[arg-type]
                renderer,
                read_line,
                root=root,
                interactive=True,
                app=FakeApp(),  # type: ignore[arg-type]
                watcher=FakeWatcher(),  # type: ignore[arg-type]
            ),
            WITHIN_S,
        )

    return renderer, asyncio.run(go())


def test_the_runner_copies_and_shows_the_answer(tmp_path, bin_dir, platform):
    got = _tool(bin_dir, _TOOL[platform])
    machine = CopyingMachine(CopySnippets("ch3.task"))
    renderer, code = _session(tmp_path, machine)
    assert code == 0
    assert got.read_bytes().decode("utf-8") == snippet_text("ch3.task")
    assert renderer.says() == [Say("copy.done", {"count": 3})]
    (done,) = machine.fed
    assert isinstance(done, StepDone) and done.passed
    assert done.elapsed_s is None  # no /start in this session: no time to stamp


def test_the_runner_names_the_file_when_there_is_no_tool(tmp_path, bin_dir, platform):
    machine = CopyingMachine(CopySnippets("ch3.task", 2))
    renderer, code = _session(tmp_path, machine)
    assert code == 0
    assert renderer.says() == [Say("copy.file", {"path": ".playground/snippets/chapter3-2.py"})]
