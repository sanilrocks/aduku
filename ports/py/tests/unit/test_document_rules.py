"""Feature 004, FR-002, FR-007, FR-008: all_or_none, at_least_one, at_most_one and ordered."""

from decimal import Decimal

import pytest

from aduku.behaviors.registry import BehaviorRegistry
from aduku.behaviors.validate import Subject
from aduku.ion_codec import FrozenStruct, parse

REGISTRY = BehaviorRegistry.with_builtins()


def doc(**fields: object) -> FrozenStruct:
    return FrozenStruct({k: (FrozenStruct(v) if isinstance(v, dict) else v) for k, v in fields.items()})


def check(rule: str, document: FrozenStruct, **args: object) -> list[tuple[str, str]]:
    """The (path scope, message) pairs the rule reports."""
    behavior = REGISTRY.get(rule, 1)
    assert behavior is not None and behavior.applies == "document"
    found = behavior.impl(Subject(document, None, document, FrozenStruct(args)))
    assert all(v.rule_id == rule for v in found)
    return [(v.path_scope, v.message) for v in found]


ABC = {"paths": ("/a", "/b", "/c")}


def test_all_or_none() -> None:
    assert check("all_or_none", doc(), **ABC) == []
    assert check("all_or_none", doc(a=1, b=2, c=3), **ABC) == []
    for partial in (doc(a=1), doc(b=2), doc(a=1, b=2), doc(b=2, c=3)):
        assert check("all_or_none", partial, **ABC) == [("/a", "these fields go together: all or none")]


def test_null_counts_as_no_value() -> None:
    assert check("all_or_none", doc(a=1, b=None, c=None), **ABC) != []
    assert check("all_or_none", doc(a=None, b=None, c=None), **ABC) == []
    assert check("at_least_one", doc(a=None), paths=("/a",)) != []
    assert check("at_most_one", doc(a=1, b=None), paths=("/a", "/b")) == []
    assert check("ordered", doc(a=5, b=None), first="/a", second="/b") == []


def test_at_least_one() -> None:
    both = {"paths": ("/phone", "/email")}
    assert check("at_least_one", doc(), **both) == [("/phone", "at least one of these fields is needed")]
    assert check("at_least_one", doc(phone="555"), **both) == [] and check("at_least_one", doc(email="x"), **both) == []
    assert check("at_least_one", doc(phone="1", email="2"), **both) == []


def test_at_most_one() -> None:
    both = {"paths": ("/personal", "/company")}
    assert check("at_most_one", doc(), **both) == [] and check("at_most_one", doc(personal="x"), **both) == []
    assert check("at_most_one", doc(personal="x", company="y"), **both) == [
        ("/personal", "at most one of these fields may be given")
    ]


def test_the_rules_read_nested_paths() -> None:
    document = doc(address={"city": "Paris"})
    assert check("all_or_none", document, paths=("/address/city", "/address/zip")) != []
    assert check("at_least_one", document, paths=("/address/zip", "/address/city")) == []


@pytest.mark.parametrize(
    "first, second, ok",
    [
        (1, 2, True), (2, 2, True), (3, 2, False), (Decimal("1.5"), 2, True), (2, Decimal("1.5"), False),
        (Decimal("2.0"), 2, True), (1.5, 2, True), ("a", "b", True), ("b", "b", True), ("b", "a", False),
        (1, "1", False), ("1", 1, False), (True, 2, False), (1, True, False),
        (parse("2026-01-01T00:00:00Z"), parse("2026-01-02T00:00:00Z"), True),
        (parse("2026-01-02T00:00:00Z"), parse("2026-01-01T00:00:00Z"), False),
        (parse("2026-01-01T02:00:00+02:00"), parse("2026-01-01T00:00:00Z"), True),  # the same instant
        (parse("2026-01-01T00:00:00Z"), 5, False),
    ],
)  # fmt: skip
def test_ordered(first: object, second: object, ok: bool) -> None:
    found = check("ordered", doc(a=first, b=second), first="/a", second="/b")
    assert (found == []) is ok, (first, second)
    if not ok:
        incomparable = _incomparable(first, second)
        assert found == [
            ("/a", "these fields cannot be compared" if incomparable else "must not be after the other field")
        ]


def test_ordered_has_nothing_to_compare_when_either_path_has_no_value() -> None:
    assert (
        check("ordered", doc(a=5), first="/a", second="/b") == []
        and check("ordered", doc(b=5), first="/a", second="/b") == []
    )
    assert check("ordered", doc(), first="/a", second="/b") == []


def test_no_message_repeats_a_value() -> None:
    messages = [m for _, m in check("ordered", doc(a="top-secret-9", b="a"), first="/a", second="/b")]
    assert messages and all("top-secret" not in m for m in messages)


def test_the_rules_are_registered_as_document_rules_with_path_arguments() -> None:
    shapes = {"all_or_none": [("paths", "paths")], "at_least_one": [("paths", "paths")], "at_most_one": [("paths", "paths")],
              "ordered": [("first", "path"), ("second", "path")]}  # fmt: skip
    for name, expected in shapes.items():
        behavior = REGISTRY.get(name, 1)
        assert behavior is not None and behavior.applies == "document"
        assert [(a.name, a.kind) for a in behavior.args or ()] == expected
    assert REGISTRY.get("all_or_none", 1).args[0].min == 2 and REGISTRY.get("at_least_one", 1).args[0].min == 1  # type: ignore[union-attr]


def _incomparable(a: object, b: object) -> bool:
    """True for a pair the rule cannot order: different kinds, or booleans (they are not numbers)."""
    numbers = (int, Decimal, float)
    kinds = [
        "number"
        if isinstance(x, numbers) and not isinstance(x, bool)
        else "text"
        if isinstance(x, str)
        else "timestamp"
        if hasattr(x, "tzinfo") or type(x).__name__ == "Timestamp"
        else "other"
        for x in (a, b)
    ]
    return kinds[0] != kinds[1] or "other" in kinds
