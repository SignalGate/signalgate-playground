"""Every canonical sample matches its own spec, and the shape check isn't vacuous."""

from __future__ import annotations

import copy
import json

import pytest

from tests.contract import samples
from tests.contract.samples import (
    ATTEMPT_RECORD,
    BOOL,
    CALL_RECORD,
    CONSTRUCT_RECORD,
    FLOAT,
    INT,
    NULL,
    SPECS_AND_SAMPLES,
    STR,
    Enum,
    J,
    ListOf,
    MapOf,
    Obj,
    Opt,
    assert_same_shape,
    record_spec,
)


@pytest.mark.parametrize(
    ("name", "sample"),
    [
        pytest.param(name, sample, id=f"{name}-{index}")
        for name, (_, examples) in SPECS_AND_SAMPLES.items()
        for index, sample in enumerate(examples)
    ],
)
def test_every_sample_matches_its_own_spec(name, sample):
    spec, _ = SPECS_AND_SAMPLES[name]
    assert_same_shape(sample, spec)


@pytest.mark.parametrize("name", sorted(SPECS_AND_SAMPLES))
def test_every_sample_survives_a_json_round_trip(name):
    spec, examples = SPECS_AND_SAMPLES[name]
    for sample in examples:
        assert_same_shape(json.loads(json.dumps(sample)), spec)


def test_every_record_kind_has_a_spec_and_a_sample():
    kinds = {sample["kind"] for sample in samples.SAMPLE_CALLS["records"]}
    assert kinds == set(samples.RECORD_SPECS) == {"call", "attempt", "construct"}
    assert record_spec(samples.SAMPLE_CALL) is CALL_RECORD
    assert record_spec(samples.SAMPLE_ATTEMPT) is ATTEMPT_RECORD
    assert record_spec(samples.SAMPLE_CONSTRUCT) is CONSTRUCT_RECORD


def test_record_samples_never_hold_request_contents():
    forbidden_keys = {"encrypted", "v", "custom", "body", "headers", "api_key", "key"}
    for sample in (samples.SAMPLE_CALL, samples.SAMPLE_ATTEMPT, samples.SAMPLE_CONSTRUCT):
        assert forbidden_keys.isdisjoint(sample)
    for spec in (CALL_RECORD, ATTEMPT_RECORD, CONSTRUCT_RECORD):
        assert forbidden_keys.isdisjoint(spec.fields)


# --- the checker itself --------------------------------------------------------


def _fails(actual, spec) -> str:
    with pytest.raises(AssertionError) as caught:
        assert_same_shape(actual, spec)
    return str(caught.value)


def test_a_missing_key_fails():
    sample = copy.deepcopy(samples.SAMPLE_HEALTH)
    del sample["key"]
    assert "missing keys ['key']" in _fails(sample, samples.HEALTH)


def test_health_reports_an_outcome_for_every_self_test_check():
    sample = copy.deepcopy(samples.SAMPLE_HEALTH)
    del sample["self_test"]["checks"]["exit_hook"]
    assert "$.self_test.checks" in _fails(sample, samples.HEALTH)
    sample = copy.deepcopy(samples.SAMPLE_HEALTH)
    sample["self_test"]["checks"]["exit_hook"] = "skip"
    assert_same_shape(sample, samples.HEALTH)
    sample["self_test"]["checks"]["exit_hook"] = "unknown"
    assert "$.self_test.checks.exit_hook" in _fails(sample, samples.HEALTH)


def test_an_extra_key_fails():
    sample = {**samples.SAMPLE_CANARY, "extra": 0}
    assert "unexpected keys ['extra']" in _fails(sample, samples.CANARY)


def test_a_nested_extra_key_fails():
    sample = copy.deepcopy(samples.SAMPLE_CALL)
    sample["site"]["column"] = 0
    assert "$.site" in _fails(sample, CALL_RECORD)


def test_a_wrong_type_fails_with_its_path():
    sample = {**samples.SAMPLE_ATTEMPT, "attempt": "1"}
    assert "$.attempt" in _fails(sample, ATTEMPT_RECORD)


def test_a_bool_is_never_an_int_and_an_int_is_never_a_float():
    _fails(True, INT)
    _fails(0, BOOL)
    _fails(0, FLOAT)
    assert_same_shape(0.0, FLOAT)


def test_null_only_where_the_spec_allows_it():
    assert_same_shape(None, INT | NULL)
    _fails(None, INT)
    _fails({**samples.SAMPLE_ATTEMPT, "t0": None}, ATTEMPT_RECORD)
    assert_same_shape({**samples.SAMPLE_CALL, "t0": None}, CALL_RECORD)


def test_enums_compare_with_the_value_type():
    assert_same_shape("422", Enum("422"))
    _fails(422, Enum("422"))
    _fails(1, Enum(True))
    _fails(True, Enum(1))
    _fails("other", Enum("check", "log"))


@pytest.mark.parametrize(
    "value", ["text", 3, 2.5, True, False, None, {"type": "dict"}, {"type": "NoneType"}]
)
def test_j_accepts_scalars_and_type_markers(value):
    assert_same_shape(value, J)


@pytest.mark.parametrize(
    "value", [[], {}, {"type": 1}, {"type": "dict", "value": 1}, {"kind": "dict"}, ["x"]]
)
def test_j_refuses_anything_else(value):
    _fails(value, J)


def test_lists_and_maps_check_every_item():
    assert_same_shape([], ListOf(STR))
    assert_same_shape(["a", "b"], ListOf(STR))
    assert "$[1]" in _fails(["a", 2], ListOf(STR))
    _fails("ab", ListOf(STR))
    assert_same_shape({}, MapOf(INT))
    assert_same_shape({"a": 1}, MapOf(INT))
    _fails({"a": "1"}, MapOf(INT))
    _fails([("a", 1)], MapOf(INT))


def test_a_union_of_objects_picks_by_shape():
    entry = samples.LEDGER_ENTRY
    for sample in samples.SAMPLE_LEDGER["entries"]:
        assert_same_shape(sample, entry)
    _fails({"kind": "inbox", "seq": 1, "ref": "r", "run": "", "ok": True}, entry)
    _fails({"kind": "unknown", "seq": 1, "ref": "r", "run": ""}, entry)


def test_a_plain_dict_reads_as_an_object_spec():
    assert_same_shape({"a": 1}, {"a": INT})
    _fails({"a": 1, "b": 2}, {"a": INT})
    assert_same_shape({"a": None}, Obj({"a": NULL | {"b": STR}}))
    assert_same_shape({"a": {"b": "x"}}, Obj({"a": NULL | {"b": STR}}))


def test_an_optional_key_may_be_left_out_but_must_match_when_present():
    spec = Obj({"a": INT, "b": Opt(STR)})
    assert_same_shape({"a": 1}, spec)
    assert_same_shape({"a": 1, "b": "x"}, spec)
    assert "$.b" in _fails({"a": 1, "b": 2}, spec)
    assert "missing keys ['a']" in _fails({"b": "x"}, spec)
    assert "unexpected keys ['c']" in _fails({"a": 1, "c": 0}, spec)


def test_optional_goes_around_a_whole_key_spec_only():
    with pytest.raises(TypeError):
        Opt(STR) | NULL
    with pytest.raises(TypeError):
        NULL | Opt(STR)
    with pytest.raises(TypeError):
        Opt(Opt(STR))
    assert_same_shape({"a": None}, Obj({"a": Opt(STR | NULL)}))
