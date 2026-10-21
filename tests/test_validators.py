from pathlib import Path

import pytest

from tester.dataset import load_dataset
from tester.models import FailureType, Task
from tester.validators import (
    ExactMatchValidator,
    JSONValidator,
    NonEmptyValidator,
    SchemaValidator,
    json_equal,
    validate,
)

EMPTY, FORMAT, WRONG = FailureType.EMPTY_RESPONSE, FailureType.FORMAT_ERROR, FailureType.VALIDATION_ERROR


def check(validator, text, expected, outcome):
    r = validator.validate(text, expected)
    assert r.passed is (outcome is None), r
    assert r.failure_type is outcome


@pytest.mark.parametrize("text,outcome", [("An API is...", None), ("", EMPTY), ("  \n", EMPTY), (None, EMPTY)])
def test_non_empty(text, outcome):
    check(NonEmptyValidator(), text, None, outcome)


@pytest.mark.parametrize(
    "text,expected,outcome",
    [
        ("BUG", "BUG", None),
        ("  BUG\n", "BUG", None),
        ("Paris.", "Paris", None),
        ('"Paris"', "Paris", None),
        ("**BUG**", "BUG", None),
        ("21/10/2026", "21/10/2026", None),
        ("FEATURE", "BUG", WRONG),
        ("Bug", "BUG", WRONG),
        ("reliability testing", "RELIABILITY TESTING", WRONG),
        ("The answer is BUG", "BUG", WRONG),
        ("", "BUG", EMPTY),
        (None, "BUG", EMPTY),
    ],
)
def test_exact_match(text, expected, outcome):
    check(ExactMatchValidator(), text, expected, outcome)


ANNA = {"name": "Anna", "age": 21}


@pytest.mark.parametrize(
    "text,expected,outcome",
    [
        ('{"name": "Anna", "age": 21}', ANNA, None),
        ('{"age": 21, "name": "Anna"}', ANNA, None),
        ('```json\n{"name": "Anna", "age": 21}\n```', ANNA, None),
        ('{"name": "Anna", "age": 21.0}', ANNA, None),
        ("[8, 14, 20]", [8, 14, 20], None),
        ('{"is_weekend": true}', {"is_weekend": True}, None),
        ('{"name": "Anna", "age": 22}', ANNA, WRONG),
        ('{"name": "Anna"}', ANNA, WRONG),
        ('{"name": "Anna", "age": 21, "x": 1}', ANNA, WRONG),
        ('{"name": "Anna", "age": "21"}', ANNA, WRONG),
        ('{"is_weekend": 1}', {"is_weekend": True}, WRONG),
        ("[20, 14, 8]", [8, 14, 20], WRONG),
        ("Here you go: {\"name\": \"Anna\"}", ANNA, FORMAT),
        ("{'name': 'Anna'}", ANNA, FORMAT),
        ("[1, 2]", ANNA, FORMAT),
        ("", ANNA, EMPTY),
    ],
)
def test_json(text, expected, outcome):
    check(JSONValidator(), text, expected, outcome)


USER = {"name": "string", "age": "integer", "email": "string"}


@pytest.mark.parametrize(
    "text,expected,outcome",
    [
        ('{"name": "Bo", "age": 30, "email": "b@x.io"}', USER, None),
        ('{"name": "Bo", "age": 30, "email": "b@x.io", "id": 7}', USER, None),
        ('```\n{"name": "Bo", "age": 30, "email": "b@x.io"}\n```', USER, None),
        ('{"name": "Bo", "email": "b@x.io"}', USER, FORMAT),
        ('{"name": "Bo", "age": "30", "email": "b@x.io"}', USER, FORMAT),
        ('{"name": "Bo", "age": 30.5, "email": "b@x.io"}', USER, FORMAT),
        ('{"name": "Bo", "age": true, "email": "b@x.io"}', USER, FORMAT),
        ('{"t": 21}', {"t": "number"}, None),
        ('{"t": 21.5}', {"t": "number"}, None),
        ('{"s": 1}', {"s": "boolean"}, FORMAT),
        ('{"l": ["a"], "c": 1}', {"l": "array", "c": "integer"}, None),
        ('{"l": "a", "c": 1}', {"l": "array", "c": "integer"}, FORMAT),
        ("[]", USER, FORMAT),
        ("not json", USER, FORMAT),
        (None, USER, EMPTY),
    ],
)
def test_schema(text, expected, outcome):
    check(SchemaValidator(), text, expected, outcome)


def test_schema_error_names_the_field():
    r = SchemaValidator().validate('{"name": "Bo", "email": "x"}', USER)
    assert "age" in r.message


def test_json_equal_nested():
    assert json_equal({"a": [1, {"b": True}]}, {"a": [1.0, {"b": True}]})
    assert not json_equal({"a": [1, {"b": True}]}, {"a": [1, {"b": 1}]})


def test_dispatch_by_task_validation_type():
    t = Task(id="c1", category="classification", prompt="p", validation="exact_match", expected="BUG")
    assert validate(t, "BUG").passed
    assert validate(t, "FEATURE").failure_type is WRONG


def test_every_shipped_task_has_a_working_validator():
    ds = load_dataset(Path(__file__).parent.parent / "datasets" / "prompts.json")
    for task in ds.tasks:
        assert validate(task, None).failure_type is EMPTY
