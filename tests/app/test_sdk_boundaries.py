"""Boundaries the kit must respect: where the private SDK import may live,
that the master never touches the SDK, the exact pin, that the kit holds no
numeric literal used as a timeout or duration, and that it forwards the
caller's own timeout and ids unchanged.
"""

from __future__ import annotations

import ast
import re
from collections.abc import Iterator
from pathlib import Path

import pytest

from app.kit.local_constants import CANARY_DELAY_MAX_MS

REPO_ROOT = Path(__file__).resolve().parents[2]
KIT_TRANSPORT_RELATIVE = "app/kit/kit_transport.py"

# Call names that read as "wait this long", the same shape another guard
# elsewhere in this project looks for (tests/test_no_timing_constants.py).
_DURATION_CALL_NAMES = {"sleep", "wait_for", "wait"}
_DURATION_KEYWORDS = {"timeout", "timeout_s", "timeout_ms"}
_DURATION_NAME_WORDS = ("SECONDS", "TIMEOUT", "DELAY", "INTERVAL", "GRACE")
_DURATION_NAME_SUFFIXES = ("_S", "_MS")

# Milliseconds become seconds by dividing by this. That divisor is a unit
# conversion, not a duration, so it is the one number a duration may hold.
_MS_PER_S = 1000

# The keyword arguments the forward call must hand on under their own names.
_FORWARDED_KEYWORDS = ("timeout_ms", "request_id", "idempotency_key")

# The one function that can load a module by its dotted string name, the way
# ``import`` and ``from ... import`` load one by name in source.
_IMPORT_MODULE_FUNCTION = "import_module"


def _iter_py_files(root: Path):
    yield from sorted(root.rglob("*.py"))


def _parse(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _is_import_module_call(node: ast.Call) -> bool:
    func = node.func
    if isinstance(func, ast.Name):
        return func.id == _IMPORT_MODULE_FUNCTION
    return isinstance(func, ast.Attribute) and func.attr == _IMPORT_MODULE_FUNCTION


def _imported_modules(tree: ast.Module) -> set[str]:
    """Every dotted module (or ``module.name``) this file imports: from
    ``import``, from ``from ... import`` (both the module and each imported
    name), and from an ``importlib.import_module("...")`` string literal."""
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
            names.update(f"{node.module}.{alias.name}" for alias in node.names)
        elif (
            isinstance(node, ast.Call)
            and _is_import_module_call(node)
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
        ):
            names.add(node.args[0].value)
    return names


# --- the private import lives only in kit_transport.py ------------------------------


def test_signalgate_http_is_imported_only_in_the_kit_transport_module():
    offenders = []
    for root in (REPO_ROOT / "app", REPO_ROOT / "playground"):
        for path in _iter_py_files(root):
            imported = _imported_modules(_parse(path))
            if any(
                name == "signalgate._http" or name.startswith("signalgate._http.")
                for name in imported
            ):
                offenders.append(path.relative_to(REPO_ROOT).as_posix())
    assert offenders == [KIT_TRANSPORT_RELATIVE]


# --- the master never imports the SDK at all -----------------------------------------


def test_nothing_under_playground_imports_signalgate():
    offenders = []
    for path in _iter_py_files(REPO_ROOT / "playground"):
        imported = _imported_modules(_parse(path))
        if any(name == "signalgate" or name.startswith("signalgate.") for name in imported):
            offenders.append(path.relative_to(REPO_ROOT).as_posix())
    assert offenders == []


# --- the pin is exactly 0.3.2, in both files ------------------------------------------


def test_pyproject_pins_signalgate_exactly():
    text = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert re.search(r'"signalgate==0\.3\.2"', text) is not None


def test_lockfile_holds_the_pinned_version():
    text = (REPO_ROOT / "uv.lock").read_text(encoding="utf-8")
    assert re.search(r'name = "signalgate"\nversion = "0\.3\.2"', text) is not None
    assert re.search(r'\{ name = "signalgate", specifier = "==0\.3\.2" \}', text) is not None


# --- no literal timeout or duration ----------------------------------------------------


def _kit_source() -> str:
    return (REPO_ROOT / KIT_TRANSPORT_RELATIVE).read_text(encoding="utf-8")


def _is_number(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Constant)
        and isinstance(node.value, (int, float))
        and not isinstance(node.value, bool)
    )


