"""Feature 005 (FR-030, contracts/schema-format.md): `purge_by` names who may purge; the default is nobody."""

import pytest

from aduku.behaviors.registry import BehaviorRegistry
from aduku.errors import SchemaError
from aduku.schema.loader import load_schema

BEHAVIORS = BehaviorRegistry.with_builtins()


def schema(extra: str = "", roles: str = "roles: [dpo],") -> str:
    return (
        f'schema::{{id: "t", version: 1, tiers: [backfill, user, admin], {roles} {extra}'
        ' fields: {"/name": {type: string, merge: lww}}}'
    )


def test_purge_by_lists_tiers_and_roles() -> None:
    loaded = load_schema(schema("purge_by: [admin, dpo],"), BEHAVIORS)
    assert loaded.purge_by == ("admin", "dpo")


def test_absent_or_empty_means_nobody() -> None:
    assert load_schema(schema(), BEHAVIORS).purge_by == ()
    assert load_schema(schema("purge_by: [],"), BEHAVIORS).purge_by == ()


def test_a_name_that_is_neither_a_tier_nor_a_role_is_refused() -> None:
    with pytest.raises(SchemaError) as caught:
        load_schema(schema("purge_by: [admin, ghost],"), BEHAVIORS)
    assert "UNKNOWN_TIER" in caught.value.codes and "ghost" in str(caught.value) and "purge_by" in str(caught.value)


def test_it_must_be_a_list_of_names() -> None:
    with pytest.raises(SchemaError):
        load_schema(schema("purge_by: admin,"), BEHAVIORS)


def test_it_is_part_of_the_rules_fingerprint_but_not_of_the_fold_fingerprint() -> None:
    plain, listed = load_schema(schema(), BEHAVIORS), load_schema(schema("purge_by: [admin],"), BEHAVIORS)
    assert plain.rules_fingerprint != listed.rules_fingerprint
    assert plain.fold_fingerprint == listed.fold_fingerprint
