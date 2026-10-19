"""Feature 003, contracts/schema-format.md: the declaration of a deferred field and every load-time check (FR-021)."""

import pytest

from aduku.behaviors.registry import BehaviorRegistry
from aduku.errors import SchemaError
from aduku.paths import parse_path
from aduku.schema.loader import load_schema

BASE = {"hook": '"fx.score@1"', "depends": '["/name"]', "refresh": "once", "invalidate_by": "[admin]"}


def declaration(type_: str | None = "int", **options: str | None) -> str:
    """A deferred field; an option set to None is left out, a new one is added."""
    merged = {**BASE, **options}
    on_missing = merged.pop("on_missing", None)  # declared on the field, as for computed fields
    body = ", ".join(f"{key}: {value}" for key, value in merged.items() if value is not None)
    head = (f"type: {type_}, " if type_ else "") + (f"on_missing: {on_missing}, " if on_missing else "")
    return f"{{{head}deferred: {{{body}}}}}"


def wrap(fields: str, top: str = "") -> str:
    return (
        f'schema::{{id: "t", version: 1, tiers: [backfill, user, admin], roles: [ops], {top} fields: '
        f'{{"/name": {{type: string, merge: lww}}, "/country": {{type: string, merge: lww}}, '
        f'"/shout": {{type: string, computed: {{macro: upper, args: ["/name"]}}}}, {fields}}}}}'
    )


def behaviors() -> BehaviorRegistry:
    registry = BehaviorRegistry.with_builtins()
    registry.register_hook("fx.score", 1, lambda inputs, timeout: 1)
    registry.register_hook("fx.rate", 2, lambda inputs, timeout: 1)
    registry.register_macro("acme.shout", 1, ["a"], "a + a")
    return registry


def schema_of(fields: str, top: str = ""):  # type: ignore[no-untyped-def]
    return load_schema(wrap(fields, top), behaviors())


def spec_of(field_text: str, top: str = ""):  # type: ignore[no-untyped-def]
    spec = schema_of(f'"/score": {field_text}', top).field_for(parse_path("/score"))
    assert spec is not None and spec.deferred is not None
    return spec


def problems_of(fields: str, top: str = "") -> tuple[str, ...]:
    with pytest.raises(SchemaError) as caught:
        load_schema(wrap(fields, top), behaviors())
    return caught.value.problems


def codes_of(fields: str, top: str = "") -> set[str]:
    return {p.split(":")[0] for p in problems_of(fields, top)}


# -- what a full declaration reads as ------------------------------------------------------------------------------


def test_a_minimal_declaration_has_the_platform_defaults() -> None:
    spec = spec_of(declaration())
    deferred = spec.deferred
    assert spec.derived == "deferred" and (deferred.hook, deferred.version) == ("fx.score", 1)
    assert deferred.depends == ("/name",) and deferred.invalidate_by == ("admin",)
    assert [p.kind for p in deferred.refresh] == ["once"] and deferred.ttl is None
    assert (deferred.timeout, deferred.retry_max, deferred.backoff, deferred.deadline) == (2.0, 2, 0.2, 6.0)
    assert (deferred.on_dependency_change, deferred.on_failure, spec.on_missing) == ("keep", None, "null")


def test_every_option_is_read() -> None:
    text = declaration(
        refresh="per_fetch",
        ttl='"5m"',
        on_failure="serve_stale",
        on_missing="absent",
        timeout='"3s"',
        retry='{max: 4, backoff: "1s"}',
        deadline='"20s"',
        invalidate_by="[admin, ops]",
        depends='["/name", "/country", "/shout"]',
    )
    spec = spec_of(text)
    deferred = spec.deferred
    assert deferred.ttl == 300.0 and deferred.on_failure == "serve_stale" and spec.on_missing == "absent"
    assert (deferred.timeout, deferred.retry_max, deferred.backoff, deferred.deadline) == (3.0, 4, 1.0, 20.0)
    assert deferred.invalidate_by == ("admin", "ops") and deferred.depends == ("/name", "/country", "/shout")