def _unit_divisors(tree: ast.AST) -> set[int]:
    """The ids of the literal divisors in every ``x / 1000`` unit conversion."""
    return {
        id(node.right)
        for node in ast.walk(tree)
        if isinstance(node, ast.BinOp)
        and isinstance(node.op, ast.Div)
        and _is_number(node.right)
        and node.right.value == _MS_PER_S
    }


def _identifier(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _call_name(node: ast.Call) -> str | None:
    return _identifier(node.func)


def _looks_like_a_duration_name(name: str) -> bool:
    """Broad: any name that could hold or label a duration."""
    upper = name.upper()
    if any(word in upper for word in _DURATION_NAME_WORDS):
        return True
    return any(upper.endswith(suffix) for suffix in _DURATION_NAME_SUFFIXES)


def _holds_a_duration_value(name: str) -> bool:
    """Narrow: a name that holds a duration value, such as ``timeout_ms``."""
    lower = name.lower()
    return lower in ("timeout", "delay") or lower.endswith(("_ms", "_s"))


def _mentions_a_duration(node: ast.AST) -> bool:
    for sub in ast.walk(node):
        name = _identifier(sub)
        if name is not None and _holds_a_duration_value(name):
            return True
    return False


def _call_values(node: ast.Call) -> list[ast.expr]:
    return [*node.args, *(kw.value for kw in node.keywords)]


def _defaults(node: ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda):
    """``(parameter, default)`` for every parameter that has a default."""
    args = node.args
    positional = [*args.posonlyargs, *args.args]
    with_defaults = positional[len(positional) - len(args.defaults) :]
    yield from zip(with_defaults, args.defaults)
    for parameter, default in zip(args.kwonlyargs, args.kw_defaults):
        if default is not None:
            yield parameter, default


def _duration_expressions(tree: ast.AST) -> Iterator[tuple[ast.AST, str]]:
    """Every expression that holds or feeds a duration, with where it sits."""
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            name = _call_name(node)
            if name in _DURATION_CALL_NAMES:
                for value in _call_values(node):
                    yield value, f"an argument to {name}()"
            for kw in node.keywords:
                if kw.arg is not None and (
                    kw.arg in _DURATION_KEYWORDS or _looks_like_a_duration_name(kw.arg)
                ):
                    yield kw.value, f"the {kw.arg}= keyword"
            for value in _call_values(node):
                if _mentions_a_duration(value):
                    yield value, "a call argument built from a duration"
        elif isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
            if node.value is None:
                continue
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            names = [
                name
                for target in targets
                for sub in ast.walk(target)
                if (name := _identifier(sub)) is not None
            ]
            if any(_looks_like_a_duration_name(name) for name in names):
                yield node.value, "a value given to a duration name"
            elif _mentions_a_duration(node.value):
                yield node.value, "a value built from a duration"
        elif isinstance(node, ast.Compare):
            if _mentions_a_duration(node):
                yield node, "a comparison with a duration"
        elif isinstance(node, ast.Return):
            if node.value is not None and _mentions_a_duration(node.value):
                yield node.value, "a return value built from a duration"
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            for parameter, default in _defaults(node):
                if _looks_like_a_duration_name(parameter.arg):
                    yield default, f"the default of {parameter.arg}"


def _duration_literal_violations(source: str) -> list[str]:
    """Every numeric literal inside a duration expression, bar a ``/ 1000`` divisor."""
    tree = ast.parse(source)
    allowed = _unit_divisors(tree)
    violations = set()
    for expression, where in _duration_expressions(tree):
        for sub in ast.walk(expression):
            if _is_number(sub) and id(sub) not in allowed:
                violations.add(f"line {sub.lineno}: literal {sub.value!r} in {where}")
    return sorted(violations)


def _is_forward_call(node: ast.AST) -> bool:
    """A call to ``self._inner.post(...)``: the kit handing a request on."""
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "post"
        and _identifier(node.func.value) == "_inner"
    )


