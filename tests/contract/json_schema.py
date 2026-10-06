"""Specs from ``samples.py`` as JSON Schema, and the contract documents built from them.

``to_json_schema(spec)`` writes a spec in a small subset of JSON Schema
(draft 2020-12): ``type``, ``const``, ``enum``, ``anyOf``, ``properties``,
``required``, ``patternProperties``, ``items`` and ``additionalProperties``.
Every object schema it writes sets ``additionalProperties: false``: an
``Obj`` lists its keys under ``properties``, and a ``MapOf`` matches every
key through ``patternProperties``, so neither ever lets an unlisted key
through. ``FLOAT`` becomes ``number``, which JSON Schema also lets an
integer match; everything else is as strict as ``assert_same_shape``.

``schema_problems(value, schema)`` checks a value against a schema written
in that same subset, so a test can check a response against a committed
contract file with no extra dependency.

``contract_document`` lays out one contract file: the headers a caller
sets, then every route with its request body and its responses by status.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from tests.contract.samples import Enum, Kind, ListOf, MapOf, Obj, Opt, Spec, Union, as_spec

DIALECT = "https://json-schema.org/draft/2020-12/schema"
ANY_KEY = ".*"

_TYPE_NAMES = {
    "str": "string",
    "int": "integer",
    "float": "number",
    "bool": "boolean",
    "null": "null",
}


def to_json_schema(spec: Any) -> dict[str, Any]:
    """``spec`` as a JSON Schema object (see the module docstring for the subset)."""
    spec = as_spec(spec)
    if isinstance(spec, Kind):
        return {"type": _TYPE_NAMES[spec.name]}
    if isinstance(spec, Enum):
        values = list(spec.values)
        return {"const": values[0]} if len(values) == 1 else {"enum": values}
    if isinstance(spec, Obj):
        return {
            "type": "object",
            "properties": {
                key: to_json_schema(value.spec if isinstance(value, Opt) else value)
                for key, value in spec.fields.items()
            },
            "required": [key for key, value in spec.fields.items() if not isinstance(value, Opt)],
            "additionalProperties": False,
        }
    if isinstance(spec, MapOf):
        return {
            "type": "object",
            "patternProperties": {ANY_KEY: to_json_schema(spec.value)},
            "additionalProperties": False,
        }
    if isinstance(spec, ListOf):
        return {"type": "array", "items": to_json_schema(spec.item)}
    if isinstance(spec, Union):
        if all(isinstance(option, Kind) for option in spec.options):
            types = [_TYPE_NAMES[option.name] for option in spec.options]
            if "number" in types and "integer" in types:
                types.remove("integer")
            return {"type": types[0] if len(types) == 1 else types}
        return {"anyOf": [to_json_schema(option) for option in spec.options]}
    if isinstance(spec, Opt):
        raise TypeError("Opt() is only meaningful as the spec of an Obj key")
    raise TypeError(f"no JSON Schema for {spec!r}")


# --- checking a value against a schema in that subset -------------------------


def _is_type(value: Any, name: str) -> bool:
    if name == "string":
        return isinstance(value, str)
    if name == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if name == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if name == "boolean":
        return isinstance(value, bool)
    if name == "null":
        return value is None
    if name == "object":
        return isinstance(value, dict)
    if name == "array":
        return isinstance(value, list)
    raise AssertionError(f"unknown JSON Schema type {name}")


def _same(value: Any, allowed: Any) -> bool:
    if isinstance(value, bool) or isinstance(allowed, bool):
        return type(value) is type(allowed) and value == allowed
    return value == allowed


def schema_problems(value: Any, schema: Mapping[str, Any], path: str = "$") -> list[str]:
    """Every mismatch between ``value`` and ``schema``, as ``path: reason``."""
    if "anyOf" in schema:
        attempts = [schema_problems(value, option, path) for option in schema["anyOf"]]
        if any(not problems for problems in attempts):
            return []
        return [f"{path}: matches no option", *min(attempts, key=len)]
    if "const" in schema and not _same(value, schema["const"]):
        return [f"{path}: {value!r} is not {schema['const']!r}"]
    if "enum" in schema and not any(_same(value, allowed) for allowed in schema["enum"]):
        return [f"{path}: {value!r} is not one of {schema['enum']!r}"]
    types = schema.get("type")
    if types is not None:
        names = [types] if isinstance(types, str) else list(types)
        if not any(_is_type(value, name) for name in names):
            return [f"{path}: expected {' or '.join(names)}, got {type(value).__name__}"]
    problems: list[str] = []
    if isinstance(value, dict):
        properties = schema.get("properties", {})
        patterns = schema.get("patternProperties", {})
        missing = [key for key in schema.get("required", []) if key not in value]
        if missing:
            problems.append(f"{path}: missing keys {missing}")
        for key, item in value.items():
            matched = False
            if key in properties:
                matched = True
                problems.extend(schema_problems(item, properties[key], f"{path}.{key}"))
            for pattern, item_schema in patterns.items():
                if re.search(pattern, key):
                    matched = True
                    problems.extend(schema_problems(item, item_schema, f"{path}[{key!r}]"))
            if not matched and schema.get("additionalProperties") is False:
                problems.append(f"{path}: unexpected key {key!r}")
    if isinstance(value, list) and "items" in schema:
        for index, item in enumerate(value):
            problems.extend(schema_problems(item, schema["items"], f"{path}[{index}]"))
    return problems


def assert_valid(value: Any, schema: Mapping[str, Any]) -> None:
    """Fail unless ``value`` matches ``schema``."""
    problems = schema_problems(value, schema)
    assert not problems, "schema mismatch:\n" + "\n".join(problems)


# --- contract documents ---------------------------------------------------------


@dataclass(frozen=True)
class Response:
    """One status of a route: its body's spec (None: no body) and the headers it must carry."""

    body: Spec | None
    headers: tuple[str, ...] = ()


