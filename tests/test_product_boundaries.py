"""The product never reaches into the tests, and never names the SDK's server-address variable.

- No module under ``playground/`` or ``app/`` imports anything from
  ``tests``: the fake server, the SDK stub and the contract specs stay test
  fixtures, never a mode of the product.
- No tracked file mentions the environment variable the SDK reads its
  server address from. Its name is read from the installed SDK's own
  source, so it is never spelled out here either.
"""

from __future__ import annotations

import ast
import inspect
import subprocess
from pathlib import Path

import signalgate._constants as sdk_constants

ROOT = Path(__file__).resolve().parents[1]
PRODUCT_DIRS = ("playground", "app")


def _imported_modules(tree: ast.AST) -> list[str]:
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names.append(node.module)
        elif isinstance(node, ast.Call) and getattr(node.func, "attr", None) == "import_module":
            names.extend(
                arg.value
                for arg in node.args[:1]
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str)
            )
    return names


def test_no_product_module_imports_the_tests():
    offenders = []
    for directory in PRODUCT_DIRS:
        for path in sorted((ROOT / directory).rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for name in _imported_modules(tree):
                if name == "tests" or name.startswith("tests."):
                    offenders.append(f"{path.relative_to(ROOT)}: {name}")
    assert offenders == []


def test_the_import_scan_sees_every_import_form():
    tree = ast.parse(
        "import tests.fake_server\n"
        "from tests.support import sdk_stub\n"
        "importlib.import_module('tests.contract')\n"
        "from . import sibling\n"
    )
    assert _imported_modules(tree) == ["tests.fake_server", "tests.support", "tests.contract"]


def _sdk_address_variable() -> str:
    """The variable the installed SDK reads its server address from, found in its source."""
    tree = ast.parse(inspect.getsource(sdk_constants))
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Assign) and len(node.targets) == 1):
            continue
        target = node.targets[0]
        if not (isinstance(target, ast.Name) and target.id == "BASE_URL"):
            continue
        for call in ast.walk(node.value):
            if (
                isinstance(call, ast.Call)
                and getattr(call.func, "attr", None) == "get"
                and getattr(call.func.value, "attr", None) == "environ"
                and call.args
                and isinstance(call.args[0], ast.Constant)
                and isinstance(call.args[0].value, str)
            ):
                return call.args[0].value
    raise AssertionError("the SDK no longer reads its address from the environment")


def _tracked_text_files() -> dict[str, str]:
    listed = subprocess.run(
        ["git", "ls-files", "-z"], cwd=ROOT, check=True, capture_output=True
    ).stdout.decode("utf-8")
    texts = {}
    for name in filter(None, listed.split("\0")):
        try:
            texts[name] = (ROOT / name).read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
    return texts


def test_no_tracked_file_names_the_sdk_address_variable():
    variable = _sdk_address_variable()
    assert variable.isupper() and len(variable) > 8
    offenders = [name for name, text in _tracked_text_files().items() if variable in text]
    assert offenders == []
