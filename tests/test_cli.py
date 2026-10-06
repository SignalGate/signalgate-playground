"""Tests for the playground command-line interface.

Argument parsing and the lock/env plumbing are unit-tested here, in process
and without ever starting the sample app. The full console session -- lock,
app launch, chapter 1, exit codes -- is covered end to end under
``tests/e2e``, against a temporary copy of the tree.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import filelock
import pytest

from playground import cli, statefile
from playground.cli import main
from playground.copy.en import text
from playground.machine.machine import Machine
from playground.runner import _auto_answer
from playground.tui.jsonl import JsonLinesRenderer
from tests.e2e.seam import SEAM_ENV

REPO_ROOT = Path(__file__).resolve().parents[1]

ALL_FLAGS = ["--size", "--plain", "--no-telemetry", "--ci", "--yes", "--until"]


def test_help_exits_zero_and_lists_every_flag(capsys):
    with pytest.raises(SystemExit) as excinfo:
        main(["--help"])
    assert excinfo.value.code == 0
    out = capsys.readouterr().out
    for flag in ALL_FLAGS:
        assert flag in out


@pytest.mark.parametrize("size", [1099, 5001])
def test_size_out_of_range_exits_three(size, capsys):
    with pytest.raises(SystemExit) as excinfo:
        main(["--size", str(size)])
    assert excinfo.value.code == 3


@pytest.mark.parametrize("size", [1100, 5000])
def test_size_in_range_is_accepted(size, monkeypatch):
    # Parsing is all this checks: what happens once parsing succeeds
    # (starting the app, running the console) is covered by tests/e2e.
    monkeypatch.setattr(cli, "_run", lambda args: 0)
    exit_code = main(["--size", str(size)])
    assert exit_code == 0


def test_ensure_env_file_copies_the_example_once(tmp_path):
    app_dir = tmp_path / "app"
    app_dir.mkdir()
    (app_dir / ".env.example").write_text("SIGNALGATE_API_KEY=PASTE_YOUR_API_KEY_HERE\n")

    assert cli._ensure_env_file(tmp_path) is True
    assert (app_dir / ".env").read_text() == "SIGNALGATE_API_KEY=PASTE_YOUR_API_KEY_HERE\n"

    # Already there: leave it alone, and report that nothing was created.
    (app_dir / ".env").write_text("SIGNALGATE_API_KEY=super-secret\n")
    assert cli._ensure_env_file(tmp_path) is False
    assert (app_dir / ".env").read_text() == "SIGNALGATE_API_KEY=super-secret\n"


def test_ensure_env_file_is_a_no_op_without_an_example(tmp_path):
    assert cli._ensure_env_file(tmp_path) is False
    assert not (tmp_path / "app" / ".env").exists()


def test_build_renderer_chooses_json_lines_for_ci():
    renderer, _read_line, discard_typeahead = cli._build_renderer(ci=True, plain=False)
    assert isinstance(renderer, JsonLinesRenderer)
    # Lines read from a pipe are answers in order: none is ever dropped.
    assert discard_typeahead is None


def test_second_console_is_refused_while_the_lock_is_held(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli.runner, "find_repo_root", lambda: tmp_path)
    lock_path = tmp_path / ".playground" / "console.lock"
    lock_path.parent.mkdir(parents=True)
    held = filelock.FileLock(str(lock_path))
    held.acquire()
    try:
        exit_code = main([])
    finally:
        held.release()

    assert exit_code == cli.PARSE_ERROR_EXIT_CODE
    assert text("console.lock_held") in capsys.readouterr().err


def _capture_run(monkeypatch, tmp_path) -> dict:
    """Point the command line at ``tmp_path`` and capture what it hands the runner."""
    (tmp_path / "app").mkdir(exist_ok=True)
    (tmp_path / "app" / "main.py").write_text("")
    monkeypatch.setattr(cli.runner, "find_repo_root", lambda: tmp_path)
    captured: dict = {}

    async def fake_run(machine, renderer, read_line, **kwargs):
        captured["machine"] = machine
        captured.update(kwargs)
        return 0

    monkeypatch.setattr(cli.runner, "run", fake_run)
    return captured


def test_an_attended_session_loads_saved_progress_and_says_where_it_saves(tmp_path, monkeypatch):
    captured = _capture_run(monkeypatch, tmp_path)
    state_path = tmp_path / ".playground" / "state.json"
    snapshot = Machine(offline_total=10_000).to_dict() | {
        "awaiting": "yes_no",
        "last_conversion": 0.2,
        "last_attack_in_ten": 8,
    }
    assert statefile.save(state_path, snapshot)

    assert main(["--ci"]) == 0

    assert captured["state_path"] == state_path
    assert captured["yes"] is False
    assert captured["interactive"] is False  # --ci reads JSON lines, not the prompt
    # The saved session is offered: the machine reports it until the user chooses.
    assert captured["machine"].to_dict() == snapshot


def test_an_unreadable_save_is_set_aside_with_a_notice(tmp_path, monkeypatch, capsys):
    captured = _capture_run(monkeypatch, tmp_path)
    state_path = tmp_path / ".playground" / "state.json"
    state_path.parent.mkdir(parents=True)
    state_path.write_text("{not json")

    assert main(["--ci"]) == 0

    assert not state_path.exists()
    assert (tmp_path / ".playground" / "state.json.bad").exists()
    keys = [json.loads(line)["key"] for line in capsys.readouterr().out.splitlines()]
    assert "state.set_aside" in keys
    assert captured["machine"].to_dict() == Machine(offline_total=10_000).to_dict()


def test_an_unattended_session_never_reads_or_writes_saved_progress(tmp_path, monkeypatch):
    captured = _capture_run(monkeypatch, tmp_path)

    def forbidden(*_args, **_kwargs):
        raise AssertionError("an unattended session touched saved progress")

    monkeypatch.setattr(cli.statefile, "load", forbidden)
    state_path = tmp_path / ".playground" / "state.json"
    state_path.parent.mkdir(parents=True)
    state_path.write_text("{not json")

    assert main(["--ci", "--yes", "--until", "1"]) == 0

    assert captured["state_path"] is None
    assert captured["yes"] is True
    assert state_path.read_text() == "{not json"


@pytest.mark.parametrize(
    ("expect", "decline", "answer"),
    [
        ("start", False, "/start"),
        ("yes_no", False, "yes"),
        ("yes_no", True, "no"),
        ("any", False, "/quit"),
        ("continue", False, "yes"),
    ],
)
def test_yes_answers_by_itself_and_says_no_where_the_machine_declines(expect, decline, answer):
    assert _auto_answer(expect, yes=True, until=None, decline=decline) == answer
    assert _auto_answer(expect, yes=False, until=None, decline=decline) is None


def test_the_test_seam_is_unknown_to_the_product_and_its_docs():
    # Tracked files only: an untracked app/.env is never opened.
    listed = subprocess.run(
        ["git", "ls-files", "-z", "--", "app", "playground", "docs", "README.md"],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
    ).stdout.decode()
    files = [REPO_ROOT / name for name in listed.split("\0") if name]
    assert any(path.name == "README.md" for path in files)
    for path in files:
        assert SEAM_ENV not in path.read_text(encoding="utf-8", errors="replace"), path
