"""Deterministic response validators.

HTTP success is not task success. Each validator checks, in order:
  usable content  -> EMPTY_RESPONSE
  format          -> FORMAT_ERROR      (not JSON, wrong JSON shape/types)
  correctness     -> VALIDATION_ERROR  (wrong answer)
"""

import json
import re
from dataclasses import dataclass
from typing import Any

from pydantic import StrictBool, StrictFloat, StrictInt, StrictStr, ValidationError, create_model

from tester.models import FailureType, Task


@dataclass(frozen=True)
class ValidationResult:
    passed: bool
    failure_type: FailureType | None = None
    message: str = ""


PASS = ValidationResult(True)


def fail(kind: FailureType, message: str) -> ValidationResult:
    return ValidationResult(False, kind, message)


_FENCE = re.compile(r"^```[A-Za-z]*\s*\n?(.*?)\n?\s*```$", re.S)


def strip_fence(text: str) -> str:
    # Lenient by design: a ```json fence around valid JSON counts as a pass, since nearly
    # every chat model adds one. Raw text is still stored, so strictness can be re-scored later.
    text = text.strip()
    m = _FENCE.match(text)
    return m.group(1).strip() if m else text


def parse_json(text: str) -> tuple[Any, ValidationResult | None]:
    try:
        return json.loads(strip_fence(text)), None
    except json.JSONDecodeError as e:
        return None, fail(FailureType.FORMAT_ERROR, f"not valid JSON: {e}")


def json_equal(a: Any, b: Any) -> bool:
    """Equality with JSON types: unlike Python ==, true != 1 and "1" != 1. 21 == 21.0 is allowed."""
    if isinstance(a, bool) or isinstance(b, bool):
        return type(a) is type(b) and a == b
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return a == b
    if isinstance(a, dict) and isinstance(b, dict):
        return a.keys() == b.keys() and all(json_equal(a[k], b[k]) for k in a)
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(map(json_equal, a, b))
    return type(a) is type(b) and a == b


class NonEmptyValidator:
    def validate(self, text: str | None, expected: Any) -> ValidationResult:
        if text is None or not text.strip():
            return fail(FailureType.EMPTY_RESPONSE, "response has no content")
        return PASS


class ExactMatchValidator:
    """Case-sensitive (some tasks test casing). Ignores surrounding whitespace, quotes,
    markdown emphasis and one trailing period, e.g. '**Paris.**' matches 'Paris'."""

    @staticmethod
    def normalize(text: str) -> str:
        t = text.strip().strip("\"'`*").strip()
        return t[:-1].rstrip() if t.endswith(".") else t

    def validate(self, text: str | None, expected: str) -> ValidationResult:
        if (r := NonEmptyValidator().validate(text, None)) is not PASS:
            return r
        got = self.normalize(text)
        if got != expected:
            return fail(FailureType.VALIDATION_ERROR, f"expected {expected!r}, got {got[:200]!r}")
        return PASS


class JSONValidator:
    """Response must parse as JSON and equal `expected` exactly (JSON types, key set, order of arrays)."""

    def validate(self, text: str | None, expected: Any) -> ValidationResult:
        if (r := NonEmptyValidator().validate(text, None)) is not PASS:
            return r
        data, err = parse_json(text)
        if err:
            return err
        if type(data) is not type(expected):
            return fail(FailureType.FORMAT_ERROR, f"expected a JSON {type(expected).__name__}, got {type(data).__name__}")
        if not json_equal(data, expected):
            return fail(FailureType.VALIDATION_ERROR, f"expected {expected}, got {json.dumps(data)[:200]}")
        return PASS


_SCHEMA_TYPES = {
    "string": StrictStr,
    "integer": StrictInt,
    "number": StrictInt | StrictFloat,
    "boolean": StrictBool,
    "array": list,
    "object": dict,
}


class SchemaValidator:
    """Response must be a JSON object with the given fields and types. Extra fields are allowed."""

    def validate(self, text: str | None, expected: dict[str, str]) -> ValidationResult:
        if (r := NonEmptyValidator().validate(text, None)) is not PASS:
            return r
        data, err = parse_json(text)
        if err:
            return err
        if not isinstance(data, dict):
            return fail(FailureType.FORMAT_ERROR, f"expected a JSON object, got {type(data).__name__}")
        model = create_model("ExpectedShape", **{k: (_SCHEMA_TYPES[t], ...) for k, t in expected.items()})
        try:
            model.model_validate(data)
        except ValidationError as e:
            problems = "; ".join(f"{'.'.join(map(str, x['loc']))}: {x['msg']}" for x in e.errors())
            return fail(FailureType.FORMAT_ERROR, f"schema mismatch: {problems}")
        return PASS


VALIDATORS = {
    "non_empty": NonEmptyValidator(),
    "exact_match": ExactMatchValidator(),
    "json": JSONValidator(),
    "schema": SchemaValidator(),
}


def validate(task: Task, text: str | None) -> ValidationResult:
    return VALIDATORS[task.validation].validate(text, task.expected)
