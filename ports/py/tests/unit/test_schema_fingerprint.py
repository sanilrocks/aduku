"""Feature 006 (R-2): the full fingerprint of a version and the per-field parts that name what differs."""

from aduku.behaviors.registry import BehaviorRegistry
from aduku.schema.loader import load_schema
from aduku.schema.model import differing_parts

BEHAVIORS = BehaviorRegistry.with_builtins()


def schema(fields: str, version: int = 1, extra: str = ""):  # type: ignore[no-untyped-def]
    return load_schema(
        f'schema::{{id: "t", version: {version}, tiers: [backfill, user, admin], {extra} fields: {{{fields}}}}}',
        BEHAVIORS,
    )


A = '"/name": {type: string, merge: lww}, "/age": {type: int, merge: max}'


def test_equal_content_has_the_same_fingerprint_and_parts() -> None:
    one, two = schema(A), schema(A)
    assert one.content_fingerprint == two.content_fingerprint and one.fingerprint_parts == two.fingerprint_parts
    assert len(one.content_fingerprint) == 64


def test_any_change_changes_the_fingerprint() -> None:
    base = schema(A).content_fingerprint
    assert schema(A.replace("max", "min")).content_fingerprint != base
    assert schema(A + ', "/email": {type: string, merge: lww}').content_fingerprint != base
    assert schema(A, extra="roles: [dpo],").content_fingerprint != base
    assert schema(A, version=2).content_fingerprint != base  # the number is part of what a version is


def test_parts_hold_a_digest_per_field_and_one_for_the_top_level() -> None:
    parts = schema(A).fingerprint_parts
    assert set(parts["fields"]) == {"/name", "/age"} and parts["top"]


def test_a_changed_field_changes_only_its_own_part() -> None:
    old, new = (
        schema(A).fingerprint_parts,
        schema(A.replace('"/age": {type: int, merge: max}', '"/age": {type: int, merge: min}')).fingerprint_parts,
    )
    assert old["fields"]["/name"] == new["fields"]["/name"] and old["fields"]["/age"] != new["fields"]["/age"]
    assert old["top"] == new["top"]
    assert differing_parts(old, new) == ["/age"]


def test_a_top_level_change_names_the_top_level_and_an_added_or_removed_field_is_named() -> None:
    old = schema(A).fingerprint_parts
    assert differing_parts(old, schema(A, extra="roles: [dpo],").fingerprint_parts) == ["(top-level settings)"]
    assert differing_parts(old, schema(A + ', "/email": {type: string, merge: lww}').fingerprint_parts) == ["/email"]
    assert differing_parts(schema(A + ', "/email": {type: string, merge: lww}').fingerprint_parts, old) == ["/email"]


def test_at_most_the_limit_are_named_in_order() -> None:
    many = ", ".join(f'"/f{i}": {{type: int, merge: max}}' for i in range(9))
    other = many.replace("max", "min")
    assert differing_parts(schema(many).fingerprint_parts, schema(other).fingerprint_parts, limit=5) == [
        f"/f{i}" for i in range(5)
    ]
