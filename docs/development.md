# Development

Setting up, testing, and building the playground console.

## Setup

Install dependencies and the playground in editable mode:

```bash
uv sync
```

This installs the package and all dev dependencies (pytest, ruff) into a
virtual environment managed by `uv`.

## Testing

Run the full test suite:

```bash
uv run pytest -q
```

`uv run pytest -q` runs everything. Two pytest markers select subsets:

- **End-to-end tests**: `uv run pytest -q -m e2e` (runs the console as a
  subprocess on a copy of the tracked tree, some through a pseudo-terminal)
- **Performance tests**: `uv run pytest -q -m perf` (the offline run's timing)

Run all except perf tests (quick local check):

```bash
uv run pytest -q -m "not perf"
```

What lives where:

- `tests/app/`, `tests/supervisor/`, `tests/machine/`, `tests/tui/`,
  `tests/traffic/`: unit tests for each part, in process.
- `tests/test_runner_reload.py`: the console's session policy (reloads,
  crashes, the wait for queued `log()` events, saving progress, Ctrl-C and
  other signals), driven over a fake app and a fake file watcher.
- `tests/integration/`: the real sample app with the real SDK client,
  read back through the console's own control client, and started by the
  console's own launcher.
- `tests/e2e/`: the whole console as a subprocess, in `--ci` mode and on a
  real pseudo-terminal: typed commands, Ctrl-C and Ctrl-D, saving files
  while it waits or runs, killing processes, and resuming.
