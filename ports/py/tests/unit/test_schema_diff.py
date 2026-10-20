"""Feature 006 (contracts/changes.md): the class of each difference between two versions; pure and deterministic."""

from __future__ import annotations

import pytest

from aduku.behaviors.registry import BehaviorRegistry
from aduku.model import ChangeList
from aduku.schema.diff import schema_changes
from aduku.schema.loader import load_schema
from aduku.schema.model import Schema
from tests.fixtures import register_fixtures, register_sources

BASE = '"/name": {type: string, merge: lww}, "/age": {type: int, merge: max}'
SECRET = "SECRET-VALUE-42"
IMPURE = (
    '"/taxId": {type: string, computed: {impure: {hook: "fx.echo@1", depends: ["/name"], reason: "registry", '
    'timeout: "1s", retry: {max: %d, backoff: "1ms"}, deadline: "3s", recompute_by: [admin]}}}'
)
DEFERRED = '"/score": {type: int, deferred: {hook: "fx.score@1", depends: ["/name"], refresh: once, invalidate_by: %s}}'


def make(
    fields: str = BASE, tiers: str = "backfill, user, admin", top: str = "", version: int = 1, ident: str = "t"
) -> Schema:
    behaviors = BehaviorRegistry.with_builtins()
    register_fixtures(behaviors)
    register_sources(behaviors, ["fx.score@1"])
    text = f'schema::{{id: "{ident}", version: {version}, tiers: [{tiers}], {top} fields: {{{fields}}}}}'
    return load_schema(text, behaviors)


def rows(changes: ChangeList) -> list[tuple[str, str, str]]:
    return [(c.kind, c.path, c.change_class) for c in changes.changes]


def age(extra: str = "", type_: str = "int", merge: str = "max") -> str:
    return f'"/name": {{type: string, merge: lww}}, "/age": {{type: {type_}, merge: {merge}{extra}}}'


NICK_PLAIN = ', "/nick": {type: string, merge: lww}'
NICK_COMPUTED = ', "/nick": {type: string, computed: {macro: upper, args: ["/name"]}}'

