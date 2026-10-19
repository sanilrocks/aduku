"""Fail-closed schema loading (SPEC 5.1, FR-010, FR-011, FR-021): every problem is reported, with a stable code."""

import pytest

from aduku.behaviors.registry import BehaviorRegistry
from aduku.errors import SchemaError
from aduku.schema.loader import load_schema
from tests.fixtures import register_fixtures

BEHAVIORS = BehaviorRegistry.with_builtins()
register_fixtures(BEHAVIORS)


def wrap(fields: str, top: str = "") -> str:
    return f'schema::{{id: "t", version: 1, tiers: [backfill, user, admin], {top} fields: {{{fields}}}}}'


def codes_of(text: str) -> set[str]:
    with pytest.raises(SchemaError) as caught:
        load_schema(text, BEHAVIORS)
    return set(caught.value.codes)


def test_valid_schemas_load() -> None:
    load_schema(wrap('"/a": {type: int, merge: max}, "/b": {type: "list<string>", merge: union}'), BEHAVIORS)


def test_unknown_merge_behavior() -> None:
    assert "UNKNOWN_BEHAVIOR" in codes_of(wrap('"/a": {type: int, merge: telepathy}'))
    assert "UNKNOWN_BEHAVIOR" in codes_of(wrap('"/a": {type: int, merge: "lww@9"}'))  # known name, unknown version


def test_a_merge_rule_must_declare_associativity() -> None:
    """Scenario 4, FR-011."""
    assert "MERGE_NOT_ASSOCIATIVE" in codes_of(wrap('"/a": {type: int, merge: custom::"acme.subtract@1"}'))
    load_schema(wrap('"/a": {type: string, merge: custom::"acme.longest@1"}'), BEHAVIORS)  # declares it: accepted


def test_a_behavior_of_the_wrong_kind_is_refused() -> None:
    assert "UNKNOWN_BEHAVIOR" in codes_of(wrap('"/a": {type: int, merge: "acme.max_spouses_by_country@1"}'))


@pytest.mark.parametrize(
    "fields",
    [
        '"/a": {type: bool, merge: max}',
        '"/a": {merge: max}',
        '"/a": {type: string, merge: union}',
        '"/a": {type: int, merge: struct}',
        '"/a": {type: struct, merge: min}',
    ],
)
def test_a_rule_needs_a_type_it_can_handle(fields: str) -> None:
    """FR-010: max and min are only for ordered types; union for lists; struct for structs."""
    assert "MERGE_TYPE_UNSUITABLE" in codes_of(wrap(fields))


def test_ordered_and_list_types_are_accepted() -> None:
    for fields in (
        '"/a": {type: int, merge: max}',
        '"/a": {type: decimal, merge: min}',
        '"/a": {type: string, merge: max}',
        '"/a": {type: timestamp, merge: min}',
        '"/a": {type: "list<int>", merge: union}',
        '"/a": {type: list, merge: union}',
        '"/a": {type: struct, merge: struct}',
        '"/a": {merge: lww}',
    ):
        load_schema(wrap(fields), BEHAVIORS)


DEFERRED = '"/score": {type: int, deferred: {hook: "credit.score@1", depends: ["/name"], refresh: once}}, "/name": {type: string, merge: lww}'


def test_nothing_may_depend_on_a_deferred_field() -> None:
    """FR-021 and SPEC 9.1: validators, computed fields and defaults never read deferred values."""
    cases = [
        DEFERRED + ', "/label": {type: string, computed: {macro: concat, args: ["/score"]}}',
        DEFERRED + ', "/label": {type: string, computed: {macro: custom::"x@1", depends: ["/score"]}}',
        DEFERRED + ', "/age": {type: int, merge: max, validate: [check::{depends: ["/score"]}]}',
        DEFERRED.replace(
            "deferred: {", "validate: [range::{min: 0}], deferred: {"
        ),  # a deferred field is never validated
        DEFERRED + ', "/d": {type: int, merge: lww, default: {macro: copy, args: ["/score"]}}',
    ]
    for fields in cases:
        assert "DEPENDS_ON_DEFERRED" in codes_of(wrap(fields)), fields
    assert "DEPENDS_ON_DEFERRED" in codes_of(wrap(DEFERRED, top='document_validate: [rule::{depends: ["/score"]}],'))
    assert "DEPENDS_ON_DEFERRED" in codes_of(
        wrap(DEFERRED + ', "/sub/x": {type: int}', top='document_validate: [rule::{depends: ["/score/part"]}],')
    )


