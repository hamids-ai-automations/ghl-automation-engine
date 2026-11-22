"""The schema validator: it must reject, and it must refuse to pretend.

The failure mode worth testing is not "invalid input passes" (annoying) but "a schema
states a constraint the validator ignores" (dangerous, because the tool then accepts
arguments its contract says it rejects). So the unsupported-keyword test is the point of
this file, not a footnote.
"""

from __future__ import annotations

import pytest

from ghl_mcp.schema import describe, validate

SCHEMA = {
    "type": "object",
    "properties": {
        "email": {"type": "string", "minLength": 3, "maxLength": 20},
        "limit": {"type": "integer", "minimum": 1, "maximum": 10},
        "tags": {"type": "array", "items": {"type": "string"}, "maxItems": 2},
        "mode": {"type": "string", "enum": ["create", "update"]},
    },
    "required": ["email"],
    "additionalProperties": False,
}


def test_valid_arguments_pass():
    assert validate({"email": "a@b.com", "limit": 3, "tags": ["x"]}, SCHEMA) == []


def test_missing_required_property_is_reported():
    problems = validate({}, SCHEMA)
    assert any("missing required property 'email'" in problem for problem in problems)


def test_unexpected_property_is_rejected():
    problems = validate({"email": "a@b.com", "nope": 1}, SCHEMA)
    assert any("unexpected property 'nope'" in problem for problem in problems)


def test_wrong_type_reports_the_path():
    problems = validate({"email": "a@b.com", "limit": "3"}, SCHEMA)
    assert problems == ["$.limit: expected integer, got str"]


def test_integer_rejects_booleans():
    problems = validate({"email": "a@b.com", "limit": True}, SCHEMA)
    assert problems and "expected integer" in problems[0]


def test_enum_violation_is_reported():
    problems = validate({"email": "a@b.com", "mode": "delete"}, SCHEMA)
    assert any("must be one of" in problem for problem in problems)


def test_string_length_bounds():
    assert validate({"email": "ab"}, SCHEMA)
    assert validate({"email": "x" * 21}, SCHEMA)


def test_numeric_bounds():
    assert validate({"email": "a@b.com", "limit": 0}, SCHEMA)
    assert validate({"email": "a@b.com", "limit": 11}, SCHEMA)


def test_array_items_are_validated_with_index_path():
    problems = validate({"email": "a@b.com", "tags": ["ok", 5]}, SCHEMA)
    assert problems == ["$.tags[1]: expected string, got int"]


def test_array_max_items():
    problems = validate({"email": "a@b.com", "tags": ["a", "b", "c"]}, SCHEMA)
    assert any("at most 2 item(s)" in problem for problem in problems)


def test_unsupported_keyword_raises_instead_of_passing():
    with pytest.raises(ValueError, match="unsupported keyword"):
        validate({"email": "a@b.com"}, {"type": "object", "patternProperties": {}})


def test_additional_properties_schema_is_enforced():
    schema = {
        "type": "object",
        "additionalProperties": {"type": "string"},
    }
    assert validate({"any": "text"}, schema) == []
    assert validate({"any": 1}, schema)


def test_describe_lists_properties():
    lines = list(describe(SCHEMA))
    assert any(line.startswith("email: string") for line in lines)
