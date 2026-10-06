"""The disclosure workflows keep a pull request's code away from the deny-list.

"Disclosure collect" runs on each pull request with no secrets and records
what the change touches. "Disclosure" then judges it from the default branch:
it first marks the head commit's status pending, then fetches the head
commit's objects into a scratch repository, runs its own copy of the scanner,
and posts the verdict as a commit status. On main it scans the tree and the
messages of the pushed commits. These tests read the workflow files as text
(no YAML parser is installed) and check the properties that keep that safe;
a few run a step's shell with stand-ins for the tools it calls.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"
COLLECT = WORKFLOWS / "disclosure-collect.yml"
SCAN = WORKFLOWS / "disclosure-scan.yml"
CODEOWNERS = ROOT / ".github" / "CODEOWNERS"

_SCRIPT_PATH = ROOT / "scripts" / "disclosure_scan.py"
_SPEC = importlib.util.spec_from_file_location("disclosure_scan_for_workflows", _SCRIPT_PATH)
_SCANNER = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = _SCANNER
_SPEC.loader.exec_module(_SCANNER)


# --- reading a workflow as text ------------------------------------------------


def _text(path: Path) -> str:
    """The file without its comment lines, so a comment never satisfies a check."""
    lines = path.read_text(encoding="utf-8").splitlines()
    return "\n".join(line for line in lines if not line.lstrip().startswith("#"))


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _block(text: str, key: str, indent: int = 0) -> str:
    """The line ``key:`` at ``indent`` spaces and every line nested under it."""
    lines = text.splitlines()
    head = " " * indent + key + ":"
    for start, line in enumerate(lines):
        if line == head or line.startswith(head + " "):
            body = [line]
            for following in lines[start + 1 :]:
                if following.strip() and _indent(following) <= indent:
                    break
                body.append(following)
            return "\n".join(body)
    raise AssertionError(f"no {key!r} at indent {indent}")


def _jobs(text: str) -> dict[str, str]:
    jobs = _block(text, "jobs")
    names = re.findall(r"^  ([\w-]+):\s*$", jobs, re.MULTILINE)
    return {name: _block(jobs, name, 2) for name in names}


def _steps(job: str) -> list[str]:
    """Each step of a job, as the text from its ``-`` to the next one."""
    steps = _block(job, "steps", 4)
    return [chunk for chunk in re.split(r"^      - ", steps, flags=re.MULTILINE)[1:]]


def _run_scripts(text: str) -> list[str]:
    """The shell of every ``run:``, whether on one line or as a ``|`` block."""
    scripts = []
    lines = text.splitlines()
    for number, line in enumerate(lines):
        found = re.match(r"^(\s*)(?:- )?run: ?(.*)$", line)
        if not found:
            continue
        indent, rest = len(found.group(1)), found.group(2).strip()
        if not rest:
            continue  # "defaults: run:", a mapping rather than a script
        if rest not in ("|", ">", "|-", ">-"):
            scripts.append(rest)
            continue
        body = []
        for following in lines[number + 1 :]:
            if following.strip() and _indent(following) <= indent:
                break
            body.append(following)
        scripts.append("\n".join(body))
    return scripts


def _mapping(block: str) -> dict[str, str]:
    """The ``key: value`` lines directly under a block's first line."""
    lines = block.splitlines()
    inner = _indent(lines[1]) if len(lines) > 1 else 0
    pairs = {}
    for line in lines[1:]:
        if line.strip() and _indent(line) == inner:
            key, _, value = line.strip().partition(":")
            pairs[key] = value.strip()
    return pairs


def _uses(text: str) -> list[str]:
    return re.findall(r"^\s*(?:- )?uses:\s*(\S+)", text, re.MULTILINE)


def _workflow_name(text: str) -> str:
    found = re.search(r"^name:\s*(.+?)\s*$", text, re.MULTILINE)
    assert found, "the workflow has no name"
    return found.group(1).strip("\"'")


# --- every workflow ------------------------------------------------------------


@pytest.mark.parametrize("path", sorted(WORKFLOWS.glob("*.yml")), ids=lambda p: p.name)
def test_no_workflow_runs_on_pull_request_target(path):
    assert "pull_request_target" not in path.read_text(encoding="utf-8")


@pytest.mark.parametrize("path", sorted(WORKFLOWS.glob("*.yml")), ids=lambda p: p.name)
def test_every_action_is_pinned_to_a_full_commit(path):
    uses = _uses(_text(path))
    assert uses
    for action in uses:
        assert re.fullmatch(r"[\w.-]+/[\w.-]+(/[\w./-]+)?@[0-9a-f]{40}", action), action