def test_a_deferred_field_may_depend_on_plain_computed_and_deferred_fields() -> None:
    load_schema(
        wrap(
            '"/name": {type: string, merge: lww},'
            ' "/label": {type: string, computed: {macro: upper, args: ["/name"]}},'
            ' "/a": {type: int, deferred: {hook: "h@1", depends: ["/name", "/label"]}},'
            ' "/b": {type: int, deferred: {hook: "h@1", depends: ["/a"]}}'
        ),
        BEHAVIORS,
    )


def test_derived_fields_must_not_form_a_cycle() -> None:
    cycle = '"/a": {type: int, computed: {macro: copy, args: ["/b"]}}, "/b": {type: int, computed: {macro: copy, args: ["/a"]}}'
    assert "DEPENDENCY_CYCLE" in codes_of(wrap(cycle))
    assert "DEPENDENCY_CYCLE" in codes_of(wrap('"/a": {type: int, computed: {macro: copy, args: ["/a"]}}'))
    deferred = '"/a": {type: int, deferred: {hook: "h@1", depends: ["/b"]}}, "/b": {type: int, deferred: {hook: "h@1", depends: ["/a"]}}'
    assert "DEPENDENCY_CYCLE" in codes_of(wrap(deferred))


def test_a_default_must_conform_to_the_field_type() -> None:
    assert "DEFAULT_TYPE_MISMATCH" in codes_of(wrap('"/a": {type: int, merge: lww, default: "x"}'))
    load_schema(wrap('"/a": {type: int, merge: lww, default: 3}'), BEHAVIORS)


def test_on_delete_default_needs_a_default() -> None:
    assert "ON_DELETE_NEEDS_DEFAULT" in codes_of(wrap('"/a": {type: int, merge: lww, on_delete: default}'))
    load_schema(wrap('"/a": {type: int, merge: lww, default: 1, on_delete: default}'), BEHAVIORS)


def test_tiers_named_by_the_schema_must_exist() -> None:
    assert "UNKNOWN_TIER" in codes_of(wrap('"/a": {type: int, merge: lww}', top="lenient: {allowed: [ghost]},"))
    assert "UNKNOWN_TIER" in codes_of(wrap('"/a": {type: int, merge: lww, authority: [ghost]}'))
    load_schema(wrap('"/a": {type: int, merge: lww, authority: [admin]}'), BEHAVIORS)
    load_schema(
        wrap('"/a": {type: int, merge: lww, authority: [ops]}', top="roles: [ops],"), BEHAVIORS
    )  # a declared role


def test_duplicate_tiers() -> None:
    text = 'schema::{id: "t", version: 1, tiers: [a, b, a], fields: {}}'
    assert "DUPLICATE_TIER" in codes_of(text)


def test_a_derived_field_carries_neither_merge_nor_default_and_is_one_kind_only() -> None:
    assert "DERIVED_HAS_MERGE_OR_DEFAULT" in codes_of(
        wrap('"/a": {type: int, merge: lww, computed: {macro: upper, args: []}}')
    )
    assert "DERIVED_HAS_MERGE_OR_DEFAULT" in codes_of(
        wrap('"/a": {type: int, default: 1, deferred: {hook: "h@1", depends: []}}')
    )
    assert "COMPUTED_AND_DEFERRED" in codes_of(
        wrap('"/a": {type: int, computed: {macro: upper, args: []}, deferred: {hook: "h@1", depends: []}}')
    )


