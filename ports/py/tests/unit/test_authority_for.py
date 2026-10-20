"""Feature 004, FR-017: the effective authority of a path is the nearest list at its own pattern or a parent's."""

from aduku.behaviors.registry import BehaviorRegistry
from aduku.paths import parse_path
from aduku.schema.loader import load_schema

FIELDS = """
  "/plan": {type: string, merge: lww, authority: [admin]},
  "/free": {type: string, merge: lww},
  "/billing": {type: struct, merge: struct, authority: [finance]},
  "/billing/note": {type: string, merge: lww},
  "/billing/limit": {type: int, merge: max, authority: [admin]},
  "/billing/tax": {type: struct, merge: struct},
  "/billing/tax/rate": {type: int, merge: max},
  "/w": {type: struct, merge: struct},
  "/w/*": {type: string, merge: lww, authority: [ops]},
  "/w/special": {type: string, merge: lww}
"""
SCHEMA = f'schema::{{id: "t", version: 1, tiers: [user, admin], roles: [finance, ops], fields: {{{FIELDS}}}}}'


def authority(path: str):  # type: ignore[no-untyped-def]
    return load_schema(SCHEMA, BehaviorRegistry.with_builtins()).authority_for(parse_path(path))


def test_a_field_with_its_own_list_uses_it_and_says_where_it_was_declared() -> None:
    assert authority("/plan") == (("admin",), "/plan")


def test_a_field_with_no_list_anywhere_is_open_to_any_permitted_tier() -> None:
    assert authority("/free") is None and authority("/w") is None


def test_a_child_without_a_list_takes_its_parents() -> None:
    assert authority("/billing/note") == (("finance",), "/billing")


def test_a_child_with_its_own_list_replaces_the_parents_it_does_not_add_to_it() -> None:
    assert authority("/billing/limit") == (("admin",), "/billing/limit")


def test_the_nearest_list_above_wins_through_several_levels() -> None:
    assert authority("/billing/tax") == (("finance",), "/billing")
    assert authority("/billing/tax/rate") == (("finance",), "/billing")


def test_a_path_the_schema_does_not_declare_takes_the_list_of_the_nearest_declared_parent() -> None:
    assert authority("/billing/undeclared/deeper") == (("finance",), "/billing")


def test_a_wildcard_pattern_with_a_list_applies_to_the_paths_it_matches() -> None:
    assert authority("/w/anything") == (("ops",), "/w/*")


def test_an_exact_declaration_without_a_list_does_not_borrow_a_sibling_wildcards() -> None:
    assert authority("/w/special") is None  # /w/* is not above /w/special: it is the pattern for its siblings


def test_the_effective_authority_of_a_declared_field_agrees_with_the_lookup_for_a_path_it_matches() -> None:
    """`schema check` and the delete rule read it per field, the write check per path: they must say the same."""
    text = (
        'schema::{id: "t", version: 1, tiers: [user, admin], roles: [finance, ops], fields: {'
        '"/m": {type: struct, merge: struct, authority: [finance]}, "/m/*": {type: struct, merge: struct},'
        '"/m/*/x": {type: int, merge: max}, "/m/*/y": {type: int, merge: max, authority: [admin]},'
        '"/k": {type: struct, merge: struct}, "/k/*": {type: string, merge: lww, authority: [ops]}, "/z": {type: int, merge: max}}}'
    )
    schema = load_schema(text, BehaviorRegistry.with_builtins())
    concrete = {
        "/m": "/m",
        "/m/*": "/m/a",
        "/m/*/x": "/m/a/x",
        "/m/*/y": "/m/a/y",
        "/k": "/k",
        "/k/*": "/k/a",
        "/z": "/z",
    }
    for spec in schema.fields:
        assert schema.authority_of(spec) == schema.authority_for(parse_path(concrete[spec.pattern])), spec.pattern
