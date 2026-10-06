"""Keeping the API key out of git: each check on a throwaway repo, and the key never in argv.

Keys are built at run time from harmless parts, so no tracked file holds a
key-shaped string.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from playground.verify import hygiene
from playground.verify.hygiene import check_hygiene
from playground.verify.model import Verdict

KEY = "pk_live_" + "Hy9_" * 8
OTHER_KEY = "pk_live_" + "b" * 31 + "-"


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A fresh git repo with a sample app whose ``app/.env`` is ignored."""
    home = tmp_path / "home"
    home.mkdir()
    for name, value in {
        "HOME": str(home),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.invalid",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.invalid",
    }.items():
        monkeypatch.setenv(name, value)
    root = tmp_path / "repo"
    (root / "app").mkdir(parents=True)
    (root / ".gitignore").write_text("app/.env\n")
    (root / "app" / "routes.py").write_text("# routes\n")
    (root / "app" / ".env").write_text(f"SIGNALGATE_API_KEY={KEY}\n")
    _git(root, "init", "-q", "-b", "main")
    _git(root, "add", ".gitignore", "app/routes.py")
    _git(root, "commit", "-q", "-m", "start")
    return root


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)


def _commit(root: Path, relative: str, text: str, message: str = "change") -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    _git(root, "add", "-f", relative)
    _git(root, "commit", "-q", "-m", message)


def test_a_clean_checkout_passes(repo):
    assert check_hygiene(repo, KEY) == hygiene.Hygiene(Verdict.PASS)


def test_no_git_checkout_is_not_run(tmp_path):
    assert check_hygiene(tmp_path, KEY).verdict is Verdict.NOT_RUN


def test_a_tracked_env_file_fails(repo):
    _git(repo, "add", "-f", "app/.env")
    assert check_hygiene(repo, KEY).problem == hygiene.ENV_TRACKED


def test_an_env_file_git_would_not_ignore_fails(repo):
    (repo / ".gitignore").write_text("")
    assert check_hygiene(repo, KEY).problem == hygiene.ENV_NOT_IGNORED


def test_the_key_in_a_tracked_file_fails_and_names_it(repo):
    _commit(repo, "app/settings_copy.py", f"KEY = '{KEY}'\n")
    found = check_hygiene(repo, KEY)
    assert (found.problem, found.file) == (hygiene.KEY_IN_FILE, "app/settings_copy.py")


def test_the_key_anywhere_in_history_fails(repo):
    _git(repo, "checkout", "-q", "-b", "side")
    _commit(repo, "notes.txt", f"{KEY}\n")
    _commit(repo, "notes.txt", "gone now\n")
    _git(repo, "checkout", "-q", "main")
    assert check_hygiene(repo, KEY).problem == hygiene.KEY_IN_HISTORY


def test_another_key_shaped_value_fails_outside_tests_only(repo):
    _commit(repo, "tests/fixture.txt", f"x={OTHER_KEY}\n")
    assert check_hygiene(repo, KEY).verdict is Verdict.PASS
    _commit(repo, "docs/example.md", f"`{OTHER_KEY}`\n")
    found = check_hygiene(repo, KEY)
    assert (found.problem, found.file) == (hygiene.KEY_SHAPE_IN_FILE, "docs/example.md")


def test_a_longer_token_is_not_a_key(repo):
    _commit(repo, "docs/example.md", f"{OTHER_KEY}zz\n")
    assert check_hygiene(repo, KEY).verdict is Verdict.PASS


def test_the_key_never_goes_on_a_command_line(repo, monkeypatch):
    seen: list[list[str]] = []
    real_run, real_popen = subprocess.run, subprocess.Popen

    def run(args, *rest, **kwargs):
        seen.append(list(args))
        return real_run(args, *rest, **kwargs)

    def popen(args, *rest, **kwargs):
        seen.append(list(args))
        return real_popen(args, *rest, **kwargs)

    monkeypatch.setattr(hygiene.subprocess, "run", run)
    monkeypatch.setattr(hygiene.subprocess, "Popen", popen)
    _commit(repo, "notes.txt", f"{KEY}\n")
    _commit(repo, "notes.txt", "gone\n")

    assert check_hygiene(repo, KEY).problem == hygiene.KEY_IN_HISTORY
    assert seen
    assert not any(KEY in arg for args in seen for arg in args)