def test_the_text_helpers_see_what_they_should():
    sample = (
        "name: Sample\n"
        "jobs:\n"
        "  one:\n"
        "    permissions:\n"
        "      contents: read\n"
        "    defaults:\n"
        "      run:\n"
        "        shell: bash\n"
        "    steps:\n"
        "      - uses: a/b@" + "0" * 40 + "\n"
        "      - name: Do\n"
        "        run: |\n"
        "          echo one\n"
        "          echo two\n"
        "  two:\n"
        "    steps:\n"
        "      - run: echo three\n"
    )
    jobs = _jobs(sample)
    assert list(jobs) == ["one", "two"]
    assert _mapping(_block(jobs["one"], "permissions", 4)) == {"contents": "read"}
    assert len(_steps(jobs["one"])) == 2
    assert [s.strip() for s in _run_scripts(sample)] == [
        "echo one\n          echo two",
        "echo three",
    ]


# --- Disclosure collect --------------------------------------------------------


def test_collect_runs_on_pull_requests_including_edits():
    text = _text(COLLECT)
    assert _workflow_name(text) == "Disclosure collect"
    on = _block(text, "on")
    assert list(_mapping(on)) == ["pull_request"]
    types = re.search(r"types:\s*\[([^\]]*)\]", on)
    assert types
    assert {t.strip() for t in types.group(1).split(",")} == {
        "opened",
        "synchronize",
        "reopened",
        "edited",
    }


def test_collect_holds_no_permissions_or_secrets():
    text = _text(COLLECT)
    assert re.search(r"^permissions: \{\}\s*$", text, re.MULTILINE)
    assert "secrets." not in text
    assert "DISCLOSURE_DENY_LIST" not in text
    (job,) = _jobs(text).values()
    assert _mapping(_block(job, "permissions", 4)) == {"contents": "read"}


def test_collect_uses_only_checkout_and_upload_artifact():
    actions = {action.split("@")[0] for action in _uses(_text(COLLECT))}
    assert actions == {"actions/checkout", "actions/upload-artifact"}


def test_collect_runs_nothing_from_the_pull_request():
    text = _text(COLLECT)
    (checkout,) = [s for s in _steps(next(iter(_jobs(text).values()))) if "checkout@" in s]
    assert "persist-credentials: false" in checkout
    for script in _run_scripts(text):
        for word in ("python", "uv ", "pip", "npm", "make", "scripts/", "source ", "bash "):
            assert word not in script, word


def test_collect_uploads_one_artifact_with_the_changed_files():
    text = _text(COLLECT)
    (upload,) = [s for s in _steps(next(iter(_jobs(text).values()))) if "upload-artifact@" in s]
    assert "name: disclosure-input" in upload
    assert "include-hidden-files: true" in upload  # .github/ files are changed files too
    (script,) = _run_scripts(text)
    for key in ("pr_number", "head_sha", "base_sha", "changed_paths"):
        assert key in script
    assert "--no-renames" in script and "-z" in script


# --- running a step's shell ----------------------------------------------------


def _executable(path: Path, text: str) -> None:
    path.write_text(text)
    path.chmod(0o755)


def _bash(step: str, env: dict[str, str], cwd: Path, shim: Path) -> subprocess.CompletedProcess:
    """Run a step's ``run:`` block as the runner does for ``shell: bash``, with
    ``shim`` first on the PATH and nothing else from this environment."""
    (script,) = _run_scripts(step)
    path = shim / "step.sh"
    path.write_text(textwrap.dedent(script) + "\n")
    full = {"PATH": f"{shim}{os.pathsep}{os.environ.get('PATH', '')}", "HOME": str(shim)}
    full.update(env)
    return subprocess.run(
        ["bash", "--noprofile", "--norc", "-eo", "pipefail", str(path)],
        cwd=cwd,
        env=full,
        capture_output=True,
        text=True,
        check=False,
    )