def _forward_call_violations(source: str) -> list[str]:
    """How the one forward call departs from handing on the caller's own values."""
    forwards = [node for node in ast.walk(ast.parse(source)) if _is_forward_call(node)]
    if len(forwards) != 1:
        return [f"expected exactly one forward call, found {len(forwards)}"]
    (call,) = forwards
    violations = []
    positional = [_identifier(arg) if isinstance(arg, ast.Name) else None for arg in call.args]
    if positional != ["url", "body"]:
        violations.append(f"positional arguments are {positional}, not the caller's url and body")
    keywords = {kw.arg: kw.value for kw in call.keywords}
    if set(keywords) != set(_FORWARDED_KEYWORDS):
        violations.append(f"keyword arguments are {sorted(map(str, keywords))}")
    for name in _FORWARDED_KEYWORDS:
        value = keywords.get(name)
        if not (isinstance(value, ast.Name) and value.id == name):
            violations.append(f"{name}= is not the caller's own {name}")
    return violations


def test_kit_transport_holds_no_numeric_literal_used_as_a_timeout():
    assert _duration_literal_violations(_kit_source()) == []


def test_kit_transport_forwards_the_callers_own_timeout_and_ids():
    assert _forward_call_violations(_kit_source()) == []


# A stand-in number for the edits below. Any number would do; this one is
# derived from an app constant rather than written fresh.
_N = CANARY_DELAY_MAX_MS // 2

# Each pair is an exact piece of the real kit source and an edit that sneaks
# a literal duration into it.
LITERAL_EDITS = [
    pytest.param("timeout_ms=timeout_ms,", f"timeout_ms=max(timeout_ms, {_N}),", id="forward-max"),
    pytest.param(
        "timeout_ms=timeout_ms,", f"timeout_ms=timeout_ms or {_N},", id="forward-or-default"
    ),
    pytest.param("timeout_ms=timeout_ms,", f"timeout_ms={_N},", id="forward-literal"),
    pytest.param("time.sleep(wait_ms / 1000.0)", f"time.sleep({_N} / 1000.0)", id="sleep-literal"),
    pytest.param(
        "time.sleep(wait_ms / 1000.0)",
        f"time.sleep(wait_ms / 1000.0 + {_N})",
        id="sleep-padded",
    ),
    pytest.param(
        "wait_ms = min(canary.delay_ms, timeout_ms)",
        f"wait_ms = min(canary.delay_ms, {_N})",
        id="canary-wait-cap",
    ),
    pytest.param(
        "if canary.delay_ms >= timeout_ms:",
        f"if canary.delay_ms >= {_N}:",
        id="canary-timeout-comparison",
    ),
    pytest.param(
        "self._answer_canary(canary, endpoint, url, timeout_ms, request_id)",
        f"self._answer_canary(canary, endpoint, url, max(timeout_ms, {_N}), request_id)",
        id="canary-argument",
    ),
    pytest.param(
        "        timeout_ms: int,\n        request_id: str,\n        idempotency_key: str,\n",
        f"        timeout_ms: int = {_N},\n        request_id: str,\n"
        "        idempotency_key: str,\n",
        id="parameter-default",
    ),
    pytest.param(
        "\n\ndef user_agent()",
        f"\n\n_FALLBACK_TIMEOUT_MS = {_N}\n\n\ndef user_agent()",
        id="module-constant",
    ),
]

# Each pair is an exact piece of the real forward call and an edit that
# stops it handing on the caller's own value.
FORWARD_EDITS = [
    pytest.param(
        "timeout_ms=timeout_ms,", "timeout_ms=self._timeout_ms,", id="timeout-from-elsewhere"
    ),
    pytest.param(
        "                    request_id=request_id,\n",
        "                    request_id=request_id.upper(),\n",
        id="request-id-rewritten",
    ),
    pytest.param(
        "                    idempotency_key=idempotency_key,\n",
        "                    idempotency_key=str(idempotency_key),\n",
        id="idempotency-key-rewritten",
    ),
    pytest.param(
        "                    url,\n                    body,\n",
        "                    url,\n                    dict(body),\n",
        id="body-copied",
    ),
]


def _edited_kit_source(original: str, edit: str) -> str:
    source = _kit_source()
    assert source.count(original) == 1, f"{original!r} no longer appears exactly once"
    return source.replace(original, edit)


@pytest.mark.parametrize(("original", "edit"), LITERAL_EDITS)
def test_the_literal_scan_catches_a_literal_duration_edited_into_the_kit(original, edit):
    assert _duration_literal_violations(_edited_kit_source(original, edit)) != []


@pytest.mark.parametrize(("original", "edit"), FORWARD_EDITS)
def test_the_forward_scan_catches_a_forward_that_changes_the_callers_values(original, edit):
    assert _forward_call_violations(_edited_kit_source(original, edit)) != []
