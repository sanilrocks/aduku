"""Feature 004, FR-003 to FR-006, FR-008: min_len, pattern, one_of and unique_items."""

from decimal import Decimal

import pytest

from aduku.behaviors.registry import BehaviorRegistry
from aduku.behaviors.validate import Subject
from aduku.ion_codec import FrozenStruct

REGISTRY = BehaviorRegistry.with_builtins()


def check(rule: str, value: object, **args: object) -> list[str]:
    """The messages the rule reports for `value`."""
    behavior = REGISTRY.get(rule, 1)
    assert behavior is not None and behavior.applies == "field"
    found = behavior.impl(Subject(FrozenStruct(), "/f", value, FrozenStruct(args)))
    assert all(v.rule_id == rule and v.path_scope == "/f" for v in found)
    return [v.message for v in found]


def test_min_len_accepts_a_long_enough_text_or_list_and_reports_a_shorter_one() -> None:
    assert check("min_len", "ab", n=2) == [] and check("min_len", ("a", "b"), n=2) == []
    assert check("min_len", "a", n=2) == ["must have at least 2 items or characters"]
    assert check("min_len", (), n=1) and check("min_len", "", n=1) and check("min_len", "", n=0) == []


def test_min_len_ignores_values_that_have_no_length() -> None:
    for value in (5, Decimal("1.5"), True, None, FrozenStruct({"a": 1})):
        assert check("min_len", value, n=3) == []


PATTERN = "[A-Z]{2}"


def test_pattern_accepts_a_match_of_the_whole_value_and_reports_anything_else() -> None:
    assert check("pattern", "US", regex=PATTERN) == []
    for bad in ("us", "USA", "U", "", "U1", " US"):
        assert check("pattern", bad, regex=PATTERN) == ["must match the pattern"], bad


def test_pattern_ignores_values_that_are_not_text() -> None:
    for value in (5, ("US",), None, True, FrozenStruct()):
        assert check("pattern", value, regex=PATTERN) == []


def test_one_of_accepts_each_listed_value_and_reports_the_others() -> None:
    values = ("free", "pro", 3, None, True)
    for good in ("free", "pro", 3, None, True):
        assert check("one_of", good, values=values) == [], good
    for bad in ("team", "Free", 4, False, "3", Decimal("3.5")):
        assert check("one_of", bad, values=values) == ["must be one of the allowed values"], bad


def test_one_of_compares_by_canonical_form() -> None:
    assert check("one_of", Decimal("1.0"), values=(1,)) != []  # 1 and 1.0 are different values
    assert check("one_of", 1, values=(1,)) == []
    assert check("one_of", FrozenStruct({"a": 1}), values=("x",)) == ["must be one of the allowed values"]


def test_unique_items_accepts_distinct_items_and_an_empty_list_and_reports_a_repeat() -> None:
    assert (
        check("unique_items", ()) == []
        and check("unique_items", ("a",)) == []
        and check("unique_items", ("a", "b")) == []
    )
    assert check("unique_items", ("a", "b", "a")) == ["must not repeat an item"]


def test_unique_items_compares_by_canonical_form() -> None:
    assert check("unique_items", (1, Decimal("1.0"))) == []  # different values
    assert check("unique_items", (1, 1)) != []
    same = FrozenStruct({"a": 1, "b": 2}), FrozenStruct({"b": 2, "a": 1})
    assert check("unique_items", same) != []
    assert check("unique_items", (("a",), ("a",))) != []
    assert check("unique_items", (None, None)) != []


def test_unique_items_ignores_values_that_are_not_lists() -> None:
    for value in ("aa", 5, None, FrozenStruct({"a": 1})):
        assert check("unique_items", value) == []


@pytest.mark.parametrize(
    "rule, value, args, secret",
    [
        ("min_len", "secret", {"n": 10}, "secret"),
        ("pattern", "top-secret", {"regex": "[0-9]+"}, "top-secret"),
        ("one_of", "top-secret", {"values": ("a",)}, "top-secret"),
        ("unique_items", ("top-secret", "top-secret"), {}, "top-secret"),
    ],
)
def test_no_message_repeats_the_value(rule: str, value: object, args: dict[str, object], secret: str) -> None:
    messages = check(rule, value, **args)
    assert messages and all(secret not in m for m in messages)


def test_the_rules_are_registered_as_field_rules_with_declared_arguments() -> None:
    for name, expected in (("min_len", ["n"]), ("pattern", ["regex"]), ("one_of", ["values"]), ("unique_items", [])):
        behavior = REGISTRY.get(name, 1)
        assert (
            behavior is not None and behavior.applies == "field" and [a.name for a in behavior.args or ()] == expected
        )


def test_an_explicit_null_is_no_value_so_field_rules_are_not_run_for_it() -> None:
    """Null counts as no value (clarification 3) for field rules too: the engine skips it, and a rule given one reports on the
    value it is handed (here one_of, which would otherwise call a null a value that is not allowed)."""
    from aduku.behaviors.registry import BehaviorRegistry as Registry  # noqa: F401
    from aduku.engine.validate import validate_document
    from aduku.schema.loader import load_schema

    text = 'schema::{id: "t", version: 1, tiers: [user], fields: {"/x": {type: any, merge: lww, validate: [one_of::{values: ["a"]}]}}}'
    schema = load_schema(text, REGISTRY)
    assert validate_document(schema, REGISTRY, FrozenStruct({"x": None})) == []
    assert [v.rule_id for v in validate_document(schema, REGISTRY, FrozenStruct({"x": "b"}))] == ["one_of"]