def test_one_policy_or_a_list_and_each_kind_reads_its_options() -> None:
    refresh = '[once, scheduled::{every: "1h"}, scheduled::{cron: "0 * * * *"}, notification::{topic: "t.updated"}]'
    policies = spec_of(declaration(refresh=refresh)).deferred.refresh
    assert [p.kind for p in policies] == ["once", "scheduled", "scheduled", "notification"]
    assert (policies[1].every, policies[2].cron, policies[3].topic) == (3600.0, "0 * * * *", "t.updated")
    assert [p.kind for p in spec_of(declaration(refresh="per_fetch", ttl='"1d"')).deferred.refresh] == ["per_fetch"]
    assert spec_of(declaration(refresh="per_fetch", ttl='"1d"')).deferred.ttl == 86400.0


def test_on_dependency_change_belongs_to_once() -> None:
    assert spec_of(declaration(on_dependency_change="recompute")).deferred.on_dependency_change == "recompute"
    assert spec_of(declaration(refresh="[once, per_fetch]", ttl='"1m"', on_dependency_change="keep")).deferred


def test_the_schema_level_failure_default_is_read_and_defaults_to_error() -> None:
    assert schema_of(f'"/score": {declaration()}').deferred_on_failure == "error"
    assert (
        schema_of(f'"/score": {declaration()}', "deferred_on_failure: serve_stale,").deferred_on_failure
        == "serve_stale"
    )
    assert "deferred_on_failure" in " ".join(problems_of(f'"/score": {declaration()}', "deferred_on_failure: maybe,"))


def test_a_deferred_field_may_depend_on_plain_computed_impure_and_deferred_fields() -> None:
    impure = (
        '{type: string, computed: {impure: {hook: "fx.rate@2", depends: ["/country"], reason: "r", timeout: "1s", '
        'retry: {max: 0}, deadline: "2s", recompute_by: [admin]}}}'
    )
    fields = (
        f'"/tax": {impure}, "/a": {declaration(depends=chr(91) + chr(34) + "/tax" + chr(34) + chr(93))}, '
        f'"/b": {declaration(depends=chr(91) + chr(34) + "/a" + chr(34) + ", " + chr(34) + "/shout" + chr(34) + chr(93))}'
    )
    schema_of(fields)


# -- every refusal, with its stable code ------------------------------------------------------------------------


@pytest.mark.parametrize(
    "field_text",
    [
        declaration(hook=None),
        declaration(depends=None),
        declaration(refresh=None),
        declaration(invalidate_by=None),
        declaration(depends="[]"),
        declaration(refresh="[]"),
        declaration(invalidate_by="[]"),
        declaration(type_=None),
    ],
)
def test_a_declaration_missing_a_required_part_is_incomplete(field_text: str) -> None:
    assert "DEFERRED_DECLARATION_INCOMPLETE" in codes_of(f'"/score": {field_text}')


@pytest.mark.parametrize(
    "refresh",
    [
        "sometimes",
        "[once, sometimes]",
        "scheduled",
        "scheduled::{}",
        'scheduled::{every: "1h", cron: "0 * * * *"}',
        'scheduled::{every: "500ms"}',
        'scheduled::{cron: "61 * * * *"}',
        'scheduled::{cron: "* * * *"}',
        "notification",
        "notification::{}",
        'notification::{topic: ""}',
        "once::{x: 1}",
        "[once, once]",
    ],
)
def test_a_bad_refresh_policy_is_refused(refresh: str) -> None:
    assert "BAD_REFRESH_POLICY" in codes_of(f'"/score": {declaration(refresh=refresh)}')


