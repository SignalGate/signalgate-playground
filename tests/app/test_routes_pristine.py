"""`app/routes.py` stays byte-identical to the pinned version: no SignalGate
calls.

CI's checkout is shallow and carries no `main` ref, so this pins a SHA-256
instead of comparing against git history.
"""

from __future__ import annotations

import ast
import hashlib
from pathlib import Path

import pytest

ROUTES_PATH = Path(__file__).resolve().parents[2] / "app" / "routes.py"

# SHA-256 of `app/routes.py` as pinned.
PINNED_SHA256 = "1848942cab7cbcdd7aa075fa33af72289193b4fa0cb7912a6efd461ef321ef69"

_FORBIDDEN_CALL_NAMES = {"sg", "sg_event"}
_FORBIDDEN_METHODS = {"log", "check"}

# The two neutral markers the action route and the success route carry. They
# are checked on their own, so they survive even when the pin is updated.
ACTION_ANCHOR = "# the action happens here"
SUCCESS_ANCHOR = "# the user succeeded here"


def _source() -> str:
    return ROUTES_PATH.read_text(encoding="utf-8")


def test_the_file_is_byte_identical_to_the_pinned_hash():
    digest = hashlib.sha256(_source().encode("utf-8")).hexdigest()
    assert digest == PINNED_SHA256


def test_both_anchor_comments_are_present_exactly_once():
    # Independent of the hash pin above: a change that updates the pin on
    # purpose must keep these anchors.
    source = _source()
    assert source.count(ACTION_ANCHOR) == 1
    assert source.count(SUCCESS_ANCHOR) == 1


def _call_names(tree: ast.AST) -> list[str]:
    """Every plain-name call (`sg(...)`, `sg_event(...)`) in `tree`."""
    names = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            names.append(node.func.id)
    return names


def _method_calls(tree: ast.AST) -> list[str]:
    """Every `<something>.log(...)` / `<something>.check(...)` call in `tree`."""
    names = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            names.append(node.func.attr)
    return names


def test_no_sg_or_sg_event_calls():
    tree = ast.parse(_source(), filename=str(ROUTES_PATH))
    found = _FORBIDDEN_CALL_NAMES & set(_call_names(tree))
    assert not found, f"app/routes.py calls {sorted(found)}, which belongs in kit code"


def test_no_log_or_check_method_calls():
    tree = ast.parse(_source(), filename=str(ROUTES_PATH))
    found = _FORBIDDEN_METHODS & set(_method_calls(tree))
    assert not found, f"app/routes.py calls .{sorted(found)}(...), which belongs in kit code"


@pytest.mark.parametrize("name", sorted(_FORBIDDEN_CALL_NAMES))
def test_the_name_call_anchor_would_catch_a_real_offender(name):
    # Proves `_call_names` actually recognises this shape, so a silent typo
    # in the walk above can't make `test_no_sg_or_sg_event_calls` vacuous.
    tree = ast.parse(f"{name}(1)\n")
    assert name in _call_names(tree)


@pytest.mark.parametrize("attr", sorted(_FORBIDDEN_METHODS))
def test_the_method_call_anchor_would_catch_a_real_offender(attr):
    tree = ast.parse(f"thing.{attr}(1)\n")
    assert attr in _method_calls(tree)