def _run_with_gh(tmp_path: Path, step: str, env: dict[str, str]):
    """Run a step with a stand-in ``gh`` that records each call's arguments."""
    shim = tmp_path / "shim"
    shim.mkdir(exist_ok=True)
    log = tmp_path / "gh.log"
    recorder = shim / "record_gh.py"
    recorder.write_text(
        "import json, os, sys\n"
        "with open(os.environ['GH_LOG'], 'a') as log:\n"
        "    log.write(json.dumps(sys.argv[1:]) + '\\n')\n"
    )
    _executable(shim / "gh", f'#!/bin/sh\nexec "{sys.executable}" "{recorder}" "$@"\n')
    result = _bash(step, {**env, "GH_LOG": str(log)}, tmp_path, shim)
    lines = log.read_text().splitlines() if log.exists() else []
    return result, [json.loads(line) for line in lines]


def _status_call(sha: str, state: str, description: str, url: str) -> list[str]:
    return [
        "api",
        "--method",
        "POST",
        f"repos/o/r/statuses/{sha}",
        "-f",
        f"state={state}",
        "-f",
        "context=Disclosure",
        "-f",
        f"description={description}",
        "-f",
        f"target_url={url}",
    ]


needs_bash = pytest.mark.skipif(shutil.which("bash") is None, reason="runs a step's bash")


# --- Disclosure: the pending status --------------------------------------------
#
# A pull request's scan waits for a maintainer's approval. Until it reports, the
# status on the head commit must not be an earlier run's verdict, which may be
# for a title or body that has changed since: an ungated job marks it pending
# first, on every run.


def _pending() -> str:
    return _jobs(_text(SCAN))["pending"]


def _step(job: str, marker: str) -> str:
    (step,) = [step for step in _steps(job) if marker in step]
    return step


def test_the_pending_job_comes_first_and_every_gated_job_needs_it():
    jobs = _jobs(_text(SCAN))
    assert next(iter(jobs)) == "pending"
    gated = [name for name, job in jobs.items() if "environment:" in job]
    assert gated == ["scan-pr"]
    for name in gated:
        assert re.search(r"^    needs: pending\s*$", jobs[name], re.MULTILINE), name


def test_the_pending_job_runs_on_every_pull_request_run():
    condition = _block(_pending(), "if", 4)
    assert "github.event_name == 'workflow_run'" in condition
    assert "github.event.workflow_run.event == 'pull_request'" in condition
    assert "github.event.action" not in condition  # both requested and completed


def test_the_pending_job_holds_nothing_but_the_status_permission():
    job = _pending()
    assert "environment:" not in job
    assert "needs:" not in job
    assert _mapping(_block(job, "permissions", 4)) == {"statuses": "write"}
    assert "secrets." not in job and "DISCLOSURE_DENY_LIST" not in job
    # No action at all, so no checkout: the only code it runs is the workflow
    # file itself, which a workflow_run always takes from the default branch.
    assert _uses(job) == []
    assert "ref:" not in job
    for script in _run_scripts(job):
        for word in ("python", "scripts/", "git ", "source ", "bash ", "curl", "download"):
            assert word not in script, word


@needs_bash
def test_the_pending_job_marks_the_head_commit_pending(tmp_path):
    job = _pending()
    (step,) = _steps(job)
    assert "HEAD_SHA: ${{ github.event.workflow_run.head_sha }}" in step
    assert "GH_REPO: ${{ github.repository }}" in step
    env = {"GH_REPO": "o/r", "HEAD_SHA": "b" * 40, "RUN_URL": "run"}
    result, calls = _run_with_gh(tmp_path, step, env)
    assert result.returncode == 0
    assert calls == [_status_call("b" * 40, "pending", "Waiting for the scan", "run")]


def test_runs_for_one_head_commit_share_a_group_and_the_newest_wins():
    text = _text(SCAN)
    concurrency = _mapping(_block(text, "concurrency"))
    assert concurrency["cancel-in-progress"] == "true"
    # A pull-request run is grouped by its head commit; a push or a manual run
    # has no workflow_run, falls back to its own run id and cancels nothing.
    assert concurrency["group"] == (
        "disclosure-${{ github.event.workflow_run.head_sha || github.run_id }}"
    )
    for job in _jobs(text).values():
        assert "concurrency:" not in job


# --- Disclosure: the pull-request scan -----------------------------------------


def _scan_pr() -> str:
    return _jobs(_text(SCAN))["scan-pr"]


def test_the_scan_runs_after_collect_and_never_on_a_pull_request():
    text = _text(SCAN)
    on = _block(text, "on")
    assert set(_mapping(on)) == {"workflow_run", "push", "workflow_dispatch"}
    run = _block(on, "workflow_run", 2)
    workflows = re.search(r"workflows:\s*\[\s*\"([^\"]+)\"\s*\]", run)
    assert workflows and workflows.group(1) == _workflow_name(_text(COLLECT))
    # "requested" marks the status pending as soon as a collect run starts.
    types = re.search(r"types:\s*\[([^\]]*)\]", run)
    assert types
    assert {t.strip() for t in types.group(1).split(",")} == {"requested", "completed"}