- `tests/contract/`: the specs behind the files in `contracts/` (see
  [Contracts](#contracts)).
- `tests/fake_server/`: a test-only fake of the playground API, so the
  console's online chapters can be built and tested without the real API.
- `tests/api/`, `tests/test_online.py`, `tests/traffic/test_pacer.py`: the
  playground API client and its decoders, the online steps against the fake
  in process, and the pacer.
- `tests/verify/`: the verifier. Each routes file under
  `tests/verify/fixtures/wrong/` gets one thing wrong and must fail with its
  own finding; each under `fixtures/right/` must pass. They run inside the
  real sample app, served on a loopback port, with the SDK's calls forwarded
  to the fake server. The reference diffs in `solutions/` must pass too.

The end-to-end tests never touch the network or your checkout. Each one runs
in a temporary copy of the tracked files, with a test-only start-up hook
(`tests/e2e/_sitecustomize.py`, put on `PYTHONPATH` by `tests/e2e/seam.py`)
that refuses any connection that isn't to loopback and, inside the sample
app, answers the SDK's calls locally and records what the app did. Nothing
in `playground/` or `app/` knows the hook exists.

For chapters 2-6 the test serves the fake on a loopback port
(`tests/e2e/fake_host.py`) and hands the hook its address: the sample app's
SDK calls go on to the fake, and the console's playground API client gets a
transport that dials it (the client's only seam; there is no address
setting). `tests/e2e/test_online.py` runs the whole tutorial in `--ci --yes`,
with the fake's script playing the dashboard,
`tests/e2e/test_ci_scripted_driver.py` runs it in `--ci` without `--yes`,
answering the way scripted drivers do, and
`tests/e2e/test_interactive_online.py` drives chapters 2-6 on a real
terminal.

When an end-to-end test ends, no sample app it started may still be running.
Only the test's own apps are counted, never a console you have open
yourself or another checkout's tests.

Tests that start processes (a console, a sample app, a file watcher) must
stop every one of them before they finish. If a timing test fails, check
first that nothing is left running from an earlier run:

```bash
uptime
pgrep -fl 'uvicorn app.main'
```

### Python version

The project targets Python 3.10 as its minimum. The CI tests on both 3.10 and
3.12. To run the suite on 3.10 locally without touching the 3.12
environment in `.venv`, point `uv` at a separate one:

```bash
UV_PROJECT_ENVIRONMENT="${TMPDIR:-/tmp}/playground-py310" uv run --locked --python 3.10 pytest -q
```

## Code quality

Format and lint with `ruff`:

```bash
uv run ruff check .
uv run ruff format --check .
```

Fix issues automatically:

```bash
uv run ruff format .
uv run ruff check . --fix
```

Both are run in CI; keep them passing.

## Snapshots

Some tests capture console output as golden snapshots and verify that future
runs match. To regenerate snapshots after an intentional change:

```bash
PLAYGROUND_UPDATE_SNAPSHOTS=1 uv run pytest -q
```

Snapshots live in `tests/tui/snapshots/`, `tests/tui/snapshots_final/` and
`tests/tui/snapshots_ux/` and are checked in to the repo. While console output
is still changing, `PLAYGROUND_SKIP_SNAPSHOTS=1` skips the snapshot comparisons
instead (a switch for local test runs only); bring the snapshots up to date
before you push.

## Logo bitmap

The console's ASCII and Unicode logo is generated from an SVG. To regenerate
`playground/tui/logo_art.py`, run:

```bash
uv run python scripts/render_logo.py /path/to/mark.svg
```

The argument is the path to the mark SVG (a filled shape, two stroked polylines
and a dot, all in one accent colour). The script reads the SVG, rasterises it
to a half-block bitmap and an ASCII fallback, then writes the output module
with no local paths embedded.

## Contracts

`contracts/` pins the shapes the console and the sample app agree on, as
JSON Schema (a draft 2020-12 subset) in which every object is closed:

- `playground-api.v1.json`: the playground API the console calls;
- `app-control.v1.json`: the sample app's `/__playground/*` routes, and the
  headers the console sets on its requests to the app;
- `playground-labels.v1.json`: the dashboard's own words that the console's
  copy quotes, exactly as the dashboard shows them.

The files are generated from the specs in `tests/contract/`. Never edit one
by hand; change the spec, then regenerate:

```bash
uv run python scripts/export_contracts.py
```

A test fails when a committed file differs from what the specs generate.

`tests/fake_server/` answers the playground API from those specs, with
random, meaningless envelopes and a workflow status a test scripts step by
step (or lets play the dashboard: `Script(advance_on_poll=True,
hold_until_actions=...)`). The sample app's SDK calls reach it through the
SDK stub's forward mode (`tests/support/sdk_stub.forward_to`), in process,
or over loopback in the end-to-end tests. It is a test fixture only:
nothing in `playground/` or `app/` imports it, and it never reads the
environment.

## Repository layout

### `playground/`

The console application: state machine, rendering, traffic generation, app
supervision. Each submodule handles one concern (copy, machine, api, tui, etc.).

- `runner.py` drives one session: it carries out what the state machine asks
  for, keeps the sample app in step with saved files, and owns Ctrl-C,
  SIGTERM and SIGHUP. `online.py` carries out the steps of chapters 2-6.
- `machine/` is the chapter state machine; `machine/steps.py` holds the
  questions of chapters 2-6 and what each answer leads to.
- `api/` is the playground API client, its whitelisting decoders (a field
  the console doesn't know is dropped) and the API key reader.
- `verify/` checks your code and your runs against evidence (see its
  docstring for the entry points).
- `traffic/` builds and sends visitors; `traffic/pacer.py` paces runs
  through SignalGate in lease batches.
- `supervisor/` starts, stops and restarts the sample app, watches `app/`
  for changes, and waits for the app's queued `log()` events before a stop.
- `statefile.py` saves progress to `.playground/state.json` for `/continue`
  (version 2; a version 1 file still loads).

The console keeps its own files in `.playground/` (git-ignored): the lock
that stops a second console, the sample app's output in `app.log`, saved
progress in `state.json`, the `/report` bundle in `report.json`, the code
`/copy` saves in `snippets/` when there's no clipboard tool, and in
`console.log` any unexpected error an online step ran into.

### `app/`

The sample application that playground visitors are sent to: route handlers and
scaffolding utilities. Only `app/routes.py` is meant to be edited by tutorial
users; `app/kit/` provides utilities and stays out of the way.
`app/signalgate_client.py` gives the app its shared SDK client (`sg()`) and
`sg_event()`.

## Rules

### User-facing strings

Every string the console shows to the user lives in `playground/copy/en.py`
under the `TEXT` dictionary. The module owns all formatting (e.g.,
`{total:,}` for thousands separators). No UI text is hardcoded elsewhere.

### Timing constants

Durations (timeouts, delays, polling intervals) used by the console live
only in `playground/traffic/local_constants.py`; the sample app keeps its own
in `app/kit/local_constants.py`. Each module documents every constant and is
the single place to audit local pacing logic. A test
(`test_no_timing_constants.py`) enforces this: it scans the traffic, machine,
supervisor, verify and api code, the runner, the online steps, the state
file, the progress and clipboard helpers and the app's kit for stray
numeric literals passed to sleep/wait calls or module-level constants with
duration-like names.

Anything tied to SignalGate's own timing (how long to wait for stored
events, when to retry a readiness round, clock tolerances) comes from the
session's `limits` at run time, never from a constant.

Tests follow the same rule: express a delay or a timeout through these
constants, or derive it from them, rather than typing a new number.

### Path allowlist

`scripts/check_tracked_paths.py`, run by the CI `paths` job, enforces that the
repo tracks only files in an approved list. This prevents accidental commits of local state,
internal metadata, or generated artifacts. The allowed paths include:

- Top-level configuration: `pyproject.toml`, `uv.lock`, `.python-version`,
  `README.md`, license files, `.gitignore`
- Source code: `playground/**`, `app/**`, `tests/**`, `scripts/*.py`
- Reference diffs: `solutions/**`
- Contracts: `contracts/*.json`
- Docs: `docs/**`
- CI: `.github/workflows/ci.yml`, `.github/workflows/disclosure-collect.yml`,
  `.github/workflows/disclosure-scan.yml`, `.github/CODEOWNERS`,
  `.github/ISSUE_TEMPLATE/**`

Tracked files are also scanned for home-directory paths
(`test_no_local_paths.py`). The CI runs both
checks; see `.github/workflows/ci.yml` for details.

### Disclosure check

`scripts/disclosure_scan.py` scans every change against a private list of terms
that must never appear in this repository. For a pull request it scans the files
the change adds or modifies, its commit messages, and the PR's title and body; on
`main` it scans every tracked file and the messages of the commits each push
brings. It prints one line, `PASS` or `FAIL`; a failure gives at most a reason
that says nothing about the change, such as `FAIL (the check isn't
configured)`. It never says which file, line or term matched. When it can't
tell, it fails.

The list is not in the repository. Each line is one entry: a word matched
case-insensitively as a whole word, `re:<pattern>` for a regular expression,
`allow:<phrase>` for a public phrase that contains a listed word, and one required
`canary:<token>` that the check's self-test must find. Lines starting with `#` are
comments.

#### How a pull request is checked

Two workflows share the work, so that no code from a pull request ever runs
where the list is:

- **Disclosure collect** (`.github/workflows/disclosure-collect.yml`) runs on
  each pull request, and again when its title or body is edited. It has read
  access only and no secrets, and it runs nothing from the pull request. It
  uploads one artifact, `disclosure-input`: the pull request's number, its head
  and base commits, the paths it changes, and a copy of the changed files.
- **Disclosure** (`.github/workflows/disclosure-scan.yml`) starts when a
  Disclosure collect run is requested and again when it completes. Its first
  job, `pending`, sets the `Disclosure` status on the head commit to pending.
  It has no environment, no checkout, no secrets and only the permission to
  set a status, and it runs the workflow file from the default branch, as
  every `workflow_run` does. So an earlier verdict doesn't stand for text that
  has changed since: moments after an edit to the title or body, the status is
  pending again, and it stays so until the new scan reports.

  Its job `scan-pr` needs `pending` and runs once the collect run has
  completed and a maintainer has approved it (below). It checks out the default
  branch, so the scanner it runs is the one already merged, never the pull
  request's copy. It confirms with the API that the pull request named in the
  artifact has the head commit that run was for, takes the changed files and
  the commit messages from the API, and fails if the artifact lists different
  files: a pull request from a fork controls its own copy of the first
  workflow, so the artifact is only a cross-check. It then fetches the head
  commit alone, depth 1, into a scratch repository outside the workspace with
  no checkout, and scans the changed files there as raw bytes, together with
  the title, the body and the commit messages. The verdict is posted on the
  head commit as the `Disclosure` commit status.

Runs for the same head commit take turns, and a newer one cancels an older
one, even one waiting for approval, so the status on a commit always comes
from the latest run. A run cancelled after its scan passed reports a failure,
never a success.

Pull requests from forks go through the same steps and get a real verdict.

A pull request that changes `.github/**`, `pyproject.toml` or `uv.lock` isn't
scanned: the check reports `FAIL (needs a maintainer)`, because such a change
can alter how the checks or the build run. A maintainer reviews it, runs the
scan locally (below), and then sets the status by hand from a checkout:

```bash
gh api 'repos/{owner}/{repo}/statuses/<head commit>' \
  -f state=success -f context=Disclosure -f description=PASS
```

The scanner applies this rule only when asked to, with `--maintainer-paths`;
only the pull-request job passes it.

The workflow's `scan-tree` job scans every tracked file on each push to `main`,
and when the workflow is run by hand. On a push it also scans the messages of
the commits the push brought, since a squash merge copies the pull request's
title and body into one: every commit after the push's earlier head, or every
commit on the branch when the push created it. After a force push those are
the rewritten commits. If the earlier head can't be read (for example, after
a force push it is on no branch any more), the job fails rather than guess;
check the new messages locally with `--messages-since` (below). Pushes never
cancel each other.

#### Repository settings the check needs

- **The `DISCLOSURE_DENY_LIST` repository secret.** It holds the list
  base64-encoded on one line, so the run log can mask it as a single value.
  The check fails when the secret is missing, empty or broken. To set it from a
  plain-text list:

  ```bash
  base64 < deny-list.txt | tr -d '\n' | gh secret set DISCLOSURE_DENY_LIST
  ```

- **An environment named `disclosure` with required reviewers.** Each
  pull-request scan waits there until a maintainer approves the run. The
  `pending` job doesn't use it, so the status turns pending without waiting.
- **Branch protection on `main`** that requires the `Disclosure` status and a
  review from the code owners listed in `.github/CODEOWNERS`.

#### Running it locally

If you hold the list, the first command below scans your branch's change the
way a pull request is scanned: the files it adds or modifies, and its commit
messages. The second scans every tracked file, as `scan-tree` does. Add
`--explain` to either to see what matched; it runs on your machine only,
never in CI.

```bash
python3 scripts/disclosure_scan.py --list /path/outside/the/repo/deny-list.txt \
  --base origin/main --head HEAD
python3 scripts/disclosure_scan.py --list /path/outside/the/repo/deny-list.txt --explain
```

To check the messages of commits pushed to `main` the way `scan-tree` does,
pass the earlier head (all zeros for every commit on the branch):

```bash
python3 scripts/disclosure_scan.py --list /path/outside/the/repo/deny-list.txt \
  --messages-since <earlier head>
```

To repeat the pull-request job's own steps, fetch the head commit into a
scratch repository and scan the changed files from there, with the same list
of paths the job takes (a rename counts as both of its names):

```bash
S=$(mktemp -d)
git init -q "$S/pr" && git -C "$S/pr" fetch -q --depth=1 "$PWD" HEAD
git diff --name-only -z --no-renames origin/main...HEAD > "$S/paths"
python3 scripts/disclosure_scan.py --list /path/outside/the/repo/deny-list.txt \
  --git-dir "$S/pr/.git" --head FETCH_HEAD --paths-file "$S/paths" \
  --maintainer-paths '.github/**,pyproject.toml,uv.lock'
rm -rf "$S"
```

Like the job, this prints `FAIL (needs a maintainer)` for a change that
touches one of those paths, and reads no file. To scan such a change's
content, leave out `--maintainer-paths`. The job also scans the title, the
body and the commit messages, each saved to a file and passed with
`--text-file`.

`tests/test_disclosure_workflows.py` checks the workflows' safety properties;
`tests/test_disclosure_scan.py` checks the scanner.