@dataclass(frozen=True)
class Route:
    """One route of a contract.

    ``responses`` maps each status the route may answer with to a spec, a
    ``Response``, or None for an empty body. ``query`` names the optional
    query parameters.
    """

    method: str
    path: str
    responses: Mapping[int, Spec | Response | None]
    request: Spec | None = None
    query: tuple[str, ...] = field(default_factory=tuple)

    def response(self, status: int) -> Response:
        """The ``Response`` for ``status``; a KeyError if the route never answers with it."""
        answer = self.responses[status]
        return answer if isinstance(answer, Response) else Response(answer)


@dataclass(frozen=True)
class HeaderSet:
    """Headers a caller sets, and which requests carry them."""

    applies_to: str
    spec: Spec


def _schema_or_none(spec: Spec | None) -> dict[str, Any] | None:
    return None if spec is None else to_json_schema(spec)


def _route_document(route: Route) -> dict[str, Any]:
    document: dict[str, Any] = {"method": route.method, "path": route.path}
    if route.query:
        document["query"] = list(route.query)
    document["request"] = _schema_or_none(route.request)
    responses: dict[str, Any] = {}
    for status in sorted(route.responses):
        response = route.response(status)
        entry: dict[str, Any] = {"body": _schema_or_none(response.body)}
        if response.headers:
            entry["headers"] = list(response.headers)
        responses[str(status)] = entry
    document["responses"] = responses
    return document


def contract_document(
    title: str,
    headers: Mapping[str, HeaderSet],
    routes: Mapping[str, Route],
    *,
    version: int = 1,
) -> dict[str, Any]:
    """One contract file's content: request headers, then every route by name."""
    return {
        "title": title,
        "version": version,
        "dialect": DIALECT,
        "request_headers": {
            name: {"applies_to": header_set.applies_to, "schema": to_json_schema(header_set.spec)}
            for name, header_set in headers.items()
        },
        "routes": {name: _route_document(route) for name, route in routes.items()},
    }


def object_schemas(document: Any) -> list[tuple[str, dict[str, Any]]]:
    """Every object schema anywhere in ``document``, with its path."""
    found: list[tuple[str, dict[str, Any]]] = []

    def visit(node: Any, path: str) -> None:
        if isinstance(node, dict):
            types = node.get("type")
            if isinstance(types, str):
                types = [types]
            if isinstance(types, list) and "object" in types:
                found.append((path, node))
            for key, child in node.items():
                visit(child, f"{path}.{key}")
        elif isinstance(node, list):
            for index, child in enumerate(node):
                visit(child, f"{path}[{index}]")

    visit(document, "$")
    return found


__all__ = [
    "ANY_KEY",
    "DIALECT",
    "HeaderSet",
    "Response",
    "Route",
    "assert_valid",
    "contract_document",
    "object_schemas",
    "schema_problems",
    "to_json_schema",
]