TABLE = [
    ("field_added", {"fields": BASE}, {"fields": BASE + NICK_PLAIN}, [("field_added", "/nick", "compatible")]),
    (
        "field_added_computed",
        {"fields": BASE},
        {"fields": BASE + NICK_COMPUTED},
        [("field_added", "/nick", "compatible")],
    ),
    ("field_removed", {"fields": BASE + NICK_PLAIN}, {"fields": BASE}, [("field_removed", "/nick", "visible")]),
    ("default_added", {"fields": age()}, {"fields": age(", default: 0")}, [("default_changed", "/age", "visible")]),
    (
        "default_changed",
        {"fields": age(", default: 0")},
        {"fields": age(", default: 1")},
        [("default_changed", "/age", "visible")],
    ),
    (
        "on_delete_changed",
        {"fields": age(", default: 0")},
        {"fields": age(", default: 0, on_delete: default")},
        [("default_changed", "/age", "visible")],
    ),
    ("merge_changed", {"fields": age()}, {"fields": age(merge="min")}, [("merge_changed", "/age", "breaking")]),
    ("type_changed", {"fields": age()}, {"fields": age(type_="decimal")}, [("type_changed", "/age", "breaking")]),
    (
        "validate_added",
        {"fields": age()},
        {"fields": age(", validate: [range::{min: 0, max: 5}]")},
        [("validate_changed", "/age", "visible")],
    ),
    (
        "validate_args_changed",
        {"fields": age(", validate: [range::{min: 0, max: 5}]")},
        {"fields": age(", validate: [range::{min: 0, max: 9}]")},
        [("validate_changed", "/age", "visible")],
    ),
    (
        "validate_removed",
        {"fields": age(", validate: [range::{min: 0, max: 5}]")},
        {"fields": age()},
        [("validate_changed", "/age", "visible")],
    ),
    (
        "authority_changed",
        {"fields": age()},
        {"fields": age(", authority: [admin]")},
        [("authority_changed", "/age", "visible")],
    ),
    ("ops_changed", {"fields": age()}, {"fields": age(", ops: [set]")}, [("authority_changed", "/age", "visible")]),
    (
        "became_derived",
        {"fields": BASE + NICK_PLAIN},
        {"fields": BASE + NICK_COMPUTED},
        [("became_derived", "/nick", "breaking")],
    ),
    (
        "became_plain",
        {"fields": BASE + NICK_COMPUTED},
        {"fields": BASE + NICK_PLAIN},
        [("became_plain", "/nick", "breaking")],
    ),
    (
        "computed_changed",
        {"fields": BASE + NICK_COMPUTED},
        {"fields": BASE + NICK_COMPUTED.replace("type: string,", "type: string, on_missing: absent,")},
        [("computed_changed", "/nick", "visible")],
    ),
    (
        "computed_args_changed",
        {"fields": BASE + NICK_COMPUTED},
        {"fields": BASE + NICK_COMPUTED.replace('"/name"', '"/age"')},
        [("computed_changed", "/nick", "visible")],
    ),
    (
        "impure_changed",
        {"fields": BASE + ", " + IMPURE % 1},
        {"fields": BASE + ", " + IMPURE % 2},
        [("impure_changed", "/taxId", "visible")],
    ),
    (
        "deferred_changed",
        {"fields": BASE + ", " + DEFERRED % "[admin]"},
        {"fields": BASE + ", " + DEFERRED % "[admin, user]"},
        [("deferred_changed", "/score", "visible")],
    ),
    ("tier_appended_on_top", {}, {"tiers": "backfill, user, admin, root"}, [("tier_added_top", "root", "compatible")]),
    ("tier_inserted_below_top", {}, {"tiers": "backfill, extra, user, admin"}, [("tier_changed", "extra", "breaking")]),
    ("tier_dropped", {}, {"tiers": "backfill, user"}, [("tier_changed", "admin", "breaking")]),
    (
        "tier_reordered",
        {},
        {"tiers": "backfill, admin, user"},
        [("tier_changed", "admin", "breaking"), ("tier_changed", "user", "breaking")],
    ),
    (
        "tier_renamed",
        {},
        {"tiers": "backfill, user, root"},
        [("tier_changed", "admin", "breaking"), ("tier_added_top", "root", "compatible")],
    ),
    ("role_added", {}, {"top": "roles: [dpo],"}, [("roles_added", "dpo", "compatible")]),
    ("role_removed", {"top": "roles: [dpo],"}, {}, [("roles_removed", "dpo", "visible")]),
    (
        "lenient_changed",
        {"top": "lenient: {allowed: [backfill]},"},
        {"top": "lenient: {allowed: [backfill, user]},"},
        [("settings_changed", "", "visible")],
    ),
    (
        "purge_by_changed",
        {"top": "roles: [dpo],"},
        {"top": "roles: [dpo], purge_by: [dpo],"},
        [("settings_changed", "", "visible")],
    ),
    (
        "failure_modes_changed",
        {},
        {"top": "impure_failure_mode: model_error_on_failure,"},
        [("settings_changed", "", "visible")],
    ),
    (
        "audit_changed",
        {},
        {"top": "rejection_audit: {enabled: true, mode: full, retention: {max_entries: 10}},"},
        [("audit_changed", "", "compatible")],
    ),
    (
        "document_rule_added",
        {},
        {"top": 'document_validate: [at_least_one::{paths: ["/name", "/age"]}],'},
        [("document_validate_changed", "", "visible")],
    ),
]


@pytest.mark.parametrize(("name", "old", "new", "expected"), TABLE, ids=[row[0] for row in TABLE])
def test_each_row_of_the_table(name: str, old: dict, new: dict, expected: list[tuple[str, str, str]]) -> None:  # type: ignore[type-arg]
    changes = schema_changes(make(**old), make(version=2, **new))
    assert rows(changes) == expected, name
    assert all(c.classified for c in changes.changes)


