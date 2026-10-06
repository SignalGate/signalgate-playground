"""The committed contract files are exactly what the specs generate; every object is closed."""

from __future__ import annotations

import copy
import importlib.util
import json
import sys
from pathlib import Path

import pytest

from tests.contract import contracts, labels, playground_api
from tests.contract.contracts import CONTRACTS_DIR, documents, render
from tests.contract.json_schema import (
    ANY_KEY,
    assert_valid,
    object_schemas,
    schema_problems,
    to_json_schema,
)
from tests.contract.samples import (
    BOOL,
    FLOAT,
    INT,
    NULL,
    SPECS_AND_SAMPLES,
    STR,
    Enum,
    ListOf,
    MapOf,
    Obj,
    Opt,
    assert_same_shape,
)

_SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "export_contracts.py"


def _load_export_script():
    spec = importlib.util.spec_from_file_location("export_contracts", _SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _committed(name: str) -> dict:
    return json.loads((CONTRACTS_DIR / name).read_text(encoding="utf-8"))


# --- in sync with the specs -------------------------------------------------------


@pytest.mark.parametrize("name", sorted(documents()))
def test_every_contract_file_is_exactly_what_its_specs_generate(name):
    committed = (CONTRACTS_DIR / name).read_text(encoding="utf-8")
    assert committed == render(documents()[name]), (
        f"contracts/{name} is out of date: run `uv run python scripts/export_contracts.py`"
    )


def test_the_contracts_directory_holds_only_the_generated_files():
    assert sorted(path.name for path in CONTRACTS_DIR.iterdir()) == sorted(documents())


def test_the_export_script_reports_and_rewrites_stale_files(tmp_path, monkeypatch):
    script = _load_export_script()
    assert script.main(["--check"]) == 0

    monkeypatch.setattr(contracts, "CONTRACTS_DIR", tmp_path)
    assert script.main(["--check"]) == 1
    assert list(tmp_path.iterdir()) == []
    assert script.main([]) == 0
    for name, document in documents().items():
        assert (tmp_path / name).read_text(encoding="utf-8") == render(document)
    assert script.main(["--check"]) == 0


# --- closed objects ------------------------------------------------------------------


@pytest.mark.parametrize("name", sorted(documents()))
def test_every_object_in_every_contract_file_is_closed(name):
    schemas = object_schemas(_committed(name))
    assert schemas
    for path, schema in schemas:
        assert schema.get("additionalProperties") is False, path
        assert "properties" in schema or "patternProperties" in schema, path


def test_the_playground_api_covers_every_route_and_its_error_envelope():
    document = _committed(contracts.PLAYGROUND_API_FILE)
    assert list(document["routes"]) == list(playground_api.ROUTES)
    lease = document["routes"]["lease"]["responses"]
    assert lease["429"]["headers"] == [playground_api.RETRY_AFTER]
    error = lease["409"]["body"]
    assert error["properties"]["error"]["properties"]["code"] == {
        "enum": ["KEY_ROTATED", "PLAN_CHANGED"]
    }
    assert document["routes"]["end_session"]["responses"]["204"] == {"body": None}
    header = document["request_headers"]["api"]["schema"]["properties"]
    assert header[playground_api.PROTOCOL_HEADER] == {"const": playground_api.PROTOCOL_VERSION}


# --- the JSON Schema subset agrees with assert_same_shape ------------------------------


@pytest.mark.parametrize(
    ("name", "sample"),
    [
        pytest.param(name, sample, id=f"{name}-{index}")
        for name, (_, examples) in SPECS_AND_SAMPLES.items()
        for index, sample in enumerate(examples)
    ],
)
def test_every_control_sample_matches_its_json_schema(name, sample):
    spec, _ = SPECS_AND_SAMPLES[name]
    assert_valid(sample, to_json_schema(spec))
    if isinstance(sample, dict):
        widened = {**copy.deepcopy(sample), "unlisted": 0}
        problems = schema_problems(widened, to_json_schema(spec))
        if isinstance(spec, Obj):
            assert problems


def test_the_committed_app_control_file_validates_the_control_samples():
    routes = _committed(contracts.APP_CONTROL_FILE)["routes"]
    for route, name in (
        ("health", "health"),
        ("metrics", "metrics"),
        ("ledger", "ledger"),
        ("calls", "calls"),
        ("canary", "canary"),
        ("shutdown", "shutdown"),
        ("inbox", "inbox"),
    ):
        _, examples = SPECS_AND_SAMPLES[name]
        for sample in examples:
            assert_valid(sample, routes[route]["responses"]["200"]["body"])
    for sample in SPECS_AND_SAMPLES["canary_request"][1]:
        assert_valid(sample, routes["canary"]["request"])
    assert_valid(
        SPECS_AND_SAMPLES["validation_error"][1][0], routes["ledger"]["responses"]["400"]["body"]
    )


def test_kinds_enums_and_unions_become_their_json_schema():
    assert to_json_schema(STR) == {"type": "string"}
    assert to_json_schema(INT | NULL) == {"type": ["integer", "null"]}
    assert to_json_schema(INT | FLOAT) == {"type": "number"}
    assert to_json_schema(Enum("a")) == {"const": "a"}
    assert to_json_schema(Enum(1, 2)) == {"enum": [1, 2]}
    assert to_json_schema(NULL | {"a": BOOL}) == {
        "anyOf": [
            {"type": "null"},
            {
                "type": "object",
                "properties": {"a": {"type": "boolean"}},
                "required": ["a"],
                "additionalProperties": False,
            },
        ]
    }
    assert to_json_schema(ListOf(STR)) == {"type": "array", "items": {"type": "string"}}


def test_optional_keys_are_listed_but_not_required_and_maps_stay_closed():
    schema = to_json_schema(Obj({"a": INT, "b": Opt(STR)}))
    assert schema["required"] == ["a"]
    assert schema["properties"]["b"] == {"type": "string"}
    assert schema["additionalProperties"] is False
    assert to_json_schema(MapOf(INT)) == {
        "type": "object",
        "patternProperties": {ANY_KEY: {"type": "integer"}},
        "additionalProperties": False,
    }
    with pytest.raises(TypeError):
        to_json_schema(Opt(STR))


@pytest.mark.parametrize(
    ("value", "spec"),
    [
        ({"a": 1}, Obj({"a": INT, "b": Opt(STR)})),
        ({"a": 1, "b": "x"}, Obj({"a": INT, "b": Opt(STR)})),
        ({"k": 1, "j": 2}, MapOf(INT)),
        (None, INT | NULL),
        (2, Enum(1, 2)),
        (1.5, FLOAT),
        ([{"a": True}], ListOf({"a": BOOL})),
    ],
)
def test_values_the_spec_accepts_the_schema_accepts(value, spec):
    assert_same_shape(value, spec)
    assert schema_problems(value, to_json_schema(spec)) == []


@pytest.mark.parametrize(
    ("value", "spec"),
    [
        ({}, Obj({"a": INT})),
        ({"a": 1, "c": 0}, Obj({"a": INT})),
        ({"a": "1"}, Obj({"a": INT})),
        ({"a": True}, Obj({"a": INT})),
        ({"k": "1"}, MapOf(INT)),
        (1, Enum(True)),
        (True, Enum(1)),
        ("x", INT | NULL),
        ([1, "2"], ListOf(INT)),
        ({"a": {"b": 1, "c": 2}}, Obj({"a": NULL | {"b": INT}})),
    ],
)
def test_values_the_spec_refuses_the_schema_refuses(value, spec):
    with pytest.raises(AssertionError):
        assert_same_shape(value, spec)
    assert schema_problems(value, to_json_schema(spec))


# --- the labels file --------------------------------------------------------------------


def test_every_label_follows_the_label_spec_and_quotes_something():
    assert_same_shape(labels.LABELS, labels.LABELS_SPEC)
    for key, entry in labels.LABELS.items():
        assert any(quoted in entry for quoted in labels.QUOTED_KEYS), key
    document = _committed(contracts.LABELS_FILE)
    assert_valid(document["labels"], document["schema"])
    assert document["labels"] == labels.LABELS


def test_labels_hold_no_file_paths():
    for key, entry in labels.LABELS.items():
        for value in entry.values():
            assert "/" not in value and ".tsx" not in value, key
