"""Tests for the disclosure scanner.

The scanner is loaded straight from ``scripts/disclosure_scan.py``, the way the
CI job runs it. Every list here is made up: its terms are nonsense words that
mean nothing outside these tests.
"""

from __future__ import annotations

import base64
import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

_SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "disclosure_scan.py"
_SPEC = importlib.util.spec_from_file_location("disclosure_scan", _SCRIPT_PATH)
_MODULE = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = _MODULE
_SPEC.loader.exec_module(_MODULE)

LIST = """\
# a made-up list

zzquux
zz-flarn
/v9/zzroute
zzkey:{
re:zz[0-9]{3}blip
allow:the zzquux lounge
canary:zz-canary-7c1e
"""
LOCAL = {}  # an environment with no CI markers
IN_CI = {"CI": "true", "GITHUB_ACTIONS": "true"}


def _deny(text: str = LIST):
    return _MODULE.parse_list(text)


def _found(text: str, deny=None) -> list[str]:
    return (deny or _deny()).matches(text)


# --- the list ------------------------------------------------------------------


def test_a_list_parses_into_terms_patterns_allows_and_canaries():
    deny = _deny()
    assert deny.terms == ["zzquux", "zz-flarn", "/v9/zzroute", "zzkey:{"]
    assert [p.pattern for p in deny.patterns] == ["zz[0-9]{3}blip"]
    assert deny.canaries == ["zz-canary-7c1e"]
    assert len(deny.allows) == 1


@pytest.mark.parametrize("text", [None, "", "   \n# only a comment\n"])
def test_an_empty_list_is_rejected(text):
    with pytest.raises(_MODULE.ListError):
        _deny(text)


def test_a_list_without_a_canary_is_rejected():
    with pytest.raises(_MODULE.ListError):
        _deny("zzquux\n")


def test_a_bad_pattern_is_rejected():
    with pytest.raises(_MODULE.ListError):
        _deny("re:zz(unclosed\ncanary:zz-canary-7c1e\n")


def test_the_self_test_passes_for_a_sound_list():
    assert _MODULE.self_test(_deny())


def test_the_self_test_fails_when_an_allowed_phrase_hides_a_listed_term():
    # An allow entry that swallows a term would silently switch that term off.
    assert not _MODULE.self_test(_deny("zzquux\nallow:zzquux\ncanary:zz-canary-7c1e\n"))


# --- matching ------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    ["a zzquux b", "ZZQuux", "x_zzquux_y", "(zzquux)", "zzquux.", "see zz-flarn now"],
)
def test_a_term_matches_as_a_whole_word_in_any_case(text):
    assert _found(text)


@pytest.mark.parametrize("text", ["zzquuxes", "prezzquux", "zzquux9", "zz-flarnish"])
def test_a_term_inside_a_longer_word_does_not_match(text):
    assert not _found(text)


def test_a_term_that_ends_in_punctuation_matches_before_anything():
    assert _found("GET /v9/zzroute/1")
    assert _found("the zzkey:{name} format")


def test_a_pattern_matches_case_insensitively():
    assert _found("value ZZ123BLIP here")
    assert not _found("value zz12blip here")


def test_an_allowed_phrase_is_removed_before_matching():
    assert not _found("meet me in the zzquux lounge")
    assert _found("meet me in the zzquux lounge, then zzquux again")


def test_the_canary_counts_as_a_listed_term():
    assert _found("oops zz-canary-7c1e")


def test_undecodable_bytes_do_not_stop_the_scan():
    hits = _MODULE.scan([_MODULE.Source("blob.bin", b"\xff\xfe zzquux \x00\x81")], _deny())
    assert [h.entry for h in hits] == ["zzquux"]


def test_a_term_in_a_file_name_is_found():
    hits = _MODULE.scan([_MODULE.Source("docs/zzquux.md", b"harmless")], _deny())
    assert hits and hits[0].line == 0


def test_hits_carry_the_line_of_the_file():
    hits = _MODULE.scan([_MODULE.Source("a.txt", b"one\ntwo zzquux\nthree")], _deny())
    assert [(h.source, h.line) for h in hits] == [("a.txt", 2)]


# --- the command, in a throwaway repository -----------------------------------


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
        cwd=repo,
        check=True,
        capture_output=True,
    )