@pytest.mark.parametrize(
    "options",
    [
        {"ttl": '"5m"'},  # ttl on once
        {"refresh": "per_fetch"},  # per_fetch needs a ttl
        {"refresh": "per_fetch", "ttl": '"500ms"'},
        {
            "refresh": "per_fetch",
            "ttl": '"soon"',
        },
        {"refresh": "per_fetch", "ttl": '"1m"', "on_dependency_change": "recompute"},  # needs once
        {"on_dependency_change": "sometimes"},
        {"on_failure": "ignore"},
        {"on_missing": "error"},  # a deferred field never produces a model error
        {"on_missing": "ignore"},
        {"timeout": '"31s"'},
        {"timeout": '"0s"'},
        {"timeout": '"soon"'},
        {"retry": "{max: 6}"},
        {"retry": '{max: 1, backoff: "11s"}'},
        {"retry": '{backoff: "1s"}'},
        {"deadline": '"61s"'},
        {"deadline": '"1s"', "timeout": '"2s"'},  # shorter than one attempt
        {"colour": "red"},
    ],
)
def test_a_bad_option_is_refused(options: dict[str, str]) -> None:
    assert "BAD_DEFERRED_OPTION" in codes_of(f'"/score": {declaration(**options)}')


def test_invalidate_by_must_name_tiers_or_roles_the_schema_knows() -> None:
    assert "UNKNOWN_TIER" in codes_of(f'"/score": {declaration(invalidate_by="[admin, wizard]")}')
    schema_of(f'"/score": {declaration(invalidate_by="[user, ops]")}')


def test_the_source_must_be_a_registered_hook() -> None:
    assert "UNKNOWN_BEHAVIOR" in codes_of(f'"/score": {declaration(hook=chr(34) + "fx.nobody@1" + chr(34))}')
    assert "UNKNOWN_BEHAVIOR" in codes_of(
        f'"/score": {declaration(hook=chr(34) + "acme.shout@1" + chr(34))}'
    )  # a macro
    assert "UNKNOWN_BEHAVIOR" in codes_of(
        f'"/score": {declaration(hook=chr(34) + "fx.score@2" + chr(34))}'
    )  # a version


def test_a_dependency_must_be_a_declared_path() -> None:
    assert "UNKNOWN_DEPENDENCY" in codes_of(
        f'"/score": {declaration(depends=chr(91) + chr(34) + "/nope" + chr(34) + chr(93))}'
    )
    assert "UNKNOWN_DEPENDENCY" in codes_of(
        f'"/score": {declaration(depends=chr(91) + chr(34) + "name" + chr(34) + chr(93))}'
    )


def test_deferred_fields_may_not_depend_on_each_other_in_a_loop() -> None:
    def deps(path: str) -> str:
        return f'["{path}"]'

    assert "DEPENDENCY_CYCLE" in codes_of(f'"/a": {declaration(depends=deps("/a"))}')
    both = f'"/a": {declaration(depends=deps("/b"))}, "/b": {declaration(depends=deps("/a"))}'
    assert "DEPENDENCY_CYCLE" in codes_of(both)


def test_a_deferred_field_has_no_merge_default_validator_or_computed_part() -> None:
    assert "DERIVED_HAS_MERGE_OR_DEFAULT" in codes_of(
        f'"/score": {declaration().replace("deferred", "merge: lww, deferred")}'
    )
    assert "DERIVED_HAS_MERGE_OR_DEFAULT" in codes_of(
        f'"/score": {declaration().replace("deferred", "default: 1, deferred")}'
    )
    assert "DEPENDS_ON_DEFERRED" in codes_of(
        f'"/score": {declaration().replace("deferred", "validate: [range::{min: 0}], deferred")}'
    )
    both = declaration().replace("deferred", 'computed: {macro: upper, args: ["/name"]}, deferred')
    assert "COMPUTED_AND_DEFERRED" in codes_of(f'"/score": {both}')


def test_everything_wrong_is_reported_together() -> None:
    found = codes_of(
        f'"/a": {declaration(hook=None)}, "/b": {declaration(refresh="sometimes")}, "/c": {declaration(ttl=chr(34) + "1m" + chr(34))}'
    )
    assert {"DEFERRED_DECLARATION_INCOMPLETE", "BAD_REFRESH_POLICY", "BAD_DEFERRED_OPTION"} <= found


def test_the_declaration_is_part_of_the_rules_fingerprint() -> None:
    first = schema_of(f'"/score": {declaration()}').rules_fingerprint
    assert (
        first
        != schema_of(f'"/score": {declaration(refresh="per_fetch", ttl=chr(34) + "1m" + chr(34))}').rules_fingerprint
    )