def test_the_scan_workflow_grants_nothing_by_default():
    assert re.search(r"^permissions: \{\}\s*$", _text(SCAN), re.MULTILINE)


def test_scan_pr_has_only_what_it_needs_and_waits_for_the_environment():
    job = _scan_pr()
    assert re.search(r"^    environment: disclosure\s*$", job, re.MULTILINE)
    assert _mapping(_block(job, "permissions", 4)) == {
        "contents": "read",
        "actions": "read",
        "pull-requests": "read",
        "statuses": "write",
    }
    assert "github.event_name == 'workflow_run'" in _block(job, "if", 4)


def test_scan_pr_runs_only_once_collect_has_completed():
    assert "github.event.action == 'completed'" in _block(_scan_pr(), "if", 4)


def test_scan_pr_checks_out_only_the_default_branch():
    job = _scan_pr()
    checkouts = [step for step in _steps(job) if "actions/checkout@" in step]
    assert len(checkouts) == 1
    assert "ref: ${{ github.event.repository.default_branch }}" in checkouts[0]
    assert "persist-credentials: false" in checkouts[0]
    text = _text(SCAN)
    for ref in re.findall(r"^\s*ref:\s*(.+)$", text, re.MULTILINE):
        assert ref.strip() == "${{ github.event.repository.default_branch }}"
    assert "refs/pull" not in text
    for script in _run_scripts(text):
        for command in ("git checkout", "git switch", "git worktree", "git clone"):
            assert command not in script


def test_scan_pr_downloads_the_collect_artifact_by_run_id():
    step = _step(_scan_pr(), "download-artifact@")
    assert "name: disclosure-input" in step
    assert "run-id: ${{ github.event.workflow_run.id }}" in step


def test_scan_pr_confirms_the_pull_request_and_its_files_with_the_api():
    script = "\n".join(_run_scripts(_step(_scan_pr(), "name: Confirm the pull request")))
    assert '"repos/$GH_REPO/pulls/$pr"' in script
    assert "'.head.sha'" in script and '"$HEAD_SHA"' in script
    assert "--paginate" in script and '"repos/$GH_REPO/pulls/$pr/files?per_page=100"' in script
    assert "cmp -s paths listed" in script
    assert '"repos/$GH_REPO/pulls/$pr/commits?per_page=100"' in script
    assert "HEAD_SHA: ${{ github.event.workflow_run.head_sha }}" in _block(_scan_pr(), "env", 4)


def test_scan_pr_fetches_the_head_commit_outside_the_workspace_with_no_checkout():
    script = "\n".join(_run_scripts(_step(_scan_pr(), "name: Fetch the head commit")))
    assert 'git init -q "$RUNNER_TEMP/pr"' in script
    assert re.search(r'git -C "\$RUNNER_TEMP/pr".*fetch[^\n]*--depth=1', script, re.DOTALL)
    assert script.rstrip().endswith('"$HEAD_SHA"')
    assert "GITHUB_WORKSPACE" not in script


def _scanner_call() -> str:
    (script,) = _run_scripts(_step(_scan_pr(), "id: scan"))
    return script


def test_scan_pr_runs_the_default_branch_scanner_on_the_fetched_objects():
    call = _scanner_call()
    assert "python3 scripts/disclosure_scan.py" in call
    assert '--git-dir "$RUNNER_TEMP/pr/.git"' in call
    assert '--head "$HEAD_SHA"' in call
    assert '--paths-file "$RUNNER_TEMP/paths"' in call
    for name in ("title", "body", "messages"):
        assert f'--text-file "$RUNNER_TEMP/text/{name}"' in call


def test_scan_pr_sends_protected_paths_to_a_maintainer():
    found = re.search(r"--maintainer-paths '([^']*)'", _scanner_call())
    assert found
    globs = {glob.strip() for glob in found.group(1).split(",")}
    assert {".github/**", "pyproject.toml", "uv.lock"} <= globs
    matchers = _SCANNER.maintainer_globs(found.group(1))
    protected = [".github/workflows/ci.yml", ".github/CODEOWNERS", "pyproject.toml", "uv.lock"]
    assert _SCANNER.needs_maintainer(protected, matchers) == protected
    assert _SCANNER.needs_maintainer(["playground/cli.py", "docs/x.md"], matchers) == []