def test_auditing_without_retention_is_refused() -> None:
    assert "AUDIT_RETENTION_REQUIRED" in codes_of(
        wrap('"/a": {type: int, merge: lww}', top="rejection_audit: {enabled: true},")
    )


def test_every_problem_is_reported_together() -> None:
    text = wrap(
        '"/a": {type: bool, merge: max}, "/b": {type: int, merge: telepathy},'
        ' "/c": {type: int, merge: lww, default: "x", on_delete: default, authority: [ghost]}',
        top="rejection_audit: {enabled: true},",
    )
    assert codes_of(text) >= {
        "MERGE_TYPE_UNSUITABLE",
        "UNKNOWN_BEHAVIOR",
        "DEFAULT_TYPE_MISMATCH",
        "UNKNOWN_TIER",
        "AUDIT_RETENTION_REQUIRED",
    }
    with pytest.raises(SchemaError) as caught:
        load_schema(text, BEHAVIORS)
    assert len(caught.value.problems) >= 5
    assert all(problem.split(":")[0].isupper() for problem in caught.value.problems if ":" in problem)


def test_the_default_registry_has_the_built_in_rules_only() -> None:
    load_schema(wrap('"/a": {type: int, merge: max}'))  # no registry argument: built-ins
    with pytest.raises(SchemaError):
        load_schema(wrap('"/a": {type: string, merge: custom::"acme.longest@1"}'))  # fixtures are not built in


def test_extension_operations_are_not_supported_yet() -> None:
    """SPEC 4.3: increment, append and reset arrive in v1.1; a field that could only use them would never be writable."""
    assert "OP_NOT_SUPPORTED" in codes_of(wrap('"/a": {type: int, merge: lww, ops: [increment]}'))
    assert "OP_NOT_SUPPORTED" in codes_of(wrap('"/a": {type: int, merge: lww, ops: [set, append]}'))
    assert "UNKNOWN_OPERATION" in codes_of(wrap('"/a": {type: int, merge: lww, ops: [frob]}'))
    load_schema(wrap('"/a": {type: int, merge: lww, ops: [set, delete]}'), BEHAVIORS)
    load_schema(wrap('"/a": {type: int, merge: lww, ops: [set]}'), BEHAVIORS)


def test_a_retention_age_must_be_positive() -> None:
    for age in ("0s", "0d", "00h"):
        top = f'rejection_audit: {{enabled: true, retention: {{max_age: "{age}"}}}},'
        assert "RETENTION_NOT_POSITIVE" in codes_of(wrap('"/a": {type: int, merge: lww}', top=top)), age
    load_schema(
        wrap('"/a": {type: int, merge: lww}', top='rejection_audit: {enabled: true, retention: {max_age: "1s"}},')
    )


def test_a_default_needs_an_exact_path() -> None:
    """A default on a wildcard pattern would have no path to fill in."""
    for pattern in ("/*", "/a/*", "/**"):
        assert "DEFAULT_ON_PATTERN" in codes_of(wrap(f'"{pattern}": {{merge: lww, default: 1}}')), pattern
    load_schema(wrap('"/a": {type: int, merge: lww, default: 1}'), BEHAVIORS)


def test_validators_must_be_registered_validators() -> None:
    assert "UNKNOWN_BEHAVIOR" in codes_of(wrap('"/a": {type: int, merge: lww, validate: [telepathy]}'))
    assert "UNKNOWN_BEHAVIOR" in codes_of(
        wrap('"/a": {type: int, merge: lww, validate: [lww]}')
    )  # a merge rule is no validator
    assert "UNKNOWN_BEHAVIOR" in codes_of(wrap('"/a": {type: int, merge: lww}', top='document_validate: ["nope@1"],'))
    load_schema(
        wrap('"/a": {type: int, merge: lww, validate: [range::{min: 0}, non_empty, max_len::{n: 3}]}'), BEHAVIORS
    )
    load_schema(
        wrap('"/a": {type: int, merge: lww}', top='document_validate: ["acme.max_spouses_by_country@1"],'), BEHAVIORS
    )


