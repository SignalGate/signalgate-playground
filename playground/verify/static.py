"""Static checks on ``app/routes.py``, with the stdlib ``ast`` only.

They read the code, never run it, and ignore comments. Four facts are
certain enough to fail a gate on their own:

- an API key written in the code (``KEY_IN_CODE``);
- a live ``check()`` call in chapter 3 (``CHECK_TOO_EARLY``);
- the SignalGate SDK's ``Client(...)`` built outside the one shared client
  (``CLIENT_PER_REQUEST``): ``signalgate.Client(...)``, through any alias of
  the package, or a ``Client`` imported from it. Any other ``Client``, such
  as ``httpx.Client()``, is not the SDK's;
- ``sg().check(...)`` called directly inside an ``async def``
  (``CHECK_BLOCKS_EVENT_LOOP``).

Everything else the verifier finds comes from what the app actually did;
``call_line`` only helps point such a finding at the right line.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

from playground.verify import findings as F
from playground.verify.evidence import Sources
from playground.verify.model import Finding

# The full shape of an API key, bounded so a longer token doesn't match.
KEY_SHAPE = re.compile(r"(?<![A-Za-z0-9_-])pk_live_[A-Za-z0-9_-]{32}(?![A-Za-z0-9_-])")

# Calls that hand back the app's shared client.
_CLIENT_GETTERS = frozenset({"sg", "get_client"})

# The SDK's package, and the name of its client class.
_SDK = "signalgate"
_CLIENT = "Client"

_FUNCTIONS = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)


class SourceUnreadable(Exception):
    """The file is missing or isn't valid Python."""


def parse(path: Path) -> ast.Module:
    try:
        return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (OSError, SyntaxError, ValueError) as error:
        raise SourceUnreadable(str(path)) from error


def _callee_name(node: ast.expr) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _is_client_getter(node: ast.expr) -> bool:
    return isinstance(node, ast.Call) and _callee_name(node.func) in _CLIENT_GETTERS


def _is_sdk(module: str | None) -> bool:
    return module is not None and (module == _SDK or module.startswith(_SDK + "."))


def _sdk_names(tree: ast.Module) -> tuple[set[str], set[str]]:
    """The names this file binds to the SDK's package (or a module of it),
    and to its ``Client`` class, wherever it imports them."""
    modules: set[str] = set()
    clients: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if _is_sdk(alias.name):
                    # ``import signalgate.x`` binds ``signalgate``; ``as y`` binds y.
                    modules.add(alias.asname or alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and _is_sdk(node.module):
            for alias in node.names:
                if alias.name in (_CLIENT, "*"):
                    clients.add(alias.asname or _CLIENT)
    return modules, clients


def _root_name(node: ast.expr) -> str | None:
    while isinstance(node, ast.Attribute):
        node = node.value
    return node.id if isinstance(node, ast.Name) else None


class _Scan(ast.NodeVisitor):
    def __init__(self, tree: ast.Module) -> None:
        # What this file calls the SDK's package and its client class.
        self._sdk_modules, self._sdk_clients = _sdk_names(tree)
        self.key_lines: list[int] = []
        self.client_lines: list[int] = []
        self.check_lines: list[int] = []
        self.async_check_lines: list[tuple[int, str]] = []
        self._functions: list[ast.AST] = []
        # Names bound to the shared client (``client = sg()``), per scope.
        self._bound: list[set[str]] = [set()]

    def _visit_function(self, node: ast.AST) -> None:
        self._functions.append(node)
        self._bound.append(set())
        self.generic_visit(node)
        self._bound.pop()
        self._functions.pop()

    visit_FunctionDef = _visit_function
    visit_AsyncFunctionDef = _visit_function
    visit_Lambda = _visit_function

    def visit_Assign(self, node: ast.Assign) -> None:
        if _is_client_getter(node.value):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    self._bound[-1].add(target.id)
        self.generic_visit(node)

    def visit_Constant(self, node: ast.Constant) -> None:
        if isinstance(node.value, str) and KEY_SHAPE.search(node.value):
            self.key_lines.append(node.lineno)

    def _is_shared_client(self, node: ast.expr) -> bool:
        if _is_client_getter(node):
            return True
        return isinstance(node, ast.Name) and any(node.id in names for names in self._bound)

    def _is_sdk_client(self, func: ast.expr) -> bool:
        """True for the SDK's ``Client``: ``signalgate.Client`` or an imported name."""
        if isinstance(func, ast.Name):
            return func.id in self._sdk_clients
        return (
            isinstance(func, ast.Attribute)
            and func.attr == _CLIENT
            and _root_name(func.value) in self._sdk_modules
        )

    def visit_Call(self, node: ast.Call) -> None:
        func = node.func
        if self._is_sdk_client(func):
            self.client_lines.append(node.lineno)
        if (
            isinstance(func, ast.Attribute)
            and func.attr == "check"
            and self._is_shared_client(func.value)
        ):
            self.check_lines.append(node.lineno)
            innermost = self._functions[-1] if self._functions else None
            if isinstance(innermost, ast.AsyncFunctionDef):
                self.async_check_lines.append((node.lineno, innermost.name))
        self.generic_visit(node)


def check_source(path: Path, *, chapter: int, sources: Sources) -> list[Finding]:
    """The certain facts in ``path`` for ``chapter``, as findings with their lines.

    Raises ``SourceUnreadable`` for a missing file or a syntax error.
    """
    tree = parse(path)
    scan = _Scan(tree)
    scan.visit(tree)
    file = sources.relative(path)
    found: list[Finding] = []
    for line in scan.key_lines:
        found.append(F.finding(F.KEY_IN_CODE, sources.site(file, line)))
    for line in scan.client_lines:
        found.append(F.finding(F.CLIENT_PER_REQUEST, sources.site(file, line), "static"))
    if chapter < 5:
        for line in scan.check_lines:
            found.append(F.finding(F.CHECK_TOO_EARLY, sources.site(file, line), "static"))
    else:
        for line, function in scan.async_check_lines:
            found.append(
                F.finding(
                    F.CHECK_BLOCKS_EVENT_LOOP,
                    sources.site(file, line),
                    "static",
                    function=function,
                )
            )
    return found


def call_line(path: Path, name: str) -> int | None:
    """The first line that calls ``name(...)`` in ``path``, or None."""
    try:
        tree = parse(path)
    except SourceUnreadable:
        return None
    lines = [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and _callee_name(node.func) == name
    ]
    return min(lines) if lines else None


__all__ = ["KEY_SHAPE", "SourceUnreadable", "call_line", "check_source", "parse"]