def test_scan_pr_prints_only_the_scanners_own_verdicts():
    call = _scanner_call()
    allowed = set(re.findall(r'"(PASS|FAIL[^"]*)"', call))
    assert allowed == {
        "PASS",
        "FAIL",
        _SCANNER.NEEDS_MAINTAINER,
        _SCANNER.NOT_CONFIGURED,
        _SCANNER.SELF_TEST_FAILED,
    }
    echoes = re.findall(r"^\s*echo (.*)$", call, re.MULTILINE)
    assert echoes == ['"$verdict"', '"verdict=$verdict" >> "$GITHUB_OUTPUT"']


def test_scan_pr_posts_the_verdict_as_the_disclosure_status():
    step = _step(_scan_pr(), "name: Report")
    assert "if: always()" in step
    assert "JOB_STATUS: ${{ job.status }}" in step
    script = "\n".join(_run_scripts(step))
    assert '"repos/$GH_REPO/statuses/$HEAD_SHA"' in script
    assert "context=Disclosure" in script


@needs_bash
@pytest.mark.parametrize(
    ("verdict", "job_status", "state", "description"),
    [
        ("PASS", "success", "success", "PASS"),
        # Cancelled by a newer run after its scan passed: never a success.
        ("PASS", "cancelled", "failure", "FAIL"),
        ("PASS", "failure", "failure", "FAIL"),
        ("", "failure", "failure", "FAIL"),
        ("", "cancelled", "failure", "FAIL"),
        ("FAIL", "failure", "failure", "FAIL"),
        (_SCANNER.NEEDS_MAINTAINER, "failure", "failure", _SCANNER.NEEDS_MAINTAINER),
    ],
)
def test_the_report_is_a_success_only_after_a_passing_uncancelled_run(
    tmp_path, verdict, job_status, state, description
):
    step = _step(_scan_pr(), "name: Report")
    env = {"VERDICT": verdict, "JOB_STATUS": job_status, "GH_REPO": "o/r", "RUN_URL": "run"}
    env["HEAD_SHA"] = "a" * 40
    result, calls = _run_with_gh(tmp_path, step, env)
    assert result.returncode == 0
    assert calls == [_status_call("a" * 40, state, description, "run")]


def test_the_deny_list_reaches_only_the_scan_steps():
    text = _text(SCAN)
    assert text.count("secrets.") == 2
    for job in _jobs(text).values():
        for step in _steps(job):
            if "secrets." in step:
                assert "name: Scan" in step
                assert "DISCLOSURE_DENY_LIST: ${{ secrets.DISCLOSURE_DENY_LIST }}" in step
                assert "GH_TOKEN" not in step


@pytest.mark.parametrize("path", [COLLECT, SCAN], ids=lambda p: p.name)
def test_no_run_script_expands_an_expression(path):
    # Values reach the shell through the environment, never pasted into it.
    for script in _run_scripts(_text(path)):
        assert "${{" not in script


# --- Disclosure: the full-tree scan --------------------------------------------


def _scan_tree() -> str:
    return _jobs(_text(SCAN))["scan-tree"]


def test_scan_tree_runs_on_main_and_on_demand_only():
    text = _text(SCAN)
    assert re.search(r"push:\s*\n\s*branches:\s*\n\s*- main\s*$", _block(text, "on"), re.MULTILINE)
    job = _scan_tree()
    condition = _block(job, "if", 4)
    assert "'push'" in condition and "'workflow_dispatch'" in condition
    assert "workflow_run" not in condition
    assert "environment:" not in job and "needs:" not in job
    assert _mapping(_block(job, "permissions", 4)) == {"contents": "read"}


def test_scan_tree_checks_out_every_commit():
    checkout = _step(_scan_tree(), "actions/checkout@")
    assert "fetch-depth: 0" in checkout
    assert "persist-credentials: false" in checkout
    assert "ref:" not in checkout


def test_scan_tree_scans_the_messages_a_push_brought():
    step = _step(_scan_tree(), "name: Scan")
    assert "PUSHED_FROM: ${{ github.event.before }}" in step
    (script,) = _run_scripts(step)
    calls = re.findall(r"^\s*(python3 .*)$", script, re.MULTILINE)
    assert calls == [
        'python3 scripts/disclosure_scan.py --messages-since "$PUSHED_FROM"',
        "python3 scripts/disclosure_scan.py",
    ]
    assert '[ "$GITHUB_EVENT_NAME" = "push" ]' in script


