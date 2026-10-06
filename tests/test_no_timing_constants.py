"""Guards against hidden timing constants outside ``local_constants.py``.

Every place that waits, polls or times out models this laptop's own pacing,
never SignalGate's. On the console side that is ``playground/traffic``,
``playground/machine``, ``playground/supervisor``, ``playground/verify``,
``playground/runner.py`` and ``playground/statefile.py``; in the sample app it is ``app/kit`` and
``app/signalgate_client.py``. Every duration lives in one of two places --
``playground/traffic/local_constants.py`` for the console and
``app/kit/local_constants.py`` for the sample app -- so it stays easy to
audit. Both are skipped by basename.

A scan root may be a directory (scanned recursively) or a single file. A
root that doesn't exist fails the test, so a typo can't silently shrink the
scan. The rest of ``playground/`` is deliberately not scanned.

This is a simple, deliberately non-exhaustive AST scan: it looks for a
numeric literal handed straight to a sleep/wait/timeout-shaped call, and for
a module- or class-level constant whose own name says it's a duration.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCAN_ROOTS = (
    REPO_ROOT / "playground" / "traffic",
    REPO_ROOT / "playground" / "machine",
    REPO_ROOT / "playground" / "supervisor",
    REPO_ROOT / "playground" / "verify",
    REPO_ROOT / "playground" / "runner.py",
    REPO_ROOT / "playground" / "online.py",
    REPO_ROOT / "playground" / "api",
    REPO_ROOT / "playground" / "statefile.py",
    REPO_ROOT / "playground" / "progress.py",
    REPO_ROOT / "playground" / "clipboard.py",
    REPO_ROOT / "app" / "kit",
    REPO_ROOT / "app" / "signalgate_client.py",
)
EXCLUDED_FILENAMES = {"local_constants.py"}

# Call names that read as "wait this long" -- checked wherever they appear.
DURATION_CALL_NAMES = {"sleep", "wait_for", "wait"}

# A module- or class-level constant is treated as a duration if its name
# contains one of these words, or ends with one of these unit suffixes.
DURATION_NAME_WORDS = ("SECONDS", "TIMEOUT", "DELAY", "INTERVAL", "GRACE")
DURATION_NAME_SUFFIXES = ("_S", "_MS")


def _iter_source_files(roots=SCAN_ROOTS):
    for root in roots:
        if root.is_file():
            yield root
        elif root.is_dir():
            yield from sorted(root.rglob("*.py"))
        else:
            raise AssertionError(f"timing scan root does not exist: {root}")


def _parse(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _numeric_literal(node: ast.AST | None) -> bool:
    if node is None:
        return False
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd)):
        node = node.operand
    return (
        isinstance(node, ast.Constant)
        and isinstance(node.value, (int, float))
        and not isinstance(node.value, bool)
    )


def _call_name(node: ast.Call) -> str | None:
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _looks_like_a_duration_name(name: str) -> bool:
    upper = name.upper()
    if any(word in upper for word in DURATION_NAME_WORDS):
        return True
    return any(upper.endswith(suffix) for suffix in DURATION_NAME_SUFFIXES)


def _check_calls(tree: ast.Module, path: Path, violations: list[str]) -> None:
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = _call_name(node)
        if name in DURATION_CALL_NAMES:
            for arg in node.args:
                if _numeric_literal(arg):
                    violations.append(f"{path}:{node.lineno}: literal duration passed to {name}()")
        for kw in node.keywords:
            if kw.arg == "timeout" and _numeric_literal(kw.value):
                violations.append(f"{path}:{node.lineno}: literal timeout= on a call")


def _check_scope_assigns(body: list[ast.stmt], path: Path, violations: list[str]) -> None:
    for node in body:
        if isinstance(node, ast.Assign):
            targets, value = node.targets, node.value
        elif isinstance(node, ast.AnnAssign):
            targets, value = [node.target], node.value
        else:
            continue
        if not _numeric_literal(value):
            continue
        for target in targets:
            if isinstance(target, ast.Name) and _looks_like_a_duration_name(target.id):
                violations.append(
                    f"{path}:{node.lineno}: {target.id} looks like a duration constant"
                )


def _collect_violations() -> list[str]:
    violations: list[str] = []
    for path in _iter_source_files():
        if path.name in EXCLUDED_FILENAMES:
            continue
        tree = _parse(path)
        _check_calls(tree, path, violations)
        _check_scope_assigns(tree.body, path, violations)
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                _check_scope_assigns(node.body, path, violations)
    return violations


def test_no_hidden_timing_constants_outside_local_constants():
    assert _collect_violations() == []


def test_every_scan_root_exists_and_yields_source():
    for root in SCAN_ROOTS:
        assert list(_iter_source_files((root,))), f"nothing to scan under {root}"


def test_a_file_root_is_scanned_as_itself(tmp_path):
    source = tmp_path / "single.py"
    source.write_text("VALUE = 0\n", encoding="utf-8")
    assert list(_iter_source_files((source,))) == [source]


def test_a_missing_root_fails_loudly(tmp_path):
    with pytest.raises(AssertionError, match="not-there"):
        list(_iter_source_files((tmp_path / "not-there",)))