def test_identical_versions_give_an_empty_compatible_list() -> None:
    changes = schema_changes(make(), make(version=2))
    assert (changes.changes, changes.overall, changes.old_version, changes.new_version) == ((), "compatible", 1, 2)


def test_several_changes_at_once_each_have_their_own_entry_and_the_overall_is_the_worst() -> None:
    old = make(BASE + NICK_PLAIN)
    new = make(age(", default: 0", merge="min") + ', "/other": {type: string, merge: lww}', version=2)
    changes = schema_changes(old, new)
    assert rows(changes) == [
        ("default_changed", "/age", "visible"),
        ("merge_changed", "/age", "breaking"),
        ("field_removed", "/nick", "visible"),
        ("field_added", "/other", "compatible"),
    ]
    assert changes.overall == "breaking"


def test_the_overall_class_is_visible_without_a_breaking_change_and_compatible_with_only_compatible_ones() -> None:
    visible = schema_changes(make(), make(age(", default: 0"), version=2))
    compatible = schema_changes(make(), make(BASE + NICK_PLAIN, version=2))
    assert (visible.overall, compatible.overall) == ("visible", "compatible")


def test_a_difference_the_table_does_not_name_is_listed_visible_and_marked_not_classified() -> None:
    changes = schema_changes(make(), make(version=2, ident="u"))
    assert [(c.kind, c.change_class, c.classified) for c in changes.changes] == [("other", "visible", False)]


def test_a_renamed_field_is_a_removal_and_an_addition() -> None:
    old = make(BASE + NICK_PLAIN)
    new = make(BASE + ', "/alias": {type: string, merge: lww}', version=2)
    assert rows(schema_changes(old, new)) == [
        ("field_added", "/alias", "compatible"),
        ("field_removed", "/nick", "visible"),
    ]


def test_the_same_two_versions_give_the_same_list_in_the_same_order_whatever_the_declaration_order() -> None:
    a = make(BASE + NICK_PLAIN + ', "/z": {type: int, merge: max}')
    b = make(BASE + ', "/z": {type: string, merge: lww}' + ', "/y": {type: int, merge: max}', version=2)
    reordered = make('"/y": {type: int, merge: max}, "/z": {type: string, merge: lww}, ' + BASE, version=2)
    first = schema_changes(a, b)
    assert first == schema_changes(a, b) and first.changes == schema_changes(a, reordered).changes


def test_changes_that_can_shadow_entries_name_the_reasons_they_can_give() -> None:
    old = make(BASE + NICK_PLAIN)
    derived = schema_changes(old, make(BASE + NICK_COMPUTED, version=2)).changes[0]
    typed = schema_changes(make(), make(age(type_="decimal"), version=2)).changes[0]
    merged = schema_changes(make(), make(age(merge="min"), version=2)).changes[0]
    dropped = schema_changes(make(), make(tiers="backfill, user", version=2)).changes[0]
    removed = schema_changes(old, make(BASE, version=2)).changes[0]
    assert derived.detail == ("now_derived",) and typed.detail == ("no_longer_conforms",)
    assert merged.detail == ("cannot_be_merged",) and dropped.detail == ("tier_not_in_schema",)
    assert removed.detail == ()  # a removed field shadows nothing


def test_no_description_holds_a_stored_value() -> None:
    old = make(age(f', default: "{SECRET}"').replace("type: int", "type: string").replace("merge: max", "merge: lww"))
    new = make(
        age(', default: "other-secret"').replace("type: int", "type: string").replace("merge: max", "merge: lww"),
        version=2,
    )
    text = " ".join(c.description + c.path + " ".join(c.detail) for c in schema_changes(old, new).changes)
    assert SECRET not in text and "other-secret" not in text and text