# A made-up list: its terms are nonsense words that mean nothing outside tests.
_MADE_UP_LIST = "zzquux\ncanary:zz-canary-7c1e\n"
_NOT_CONFIGURED = _SCANNER.NOT_CONFIGURED + "\n"


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


@pytest.fixture
def pushed(tmp_path):
    """A checkout of main as scan-tree sees it: the scanner, clean files, and
    history whose oldest message holds a listed term."""
    root = tmp_path / "checkout"
    (root / "scripts").mkdir(parents=True)
    _git(root, "init", "-q", "-b", "main")
    shutil.copy(_SCRIPT_PATH, root / "scripts" / "disclosure_scan.py")
    (root / "a.txt").write_text("fine\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "an old message that mentions zzquux")
    return root


def _scan_tree_run(tmp_path: Path, repo: Path, event: str, before: str):
    """Run scan-tree's Scan step the way the runner does, with the real scanner."""
    step = _step(_scan_tree(), "name: Scan")
    shim = tmp_path / "shim"
    shim.mkdir(exist_ok=True)
    _executable(shim / "python3", f'#!/bin/sh\nexec "{sys.executable}" "$@"\n')
    env = {
        "GITHUB_EVENT_NAME": event,
        "PUSHED_FROM": before,
        "DISCLOSURE_DENY_LIST": _MADE_UP_LIST,
        "CI": "true",
        "GITHUB_ACTIONS": "true",
    }
    return _bash(step, env, repo, shim)


@needs_bash
def test_scan_tree_fails_a_push_whose_new_message_holds_a_listed_term(tmp_path, pushed):
    _git(pushed, "commit", "-q", "--allow-empty", "-m", "clean")
    before = _git(pushed, "rev-parse", "HEAD")
    _git(pushed, "commit", "-q", "--allow-empty", "-m", "squashed: also mentions ZZQUUX")
    result = _scan_tree_run(tmp_path, pushed, "push", before)
    assert (result.returncode, result.stdout) == (1, "FAIL\n")


@needs_bash
def test_scan_tree_passes_a_clean_push(tmp_path, pushed):
    _git(pushed, "commit", "-q", "--allow-empty", "-m", "clean")
    before = _git(pushed, "rev-parse", "HEAD")
    _git(pushed, "commit", "-q", "--allow-empty", "-m", "also clean")
    result = _scan_tree_run(tmp_path, pushed, "push", before)
    assert (result.returncode, result.stdout) == (0, "PASS\n")


@needs_bash
def test_scan_tree_reads_every_message_when_the_push_created_the_branch(tmp_path, pushed):
    result = _scan_tree_run(tmp_path, pushed, "push", "0" * 40)
    assert (result.returncode, result.stdout) == (1, "FAIL\n")


@needs_bash
@pytest.mark.parametrize("before", ["1" * 40, ""])
def test_scan_tree_fails_closed_on_a_push_start_it_cannot_read(tmp_path, pushed, before):
    result = _scan_tree_run(tmp_path, pushed, "push", before)
    assert (result.returncode, result.stdout) == (2, _NOT_CONFIGURED)


@needs_bash
def test_scan_tree_on_demand_scans_the_tracked_files_only(tmp_path, pushed):
    result = _scan_tree_run(tmp_path, pushed, "workflow_dispatch", "")
    assert (result.returncode, result.stdout) == (0, "PASS\n")
    (pushed / "a.txt").write_text("now it mentions zzquux\n")
    result = _scan_tree_run(tmp_path, pushed, "workflow_dispatch", "")
    assert (result.returncode, result.stdout) == (1, "FAIL\n")


# --- CODEOWNERS ----------------------------------------------------------------


def test_codeowners_cover_the_reviewed_paths():
    owned = {}
    for line in _text(CODEOWNERS).splitlines():
        if line.strip():
            path, *owners = line.split()
            owned[path] = owners
    for path in (
        "/playground/copy/",
        "/README.md",
        "/docs/",
        "/contracts/",
        "/playground/traffic/local_constants.py",
        "/app/kit/local_constants.py",
        "/.github/",
        "/scripts/disclosure_scan.py",
    ):
        assert path in owned, path
    for path, owners in owned.items():
        assert owners and all(owner.startswith("@") for owner in owners), path
        assert path == "/README.md" or (ROOT / path.strip("/")).exists(), path
