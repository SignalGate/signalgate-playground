"""Tests for the tracked-path allowlist checker.

This suite loads `scripts/check_tracked_paths.py` directly by file path so it
stays self-contained even in a worktree with no pyproject.toml and no
installed package.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "check_tracked_paths.py"
_SPEC = importlib.util.spec_from_file_location("check_tracked_paths", _SCRIPT_PATH)
_MODULE = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = _MODULE
_SPEC.loader.exec_module(_MODULE)

find_violations = _MODULE.find_violations

# A small clean baseline so each single-offender case is checked alongside
# paths that must never be flagged.
BASELINE = [
    ".gitignore",
    "pyproject.toml",
    "uv.lock",
    ".python-version",
    "README.md",
    "LICENSE",
    "NOTICE",
    "TRADEMARK.md",
    ".github/workflows/ci.yml",
    "scripts/check_tracked_paths.py",
    "tests/test_tracked_paths.py",
]


def test_claude_md_is_a_violation():
    result = find_violations(BASELINE + ["CLAUDE.md"])
    assert "CLAUDE.md" in result


def test_dot_claude_directory_is_a_violation():
    result = find_violations(BASELINE + [".claude/x"])
    assert ".claude/x" in result


def test_dot_dreamteam_directory_is_a_violation():
    result = find_violations(BASELINE + [".dreamteam/x"])
    assert ".dreamteam/x" in result


def test_planning_doc_named_like_a_spec_is_a_violation():
    result = find_violations(BASELINE + ["docs/a-spec.md"])
    assert "docs/a-spec.md" in result


def test_architecture_named_file_is_a_violation():
    result = find_violations(BASELINE + ["X_ARCHITECTURE.txt"])
    assert "X_ARCHITECTURE.txt" in result


def test_real_env_file_is_a_violation():
    result = find_violations(BASELINE + ["app/.env"])
    assert "app/.env" in result


def test_unknown_top_level_file_is_a_violation():
    result = find_violations(BASELINE + ["notes.txt"])
    assert "notes.txt" in result


def test_env_example_is_allowed():
    result = find_violations(BASELINE + ["app/.env.example"])
    assert "app/.env.example" not in result


def test_full_m0_tree_has_zero_violations():
    # A realistic approximation of the repository tree.
    # Exact filenames may differ slightly once merged; the point is that the
    # allowlist accepts a full, realistic tree shape, not a byte-for-byte one.
    m0_tree = [
        ".gitignore",
        "pyproject.toml",
        "uv.lock",
        ".python-version",
        "README.md",
        "LICENSE",
        "NOTICE",
        "TRADEMARK.md",
        ".github/workflows/ci.yml",
        "scripts/check_tracked_paths.py",
        "tests/test_tracked_paths.py",
        "tests/test_no_local_paths.py",
        "tests/test_cli.py",
        "tests/__init__.py",
        "playground/__init__.py",
        "playground/__main__.py",
        "playground/cli.py",
        "playground/copy/__init__.py",
        "playground/copy/en.py",
        "playground/machine/__init__.py",
        "playground/traffic/__init__.py",
        "playground/verify/__init__.py",
        "playground/api/__init__.py",
        "playground/supervisor/__init__.py",
        "playground/tui/__init__.py",
        "app/__init__.py",
        "app/main.py",
        "app/routes.py",
        "app/kit/__init__.py",
        "app/.env.example",
    ]

    assert find_violations(m0_tree) == []
