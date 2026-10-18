"""US3 scenarios 1 to 3 and SC-007: each field resolves by its declared rule; merge type conflicts never merge."""

from decimal import Decimal
from typing import Any

import pytest

from aduku.behaviors.merge import MergeTypeError, SchemaMergeContext, step, unwrap
from aduku.behaviors.registry import BehaviorRegistry
from aduku.ion_codec import ABSENT, TOMBSTONE, FrozenStruct, parse
from aduku.paths import parse_path
from aduku.schema.loader import load_schema
from tests.service_helpers import ALICE, FIELDS, make_service, put, schema_text

BEHAVIORS = BehaviorRegistry.with_builtins()
SCHEMA = load_schema(schema_text())


def resolve(schema: Any, path: str, values: list[Any]) -> Any:
    """Fold `values` (already in (tier, seq) order) with the rule the schema declares for `path`."""
    context = SchemaMergeContext(schema, BEHAVIORS)
    result: Any = ABSENT
    for value in values:
        result = context.step(parse_path(path), result, value)
    return unwrap(result)


def test_max_keeps_the_larger_value_whatever_the_order() -> None:
    assert resolve(SCHEMA, "/age", [30, 28]) == 30  # backfill 30, then user 28
    assert resolve(SCHEMA, "/age", [28, 30]) == 30


def test_union_collects_every_tier_in_order_without_duplicates() -> None:
    assert resolve(SCHEMA, "/tags", [("x",), ("y",), ("z",)]) == ("x", "y", "z")
    assert resolve(SCHEMA, "/tags", [("x", "y"), ("y", "z"), ("x",)]) == ("x", "y", "z")


def test_lww_keeps_the_last_value() -> None:
    assert resolve(SCHEMA, "/name", ["A. Smith", "Alice", "Alice S."]) == "Alice S."


def test_nested_structs_merge_field_by_field() -> None:
    zip_only = FrozenStruct({"zip": "78701"})
    city_only = FrozenStruct({"city": "Paris"})
    schema = load_schema(
        schema_text(
            fields='"/address": {type: struct, merge: struct}, "/address/city": {type: string, merge: lww},'
            ' "/address/zip": {type: string, merge: lww}, "/address/n": {type: int, merge: max}'
        )
    )
    merged = resolve(schema, "/address", [zip_only, city_only])
    assert merged == FrozenStruct({"zip": "78701", "city": "Paris"})
    assert resolve(schema, "/address", [FrozenStruct({"n": 3}), FrozenStruct({"n": 2, "zip": "1"})]) == FrozenStruct(
        {"n": 3, "zip": "1"}
    )


def test_children_use_their_own_declared_rule() -> None:
    schema = load_schema(
        schema_text(
            fields='"/box": {type: struct, merge: struct}, "/box/hi": {type: int, merge: max}, "/box/last": {type: int, merge: lww}'
        )
    )
    merged = resolve(schema, "/box", [FrozenStruct({"hi": 9, "last": 9}), FrozenStruct({"hi": 4, "last": 4})])
    assert merged == FrozenStruct({"hi": 9, "last": 4})


def test_a_delete_inside_a_merge_wins_and_a_later_write_recreates() -> None:
    assert resolve(SCHEMA, "/age", [30, TOMBSTONE]) is TOMBSTONE
    assert resolve(SCHEMA, "/age", [30, TOMBSTONE, 7]) == 7


def test_values_of_mismatched_types_are_never_merged() -> None:
    rule = BEHAVIORS.get("max", 1)
    assert rule is not None
    ctx = SchemaMergeContext(SCHEMA, BEHAVIORS)
    for old, new in ((5, "x"), ("x", 5), (Decimal("1.5"), "x")):
        with pytest.raises(MergeTypeError):
            step(rule, (), old, new, ctx)
    with pytest.raises(MergeTypeError):
        step(BEHAVIORS.get("union", 1), (), ("a",), "b", ctx)  # type: ignore[arg-type]


def test_max_and_min_order_numbers_strings_and_timestamps() -> None:
    mx, mn = BEHAVIORS.get("max", 1), BEHAVIORS.get("min", 1)
    ctx = SchemaMergeContext(SCHEMA, BEHAVIORS)
    assert step(mx, (), Decimal("1.5"), Decimal("2.25"), ctx) == Decimal("2.25")  # type: ignore[arg-type]
    assert step(mn, (), "b", "a", ctx) == "a"  # type: ignore[arg-type]
    early, late = parse("2020-01-01T00:00:00Z"), parse("2020-01-01T01:00:00+00:00")
    assert step(mx, (), early, late, ctx) == late  # type: ignore[arg-type]
    assert step(mx, (), parse("2020-01-01T01:00:00+01:00"), early, ctx) == early  # type: ignore[arg-type]  # offset is honoured


def test_equal_values_with_different_spellings_pick_one_deterministically() -> None:
    mx = BEHAVIORS.get("max", 1)
    ctx = SchemaMergeContext(SCHEMA, BEHAVIORS)
    a, b = Decimal("1.0"), Decimal("1.00")
    assert step(mx, (), a, b, ctx) == step(mx, (), b, a, ctx)  # type: ignore[arg-type]


def test_a_new_merge_governed_field_needs_no_change_to_writers() -> None:
    """SC-007: version 2 adds /score with `max`; the writers keep sending plain entries."""
    service = make_service()
    service.schemas.put(load_schema(schema_text(version=2, fields=FIELDS + ', "/score": {type: int, merge: max}')))
    service.schemas.activate(2)
    service.append("d1", [put("/score", 5)], contributor=ALICE)
    service.append("d1", [put("/score", 3)], contributor=ALICE)
    values = [e.value for e in service.list_entries("d1") if e.path == "/score"]
    assert resolve(service.schemas.active(), "/score", values) == 5


def test_max_orders_timestamps_beyond_microseconds() -> None:
    from aduku.behaviors.merge import _max, _min
    from aduku.ion_codec import parse

    earlier, later = parse("2026-01-01T00:00:00.5Z"), parse("2026-01-01T00:00:00.50000001Z")
    for old, new in ((earlier, later), (later, earlier)):
        assert _max((), old, new, None) is later  # type: ignore[arg-type]
        assert _min((), old, new, None) is earlier  # type: ignore[arg-type]