@pytest.mark.parametrize(
    "rule",
    [
        'range::{min: "x"}',
        "range::{max: true}",
        "range::{min: 5, max: 1}",
        "range::{limit: 5}",
        "max_len::{n: -1}",
        'max_len::{n: "3"}',
        "max_len",  # n is required
    ],
)
def test_validator_arguments_are_checked_at_load(rule: str) -> None:
    assert "BAD_BEHAVIOR_ARGS" in codes_of(wrap(f'"/a": {{type: int, merge: max, validate: [{rule}]}}'))


def test_valid_validator_arguments_load() -> None:
    load_schema(
        wrap('"/a": {type: int, merge: max, validate: [range::{min: 0}, range::{max: 1.5}, non_empty]}'), BEHAVIORS
    )
    load_schema(wrap('"/b": {type: string, merge: lww, validate: [max_len::{n: 0}]}'), BEHAVIORS)


# -- feature 002, US4: impossible dependencies are refused when the schema loads --------------------------------------

NAME = '"/name": {type: string, merge: lww}'
IMPURE_ON = (
    '{type: string, computed: {impure: {hook: "fx.echo@1", depends: ["%s"], reason: "r", timeout: "1s",'
    ' retry: {max: 1, backoff: "10ms"}, deadline: "3s", recompute_by: [admin]}}}'
)


@pytest.mark.parametrize(
    "fields",
    [
        NAME + ', "/x": {type: string, computed: {macro: upper, args: ["/nope"]}}',
        NAME + ', "/x": {type: string, computed: {macro: concat, args: ["/name", "/a/b/c"]}}',
        NAME + ', "/x": ' + IMPURE_ON % "/nope",
    ],
)
def test_a_dependency_on_a_path_no_field_declares_is_refused(fields: str) -> None:
    found = codes_of(wrap(fields))
    assert "UNKNOWN_DEPENDENCY" in found


def test_a_dependency_the_schema_does_declare_is_accepted() -> None:
    declared = (
        NAME
        + ', "/address": {type: struct, merge: struct}, "/address/city": {type: string, merge: lww}'
        + ', "/a": {type: string, computed: {macro: upper, args: ["/address/city"]}}'
        + ', "/b": {type: string, computed: {macro: upper, args: ["/a"]}}'
        + ', "/open/*": {merge: lww}, "/c": {type: string, computed: {macro: upper, args: ["/open/anything"]}}'
    )
    load_schema(wrap(declared), BEHAVIORS)


@pytest.mark.parametrize("size", [1, 2, 3, 5])
def test_a_cycle_of_any_length_is_refused_and_the_loop_is_listed(size: int) -> None:
    names = [f"/c{n}" for n in range(size)]
    fields = [
        f'"{path}": {{type: string, computed: {{macro: upper, args: ["{names[(n + 1) % size]}"]}}}}'
        for n, path in enumerate(names)
    ]
    with pytest.raises(SchemaError) as caught:
        load_schema(wrap(", ".join(fields)), BEHAVIORS)
    loops = [p for p in caught.value.problems if p.startswith("DEPENDENCY_CYCLE")]
    assert loops and all(path in loops[0] for path in names)


def test_computed_problems_are_reported_together_with_the_others() -> None:
    bad = (
        '"/a": {type: string, computed: {macro: upper, args: ["/nope"]}},'
        ' "/b": {type: string, merge: lww, computed: {macro: upper, args: ["/name"]}}, ' + NAME
    )
    found = codes_of(wrap(bad))
    assert {"UNKNOWN_DEPENDENCY", "DERIVED_HAS_MERGE_OR_DEFAULT"} <= found
