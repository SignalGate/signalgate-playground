"""AST guard: `playground/machine` holds no hardcoded user-facing copy.

Every user-facing string must be looked up by key in ``playground.copy.en``,
not spelled out in the state machine. This parses every module under
``playground/machine`` and fails if it finds a string literal that isn't one
of:

- a docstring;
- a key present in ``en.TEXT`` (a copy key) or ``en.TASKS`` (a task);
- one of the literal command/answer/expect strings the machine matches
  against (the entries of ``COMMANDS``, ``ANSWERS`` and ``Expect``), a
  reload cause, or a block name (``Block``);
- a literal used only inside ``to_dict``/``from_dict``, for state
  serialisation (dict keys and the values round-tripped through them);
- the attribute name in ``object.__setattr__(self, "<name>", value)``, the
  way a frozen dataclass sets one of its own fields.
"""

from __future__ import annotations

import ast
import typing
from pathlib import Path

from playground.copy.en import TASKS, TEXT
from playground.machine.events import ANSWERS, COMMANDS, Block, Expect, ReloadCause

MACHINE_DIR = Path(__file__).resolve().parents[2] / "playground" / "machine"

_ALLOWED_LITERALS = (
    set(TEXT)
    | set(TASKS)
    | set(COMMANDS)
    | set(ANSWERS)
    | set(typing.get_args(Expect))
    | set(typing.get_args(ReloadCause))
    | set(typing.get_args(Block))
)

_SERIALISATION_FUNCTIONS = frozenset({"to_dict", "from_dict"})

_DOC_OWNERS = (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)


def _is_docstring_expr(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Expr)
        and isinstance(node.value, ast.Constant)
        and isinstance(node.value.value, str)
    )


def _docstring_ids(tree: ast.AST) -> set[int]:
    """String-literal node ids that are a module/class/function docstring."""
    ids: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, _DOC_OWNERS):
            body = getattr(node, "body", [])
            if body and _is_docstring_expr(body[0]):
                ids.add(id(body[0].value))
    return ids


def _dunder_all_ids(tree: ast.AST) -> set[int]:
    """String-literal node ids that are entries of a module's ``__all__``."""
    ids: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        targets = [t.id for t in node.targets if isinstance(t, ast.Name)]
        if "__all__" not in targets:
            continue
        if isinstance(node.value, (ast.List, ast.Tuple)):
            for element in node.value.elts:
                if isinstance(element, ast.Constant) and isinstance(element.value, str):
                    ids.add(id(element))
    return ids


def _serialisation_ids(tree: ast.AST) -> set[int]:
    """String-literal node ids that only ever appear inside to_dict/from_dict."""
    ids: set[int] = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name in _SERIALISATION_FUNCTIONS
        ):
            for sub in ast.walk(node):
                if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
                    ids.add(id(sub))
    return ids


def _set_field_ids(tree: ast.AST) -> set[int]:
    """String-literal node ids naming the field in ``object.__setattr__(self, name, value)``."""
    ids: set[int] = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "__setattr__"
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "object"
            and len(node.args) == 3
            and isinstance(node.args[1], ast.Constant)
            and isinstance(node.args[1].value, str)
        ):
            ids.add(id(node.args[1]))
    return ids


def find_string_literal_violations(source: str, filename: str = "<string>") -> list[str]:
    """Return every disallowed string literal found in ``source``."""
    tree = ast.parse(source, filename=filename)
    skip_ids = (
        _docstring_ids(tree)
        | _serialisation_ids(tree)
        | _dunder_all_ids(tree)
        | _set_field_ids(tree)
    )
    offenders = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if id(node) in skip_ids:
                continue
            if node.value in _ALLOWED_LITERALS:
                continue
            offenders.append(node.value)
    return offenders


def _machine_source_files() -> list[Path]:
    return sorted(MACHINE_DIR.rglob("*.py"))


def test_machine_package_has_no_stray_user_facing_string_literals():
    offenders = {}
    for path in _machine_source_files():
        found = find_string_literal_violations(path.read_text(encoding="utf-8"), str(path))
        if found:
            offenders[str(path)] = found
    assert offenders == {}


def test_the_guard_actually_catches_a_hardcoded_string():
    # A control case: prove the checker isn't vacuously passing everything.
    source = 'def greet():\n    """Say hello."""\n    return "Hello there, friend"\n'
    assert find_string_literal_violations(source) == ["Hello there, friend"]


def test_the_guard_allows_only_the_field_name_of_object_setattr():
    source = (
        "def __post_init__(self):\n"
        '    object.__setattr__(self, "block", "Some words for a person")\n'
        '    setattr(self, "other", 1)\n'
    )
    assert find_string_literal_violations(source) == ["Some words for a person", "other"]


def test_the_guard_allows_task_keys_and_block_names():
    source = 'def f():\n    return ("ch3.task", "achievement", "ask")\n'
    assert find_string_literal_violations(source) == []


def test_the_guard_allows_docstrings_copy_keys_and_command_literals():
    source = (
        '"""Module docstring."""\n'
        "\n"
        "class C:\n"
        '    """Class docstring."""\n'
        "\n"
        "    def m(self):\n"
        '        """Method docstring."""\n'
        '        return "console.bye"\n'
        "\n"
        "    def to_dict(self):\n"
        '        return {"phase": "anything-goes-here"}\n'
        "\n"
        '__all__ = ["C"]\n'
    )
    assert find_string_literal_violations(source) == []