@pytest.fixture
def repo(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    (root / "keep.txt").write_text("an old zzquux that was already here\n")
    (root / "clean.txt").write_text("nothing to see\n")
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", "base")
    monkeypatch.chdir(root)
    return root


def _run(capsys, *argv: str, env=None, list_text: str = LIST) -> tuple[int, str]:
    environment = dict(LOCAL if env is None else env)
    if list_text is not None:
        environment[_MODULE.LIST_ENV] = list_text
    code = _MODULE.main(list(argv), env=environment)
    return code, capsys.readouterr().out


def test_a_clean_change_prints_only_pass(repo, capsys):
    _git(repo, "checkout", "-q", "-b", "change")
    (repo / "new.txt").write_text("a harmless line\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "add a harmless file")
    code, out = _run(capsys, "--base", "main", "--head", "change", env=IN_CI)
    assert (code, out) == (0, "PASS\n")


def test_a_listed_term_in_a_change_prints_only_fail(repo, capsys):
    _git(repo, "checkout", "-q", "-b", "change")
    (repo / "new.txt").write_text("line one\nthis mentions zz-flarn\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "add a file")
    code, out = _run(capsys, "--base", "main", "--head", "change", env=IN_CI)
    # Nothing about the file, the line, the term or the count.
    assert (code, out) == (1, "FAIL\n")


def test_a_listed_term_in_a_commit_message_fails(repo, capsys):
    _git(repo, "checkout", "-q", "-b", "change")
    (repo / "new.txt").write_text("a harmless line\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "mention zzquux in the message")
    code, out = _run(capsys, "--base", "main", "--head", "change", env=IN_CI)
    assert (code, out) == (1, "FAIL\n")


def test_a_change_is_scanned_without_the_files_it_did_not_touch(repo, capsys):
    # keep.txt already holds a listed term on main; a change that doesn't
    # touch it is judged on what it changes. The full scan on main catches it.
    _git(repo, "checkout", "-q", "-b", "change")
    (repo / "clean.txt").write_text("still nothing to see\n")
    _git(repo, "commit", "-q", "-am", "touch the clean file")
    code, out = _run(capsys, "--base", "main", "--head", "change", env=IN_CI)
    assert (code, out) == (0, "PASS\n")


def test_a_deleted_file_is_not_scanned(repo, capsys):
    _git(repo, "checkout", "-q", "-b", "change")
    _git(repo, "rm", "-q", "keep.txt")
    _git(repo, "commit", "-q", "-m", "remove a file")
    code, out = _run(capsys, "--base", "main", "--head", "change", env=IN_CI)
    assert (code, out) == (0, "PASS\n")


def test_the_whole_tree_is_scanned_without_a_base(repo, capsys):
    code, out = _run(capsys, env=IN_CI)
    assert (code, out) == (1, "FAIL\n")


def test_a_symlink_is_scanned_as_its_target_path_not_followed(repo, capsys, tmp_path):
    outside = tmp_path / "outside.txt"
    outside.write_text("zzquux lives outside the repo\n")
    (repo / "keep.txt").write_text("nothing here now\n")
    os.symlink(outside, repo / "link.txt")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "add a link")
    code, out = _run(capsys, env=IN_CI)
    assert (code, out) == (0, "PASS\n")


def test_text_from_an_environment_variable_is_scanned(repo, capsys):
    (repo / "keep.txt").write_text("nothing here now\n")
    _git(repo, "commit", "-q", "-am", "clean up")
    env = dict(IN_CI, PR_BODY="this body mentions ZZ-FLARN", PR_TITLE="fine")
    code, out = _run(capsys, "--text-env", "PR_TITLE", "--text-env", "PR_BODY", env=env)
    assert (code, out) == (1, "FAIL\n")


def test_a_missing_environment_variable_counts_as_empty(repo, capsys):
    (repo / "keep.txt").write_text("nothing here now\n")
    _git(repo, "commit", "-q", "-am", "clean up")
    code, out = _run(capsys, "--text-env", "PR_BODY", env=IN_CI)
    assert (code, out) == (0, "PASS\n")


@pytest.mark.parametrize("list_text", [None, "", "zzquux\n"])
def test_a_missing_or_incomplete_list_fails_closed(repo, capsys, list_text):
    code, out = _run(capsys, env=IN_CI, list_text=list_text)
    assert code == 2
    assert out.startswith("FAIL")
    assert "zzquux" not in out


def test_a_list_whose_self_test_fails_fails_closed(repo, capsys):
    broken = "zzquux\nallow:zzquux\ncanary:zz-canary-7c1e\n"
    code, out = _run(capsys, env=IN_CI, list_text=broken)
    assert code == 2 and out.startswith("FAIL")


def test_a_list_can_come_from_a_file(repo, capsys, tmp_path):
    list_file = tmp_path / "list.txt"
    list_file.write_text(LIST)
    code, out = _run(capsys, "--list", str(list_file), env=IN_CI, list_text=None)
    assert (code, out) == (1, "FAIL\n")


def test_explain_shows_what_matched_locally(repo, capsys):
    code, out = _run(capsys, "--explain", env=LOCAL)
    assert code == 1
    assert "keep.txt:1: zzquux" in out
    assert out.rstrip().endswith("FAIL")


def test_explain_refuses_to_run_in_ci(repo, capsys):
    code, out = _run(capsys, "--explain", env=IN_CI)
    assert code == 2
    assert "keep.txt" not in out and "zzquux" not in out


# --- the secret's encoding -----------------------------------------------------


def test_a_base64_list_on_one_line_is_decoded():
    encoded = base64.b64encode(LIST.encode()).decode()
    assert "\n" not in encoded
    assert _MODULE.decode_list(encoded) == LIST


def test_a_base64_list_wrapped_over_lines_is_decoded():
    encoded = base64.encodebytes(LIST.encode()).decode()  # wrapped every 76 chars
    assert "\n" in encoded.strip()
    assert _MODULE.decode_list(encoded) == LIST


def test_a_plain_text_list_is_used_as_it_is():
    assert _MODULE.decode_list(LIST) == LIST


def test_base64_that_is_not_a_list_stays_as_given_and_fails_closed():
    not_a_list = base64.b64encode(b"just some words").decode()
    assert _MODULE.decode_list(not_a_list) == not_a_list
    with pytest.raises(_MODULE.ListError):
        _MODULE.parse_list(_MODULE.decode_list(not_a_list))


def test_a_base64_secret_drives_the_scan(repo, capsys):
    encoded = base64.b64encode(LIST.encode()).decode()
    code, out = _run(capsys, env=IN_CI, list_text=encoded)
    assert (code, out) == (1, "FAIL\n")


# --- a fetched commit, read through --git-dir and --paths-file -----------------
#
# The pull-request scan fetches the change's head commit, depth 1 and with no
# checkout, into a scratch repository outside the workspace, and reads the
# changed files from git's objects. These tests build that situation: a change
# committed in one repository, fetched into a second, and scanned from a third
# directory that is neither.

PROTECTED = ".github/**,pyproject.toml,uv.lock"
ALLOWED_OUTPUT = {
    "PASS\n",
    "FAIL\n",
    "FAIL (needs a maintainer)\n",
    "FAIL (the check isn't configured)\n",
    "FAIL (the check's self-test failed)\n",
}
BASE_FILES = {
    "keep.txt": b"an old zzquux that was already here\n",
    "clean.txt": b"nothing to see\n",
    ".github/workflows/ci.yml": b"name: CI\n",
    "pyproject.toml": b"[project]\n",
}


def _write(root: Path, files: dict[str, bytes]) -> None:
    for name, data in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)


class Fetched:
    """A change fetched into a scratch repository: its git dir, and its paths."""

    def __init__(self, tmp_path: Path, write: dict[str, bytes], delete: tuple[str, ...]):
        self.tmp = tmp_path
        source = tmp_path / "source"
        source.mkdir()
        _git(source, "init", "-q", "-b", "main")
        _write(source, BASE_FILES)
        _git(source, "add", "-A")
        _git(source, "commit", "-q", "-m", "base")
        _write(source, write)
        for path in delete:
            _git(source, "rm", "-q", "--", path)
        _git(source, "add", "-A")
        _git(source, "commit", "-q", "--allow-empty", "-m", "the change")
        listed = subprocess.run(
            ["git", "diff", "--name-only", "-z", "--no-renames", "HEAD~1", "HEAD"],
            cwd=source,
            check=True,
            capture_output=True,
        ).stdout
        self.paths = [name for name in listed.decode("utf-8").split("\0") if name]
        scratch = tmp_path / "scratch"
        scratch.mkdir()
        _git(scratch, "init", "-q")
        _git(scratch, "fetch", "-q", "--depth=1", "--no-tags", source.as_uri(), "HEAD")
        self.git_dir = str(scratch / ".git")

    def paths_file(self, paths: list[str] | None = None, sep: str = "\0") -> str:
        # Each path ends with the separator, the way the workflow writes the list.
        target = self.tmp / "paths"
        listed = self.paths if paths is None else paths
        target.write_bytes("".join(path + sep for path in listed).encode("utf-8"))
        return str(target)

    def text_file(self, name: str, text: str) -> str:
        target = self.tmp / name
        target.write_text(text, encoding="utf-8")
        return str(target)

    def args(self, *extra: str, paths: list[str] | None = None, sep: str = "\0") -> list[str]:
        return [
            "--git-dir",
            self.git_dir,
            "--head",
            "FETCH_HEAD",
            "--paths-file",
            self.paths_file(paths, sep),
            *extra,
        ]


@pytest.fixture
def fetch(tmp_path, monkeypatch):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)

    def make(write: dict[str, bytes] | None = None, delete: tuple[str, ...] = ()) -> Fetched:
        return Fetched(tmp_path, write or {}, delete)

    return make


def test_a_fetched_commit_has_no_checkout_to_read(fetch):
    change = fetch({"new.txt": b"a harmless line\n"})
    scratch = Path(change.git_dir).parent
    assert [p.name for p in scratch.iterdir()] == [".git"]
    assert (Path(change.git_dir) / "shallow").exists()


def test_the_listed_files_of_a_fetched_commit_pass_when_clean(fetch, capsys):
    change = fetch({"new.txt": b"a harmless line\n", "clean.txt": b"still clean\n"})
    assert change.paths == ["clean.txt", "new.txt"]
    code, out = _run(capsys, *change.args(), env=IN_CI)
    assert (code, out) == (0, "PASS\n")


def test_a_listed_term_in_a_fetched_file_prints_only_fail(fetch, capsys):
    change = fetch({"docs/new.md": b"line one\nthis mentions zz-flarn\n"})
    code, out = _run(capsys, *change.args(), env=IN_CI)
    assert (code, out) == (1, "FAIL\n")


def test_the_canary_in_a_fetched_file_prints_only_fail(fetch, capsys):
    change = fetch({"tests/fixture.txt": b"planted: zz-canary-7c1e\n"})
    code, out = _run(capsys, *change.args(), env=IN_CI)
    assert (code, out) == (1, "FAIL\n")


def test_the_files_are_read_as_raw_bytes(fetch, capsys):
    change = fetch({"blob.bin": b"\xff\xfe\x00 zzquux \x81"})
    code, out = _run(capsys, *change.args(), env=IN_CI)
    assert (code, out) == (1, "FAIL\n")


def test_only_the_listed_files_are_read(fetch, capsys):
    # keep.txt holds a listed term, but the change doesn't touch it.
    change = fetch({"new.txt": b"a harmless line\n"})
    code, out = _run(capsys, *change.args(), env=IN_CI)
    assert (code, out) == (0, "PASS\n")


def test_a_listed_file_the_commit_no_longer_has_is_skipped(fetch, capsys):
    change = fetch(delete=("keep.txt",))
    assert change.paths == ["keep.txt"]
    code, out = _run(capsys, *change.args(), env=IN_CI)
    assert (code, out) == (0, "PASS\n")


def test_a_listed_term_in_a_listed_path_is_found(fetch, capsys):
    change = fetch({"docs/zzquux.md": b"harmless\n"})
    code, out = _run(capsys, *change.args(), env=IN_CI)
    assert (code, out) == (1, "FAIL\n")


def test_a_symlink_is_read_as_its_target_path(fetch, capsys):
    change = fetch()
    source = change.tmp / "source"
    os.symlink("zzquux/elsewhere.txt", source / "link.txt")
    _git(source, "add", "link.txt")
    _git(source, "commit", "-q", "-m", "a link")
    _git(Path(change.git_dir).parent, "fetch", "-q", "--depth=1", source.as_uri(), "HEAD")
    code, out = _run(capsys, *change.args(paths=["link.txt"]), env=IN_CI)
    assert (code, out) == (1, "FAIL\n")


def test_a_nul_separated_list_keeps_any_file_name(fetch, capsys):
    change = fetch({"odd\nname.txt": b"this mentions zz-flarn\n"})
    assert change.paths == ["odd\nname.txt"]
    code, out = _run(capsys, *change.args(), env=IN_CI)
    assert (code, out) == (1, "FAIL\n")


def test_a_list_with_one_path_per_line_works_too(fetch, capsys):
    change = fetch({"a.txt": b"fine\n", "b.txt": b"this mentions zz-flarn\n"})
    code, out = _run(capsys, *change.args(sep="\n"), env=IN_CI)
    assert (code, out) == (1, "FAIL\n")


def test_a_head_given_as_a_commit_id_is_read(fetch, capsys):
    change = fetch({"new.txt": b"this mentions zz-flarn\n"})
    head = subprocess.run(
        ["git", "--git-dir", change.git_dir, "rev-parse", "FETCH_HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    argv = change.args()
    argv[argv.index("FETCH_HEAD")] = head
    code, out = _run(capsys, *argv, env=IN_CI)
    assert (code, out) == (1, "FAIL\n")


def test_every_git_call_goes_through_the_git_dir(fetch, capsys, monkeypatch):
    change = fetch({"new.txt": b"a harmless line\n"})
    calls = []
    real_run = subprocess.run

    def spy(command, *args, **kwargs):
        calls.append(list(command))
        return real_run(command, *args, **kwargs)

    monkeypatch.setattr(_MODULE.subprocess, "run", spy)
    code, out = _run(capsys, *change.args(), env=IN_CI)
    assert (code, out) == (0, "PASS\n")
    assert calls
    assert all(call[:3] == ["git", "--git-dir", change.git_dir] for call in calls)


def test_the_git_dir_alone_scans_every_file_of_head(fetch, capsys):
    change = fetch({"new.txt": b"a harmless line\n"})
    code, out = _run(capsys, "--git-dir", change.git_dir, "--head", "FETCH_HEAD", env=IN_CI)
    assert (code, out) == (1, "FAIL\n")  # keep.txt, untouched but in the tree


def test_text_files_are_scanned(fetch, capsys):
    change = fetch({"new.txt": b"a harmless line\n"})
    title = change.text_file("title", "a fine title")
    body = change.text_file("body", "a body that mentions ZZ-FLARN")
    messages = change.text_file("messages", "first commit\n\nsecond commit\n")
    texts = ["--text-file", title, "--text-file", body, "--text-file", messages]
    code, out = _run(capsys, *change.args(*texts), env=IN_CI)
    assert (code, out) == (1, "FAIL\n")


def test_clean_text_files_pass(fetch, capsys):
    change = fetch({"new.txt": b"a harmless line\n"})
    title = change.text_file("title", "a fine title")
    messages = change.text_file("messages", "")
    code, out = _run(capsys, *change.args("--text-file", title, "--text-file", messages), env=IN_CI)
    assert (code, out) == (0, "PASS\n")


def test_the_canary_in_a_commit_message_fails(fetch, capsys):
    change = fetch({"new.txt": b"a harmless line\n"})
    messages = change.text_file("messages", "add a file\n\nzz-canary-7c1e\n")
    code, out = _run(capsys, *change.args("--text-file", messages), env=IN_CI)
    assert (code, out) == (1, "FAIL\n")


def test_a_text_files_own_name_is_not_scanned(fetch, capsys):
    change = fetch({"new.txt": b"a harmless line\n"})
    title = change.text_file("zzquux-title", "a fine title")
    code, out = _run(capsys, *change.args("--text-file", title), env=IN_CI)
    assert (code, out) == (0, "PASS\n")


def test_a_missing_text_file_fails_closed(fetch, capsys):
    change = fetch({"new.txt": b"a harmless line\n"})
    missing = str(change.tmp / "no-such-file")
    code, out = _run(capsys, *change.args("--text-file", missing), env=IN_CI)
    assert (code, out) == (2, "FAIL (the check isn't configured)\n")


def test_a_missing_paths_file_fails_closed(fetch, capsys):
    change = fetch({"new.txt": b"a harmless line\n"})
    argv = change.args()
    argv[argv.index("--paths-file") + 1] = str(change.tmp / "no-such-file")
    code, out = _run(capsys, *argv, env=IN_CI)
    assert (code, out) == (2, "FAIL (the check isn't configured)\n")


def test_an_empty_paths_file_name_fails_closed(fetch, capsys):
    # Never a quiet fall back to another mode.
    change = fetch({"new.txt": b"a harmless line\n"})
    argv = change.args()
    argv[argv.index("--paths-file") + 1] = ""
    code, out = _run(capsys, *argv, env=IN_CI)
    assert (code, out) == (2, "FAIL (the check isn't configured)\n")


def test_a_commit_git_cannot_read_fails_closed_without_details(fetch, capsys):
    change = fetch({"zzquux.txt": b"this mentions zz-flarn\n"})
    argv = change.args()
    argv[argv.index("FETCH_HEAD")] = "0" * 40
    code, out = _run(capsys, *argv, env=IN_CI)
    assert (code, out) == (2, "FAIL (the check isn't configured)\n")


def test_a_revision_that_looks_like_an_option_is_refused(fetch, capsys):
    change = fetch({"new.txt": b"a harmless line\n"})
    argv = change.args()
    argv[argv.index("FETCH_HEAD")] = "--output=elsewhere"
    with pytest.raises(SystemExit) as stopped:
        _run(capsys, *argv, env=IN_CI)
    assert stopped.value.code == 2
    assert not (change.tmp / "elsewhere" / "elsewhere").exists()


def test_base_and_a_paths_file_do_not_go_together(fetch, capsys):
    change = fetch({"new.txt": b"a harmless line\n"})
    with pytest.raises(SystemExit):
        _run(capsys, *change.args("--base", "FETCH_HEAD"), env=IN_CI)


# --- the maintainer path rule --------------------------------------------------


@pytest.mark.parametrize(
    ("path", "protected"),
    [
        (".github/workflows/ci.yml", True),
        (".github/CODEOWNERS", True),
        (".github/a/b/c/d.yml", True),
        (".GitHub/workflows/ci.yml", True),
        ("pyproject.toml", True),
        ("uv.lock", True),
        (".github", False),
        ("docs/.github/x.yml", False),
        ("app/pyproject.toml", False),
        ("uv.lock.bak", False),
        ("playground/cli.py", False),
    ],
)
def test_the_maintainer_globs(path, protected):
    globs = _MODULE.maintainer_globs(PROTECTED)
    assert bool(_MODULE.needs_maintainer([path], globs)) is protected


@pytest.mark.parametrize(
    ("glob", "path", "matches"),
    [
        ("docs/*.md", "docs/a.md", True),
        ("docs/*.md", "docs/sub/a.md", False),
        ("docs/**/*.md", "docs/a.md", True),
        ("docs/**/*.md", "docs/sub/deeper/a.md", True),
        ("a?.txt", "ab.txt", True),
        ("a?.txt", "a/.txt", False),
        ("[ab].txt", "[ab].txt", True),
        ("[ab].txt", "a.txt", False),
    ],
)
def test_a_star_stays_in_one_directory_and_two_span_them(glob, path, matches):
    assert bool(_MODULE.needs_maintainer([path], _MODULE.maintainer_globs(glob))) is matches


def test_protected_paths_are_scanned_like_any_other_without_the_flag(fetch, capsys):
    change = fetch({".github/workflows/ci.yml": b"name: CI, renamed\n"})
    assert change.paths == [".github/workflows/ci.yml"]
    code, out = _run(capsys, *change.args(), env=IN_CI)
    assert (code, out) == (0, "PASS\n")


def test_protected_paths_need_a_maintainer_with_the_flag(fetch, capsys):
    change = fetch({".github/workflows/ci.yml": b"name: CI, renamed\n"})
    code, out = _run(capsys, *change.args("--maintainer-paths", PROTECTED), env=IN_CI)
    assert (code, out) == (1, "FAIL (needs a maintainer)\n")


def test_the_path_rule_reads_no_file(fetch, capsys, monkeypatch):
    # The protected file holds the canary: read, it would be a plain FAIL.
    change = fetch({".github/workflows/ci.yml": b"# zz-canary-7c1e\n", "new.txt": b"fine\n"})
    code, out = _run(capsys, *change.args(), env=IN_CI)
    assert (code, out) == (1, "FAIL\n")

    calls = []
    real_git = _MODULE._git

    def spy(git_dir, *args):
        calls.append(args)
        return real_git(git_dir, *args)

    monkeypatch.setattr(_MODULE, "_git", spy)
    code, out = _run(capsys, *change.args("--maintainer-paths", PROTECTED), env=IN_CI)
    assert (code, out) == (1, "FAIL (needs a maintainer)\n")
    assert calls == []


def test_the_path_rule_comes_before_the_text_files(fetch, capsys):
    change = fetch({"pyproject.toml": b"[project]\nname = 'x'\n"})
    body = change.text_file("body", "a body that mentions zz-flarn")
    argv = change.args("--maintainer-paths", PROTECTED, "--text-file", body)
    code, out = _run(capsys, *argv, env=IN_CI)
    assert (code, out) == (1, "FAIL (needs a maintainer)\n")


def test_deleting_a_protected_path_needs_a_maintainer(fetch, capsys):
    change = fetch(delete=(".github/workflows/ci.yml",))
    code, out = _run(capsys, *change.args("--maintainer-paths", PROTECTED), env=IN_CI)
    assert (code, out) == (1, "FAIL (needs a maintainer)\n")


def test_a_change_with_no_protected_path_is_scanned_under_the_flag(fetch, capsys):
    clean = fetch({"docs/new.md": b"a harmless line\n"})
    code, out = _run(capsys, *clean.args("--maintainer-paths", PROTECTED), env=IN_CI)
    assert (code, out) == (0, "PASS\n")


def test_a_listed_term_is_found_under_the_flag_when_nothing_is_protected(fetch, capsys):
    dirty = fetch({"docs/new.md": b"this mentions zz-flarn\n"})
    code, out = _run(capsys, *dirty.args("--maintainer-paths", PROTECTED), env=IN_CI)
    assert (code, out) == (1, "FAIL\n")


def test_the_path_rule_applies_to_a_base_and_head_change(repo, capsys):
    _git(repo, "checkout", "-q", "-b", "change")
    _write(repo, {"uv.lock": b"version = 1\n", "new.txt": b"fine\n"})
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "lock")
    argv = ["--base", "main", "--head", "change"]
    assert _run(capsys, *argv, env=IN_CI) == (0, "PASS\n")
    code, out = _run(capsys, *argv, "--maintainer-paths", PROTECTED, env=IN_CI)
    assert (code, out) == (1, "FAIL (needs a maintainer)\n")


def test_the_path_rule_needs_a_change_to_judge(repo, capsys):
    with pytest.raises(SystemExit) as stopped:
        _run(capsys, "--maintainer-paths", PROTECTED, env=IN_CI)
    assert stopped.value.code == 2


def test_the_path_rule_needs_at_least_one_glob(fetch, capsys):
    change = fetch({"new.txt": b"fine\n"})
    with pytest.raises(SystemExit) as stopped:
        _run(capsys, *change.args("--maintainer-paths", " , "), env=IN_CI)
    assert stopped.value.code == 2


def test_explain_names_the_protected_paths_locally(fetch, capsys):
    change = fetch({".github/CODEOWNERS": b"* @someone\n"})
    code, out = _run(capsys, *change.args("--maintainer-paths", PROTECTED, "--explain"))
    assert code == 1
    assert out == ".github/CODEOWNERS: needs a maintainer\nFAIL (needs a maintainer)\n"


# --- the commit messages a push brought ----------------------------------------
#
# On a push to main the tree scan also reads the messages of the pushed
# commits, since a squash merge copies a pull request's title and body into
# one. The workflow passes the push's earlier head as --messages-since.

NOT_CONFIGURED = "FAIL (the check isn't configured)\n"
NO_COMMIT = "0" * 40


def _rev(repo: Path, name: str) -> str:
    return subprocess.run(
        ["git", "rev-parse", name], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()


def _commit(repo: Path, message: str, files: dict[str, bytes] | None = None) -> str:
    _write(repo, files or {})
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "--allow-empty", "-m", message)
    return _rev(repo, "HEAD")


@pytest.fixture
def history(tmp_path, monkeypatch):
    root = tmp_path / "history"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    monkeypatch.chdir(root)
    return root


def test_a_listed_term_in_a_pushed_message_prints_only_fail(history, capsys):
    before = _commit(history, "start", {"a.txt": b"fine\n"})
    _commit(history, "a message that mentions zz-flarn", {"b.txt": b"fine\n"})
    _commit(history, "a later, clean one")
    assert _run(capsys, "--messages-since", before, env=IN_CI) == (1, "FAIL\n")


def test_the_canary_in_a_pushed_message_fails(history, capsys):
    before = _commit(history, "start", {"a.txt": b"fine\n"})
    _commit(history, "squashed\n\nzz-canary-7c1e\n")
    assert _run(capsys, "--messages-since", before, env=IN_CI) == (1, "FAIL\n")


def test_messages_from_before_the_push_are_not_read(history, capsys):
    _commit(history, "an old message that mentions zzquux", {"a.txt": b"fine\n"})
    before = _commit(history, "clean")
    _commit(history, "also clean", {"b.txt": b"fine\n"})
    assert _run(capsys, "--messages-since", before, env=IN_CI) == (0, "PASS\n")


def test_without_the_flag_the_tree_scan_reads_no_message(history, capsys):
    _commit(history, "a message that mentions zzquux", {"a.txt": b"fine\n"})
    assert _run(capsys, env=IN_CI) == (0, "PASS\n")


def test_the_tree_is_still_scanned_with_the_pushed_messages(history, capsys):
    before = _commit(history, "start", {"a.txt": b"fine\n"})
    _commit(history, "clean", {"b.txt": b"this mentions zz-flarn\n"})
    assert _run(capsys, "--messages-since", before, env=IN_CI) == (1, "FAIL\n")


def test_a_push_that_creates_the_branch_reads_every_commit(history, capsys):
    _commit(history, "an old message that mentions zzquux", {"a.txt": b"fine\n"})
    _commit(history, "clean")
    assert _run(capsys, "--messages-since", NO_COMMIT, env=IN_CI) == (1, "FAIL\n")


def test_a_new_branch_with_clean_messages_passes(history, capsys):
    _commit(history, "one", {"a.txt": b"fine\n"})
    _commit(history, "two")
    assert _run(capsys, "--messages-since", NO_COMMIT, env=IN_CI) == (0, "PASS\n")


def test_after_a_force_push_the_rewritten_commits_are_read(history, capsys):
    _commit(history, "start", {"a.txt": b"fine\n"})
    _git(history, "checkout", "-q", "-b", "replaced")
    before = _commit(history, "the replaced head mentions zzquux")
    _git(history, "checkout", "-q", "main")
    _commit(history, "the new head")
    # The replaced commit is gone from the branch, so its message isn't judged.
    assert _run(capsys, "--messages-since", before, env=IN_CI) == (0, "PASS\n")
    _commit(history, "and one that mentions zz-flarn")
    assert _run(capsys, "--messages-since", before, env=IN_CI) == (1, "FAIL\n")


@pytest.mark.parametrize("before", ["1" * 40, "no-such-branch", "", "--output=elsewhere"])
def test_a_push_start_git_cannot_find_fails_closed(history, capsys, before):
    _commit(history, "a message that mentions zzquux", {"a.txt": b"fine\n"})
    # The "=" form hands an empty or dash-led value to the scanner as it is.
    code, out = _run(capsys, f"--messages-since={before}", env=IN_CI)
    assert (code, out) == (2, NOT_CONFIGURED)
    assert not (history / "elsewhere").exists()


def test_a_push_start_that_is_not_a_commit_fails_closed(history, capsys):
    _commit(history, "start", {"a.txt": b"fine\n"})
    tree = _rev(history, "HEAD^{tree}")
    assert _run(capsys, "--messages-since", tree, env=IN_CI) == (2, NOT_CONFIGURED)


def test_pushed_messages_are_never_read_from_a_shallow_clone(
    history, capsys, tmp_path, monkeypatch
):
    before = _commit(history, "one", {"a.txt": b"fine\n"})
    _commit(history, "two")
    _commit(history, "three")
    shallow = tmp_path / "shallow"
    subprocess.run(
        ["git", "clone", "-q", "--depth=1", history.as_uri(), str(shallow)],
        check=True,
        capture_output=True,
    )
    monkeypatch.chdir(shallow)
    for start in (NO_COMMIT, before):
        assert _run(capsys, "--messages-since", start, env=IN_CI) == (2, NOT_CONFIGURED)


def test_pushed_messages_go_through_the_git_dir(history, capsys, tmp_path, monkeypatch):
    before = _commit(history, "start", {"a.txt": b"fine\n"})
    _commit(history, "a message that mentions zz-flarn")
    monkeypatch.chdir(tmp_path)
    git_dir = str(history / ".git")
    argv = ["--git-dir", git_dir, "--head", "HEAD", "--messages-since", before]
    assert _run(capsys, *argv, env=IN_CI) == (1, "FAIL\n")


# --- what the log can show -----------------------------------------------------


@pytest.mark.parametrize(
    ("write", "extra", "list_text"),
    [
        ({"a.txt": b"fine\n"}, (), LIST),
        ({"zzquux/a.txt": b"one\ntwo zz-flarn\nzz123blip\n"}, (), LIST),
        ({".github/x.yml": b"zz-canary-7c1e\n"}, ("--maintainer-paths", PROTECTED), LIST),
        ({"a.txt": b"fine\n"}, (), None),
        ({"a.txt": b"fine\n"}, (), "zzquux\nallow:zzquux\ncanary:zz-canary-7c1e\n"),
    ],
)
def test_the_output_is_one_allowed_line_and_nothing_else(fetch, capsys, write, extra, list_text):
    change = fetch(write)
    _code, out = _run(capsys, *change.args(*extra), env=IN_CI, list_text=list_text)
    assert out in ALLOWED_OUTPUT
    for detail in ("a.txt", "zzquux", "zz-flarn", "blip", "canary", "x.yml", ":1", ":2"):
        assert detail not in out
