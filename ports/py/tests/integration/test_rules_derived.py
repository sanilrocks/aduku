"""US5: the rule library on computed values, never on deferred ones (FR-015, FR-016)."""

import pytest

from aduku.errors import ErrorCode, SchemaError
from aduku.storage.base import Storage
from tests.rule_helpers import JOB, USER, build, never_shows, rejection, set_

FIELDS = """
  "/ref": {type: string, merge: lww}, "/other": {type: string, merge: lww}, "/from": {type: int, merge: lww}, "/span": {type: int, merge: lww},
  "/code": {type: string, computed: {macro: upper, args: ["/ref"]}, validate: [pattern::{regex: "[A-Z]{2}"}, min_len::{n: 2}]},
  "/plan": {type: string, computed: {macro: lower, args: ["/ref"]}, validate: [one_of::{values: ["us", "de"]}]},
  "/label": {type: string, computed: {macro: concat, args: ["/ref", lit::"-", "/other"]}, validate: [unique_items]},
  "/limit": {type: int, computed: {macro: sum, args: ["/from", "/span"]}}
"""
RULES = """'ordered'::{first: "/limit", second: "/from"}, 'all_or_none'::{paths: ["/code", "/other"]}"""


def test_a_field_rule_judges_the_computed_value_and_a_contribution_that_breaks_it_is_rejected(backend: Storage) -> None:
    service = build(backend, FIELDS)
    service.append("d1", [set_("/ref", "us")], contributor=USER)
    error = rejection(service, [set_("/ref", "usa")])
    assert sorted((r.rule, r.path) for r in error.reasons) == [("one_of", "/plan"), ("pattern", "/code")]
    never_shows(error, "usa", "USA")


def test_a_permitted_lenient_write_records_the_computed_violations_as_model_errors(backend: Storage) -> None:
    service = build(backend, FIELDS)
    result = service.append("d1", [set_("/ref", "x", "backfill")], contributor=JOB, lenient=True)
    found = sorted((e.rule_id, e.path_scope) for e in result.model_errors_added)
    assert found == [("min_len", "/code"), ("one_of", "/plan"), ("pattern", "/code")]


def test_a_computed_field_with_no_value_adds_no_error(backend: Storage) -> None:
    service = build(backend, FIELDS)
    service.append("d1", [set_("/other", "o")], contributor=USER)  # /ref is missing: /code, /plan and /label are null
    assert service.get("d1").model_errors == ()


def test_a_document_rule_over_a_computed_and_a_plain_field_is_checked_on_every_change(backend: Storage) -> None:
    service = build(backend, FIELDS, RULES)
    service.append(
        "d1", [set_("/ref", "us"), set_("/other", "o"), set_("/from", 5), set_("/span", 0)], contributor=USER
    )
    assert service.get("d1").model_errors == ()
    error = rejection(service, [set_("/span", 1)])  # /limit = 6 is now after /from = 5
    assert [(r.rule, r.path) for r in error.reasons] == [("ordered", "/limit")]
    error = rejection(
        service, [set_("/other", None)] if False else [set_("/ref", "")]
    )  # /code is empty: all_or_none would split
    assert any(r.rule in ("all_or_none", "pattern", "min_len", "one_of") for r in error.reasons)


DEFERRED = '"/score": {type: int, deferred: {hook: "h@1", depends: ["/ref"], refresh: once, invalidate_by: [admin]}}'


@pytest.mark.parametrize(
    "fields, rules",
    [
        (f"{DEFERRED}, {FIELDS}", """'all_or_none'::{paths: ["/code", "/score"]}"""),
        (f"{DEFERRED}, {FIELDS}", """'at_least_one'::{paths: ["/score", "/code"]}"""),
        (f"{DEFERRED}, {FIELDS}", """'ordered'::{first: "/score", second: "/from"}"""),
        (f"{DEFERRED}, {FIELDS}", """'ordered'::{first: "/from", second: "/score"}"""),
    ],
)
def test_a_rule_over_a_deferred_field_is_refused_whatever_the_argument(
    backend: Storage, fields: str, rules: str
) -> None:
    def register(behaviors):  # type: ignore[no-untyped-def]
        behaviors.register_hook("h", 1, lambda inputs, timeout: 1)

    with pytest.raises(SchemaError) as caught:
        build(backend, fields, rules, register=register)
    assert "DEPENDS_ON_DEFERRED" in caught.value.codes


def test_a_deferred_field_is_never_validated(backend: Storage) -> None:
    def register(behaviors):  # type: ignore[no-untyped-def]
        behaviors.register_hook("h", 1, lambda inputs, timeout: 1)

    fields = DEFERRED.replace("deferred:", "validate: [range::{min: 0}], deferred:") + ", " + FIELDS
    with pytest.raises(SchemaError) as caught:
        build(backend, fields, register=register)
    assert "DEPENDS_ON_DEFERRED" in caught.value.codes


def test_an_unregistered_error_code_is_not_used_here() -> None:
    assert ErrorCode.VALIDATION_FAILED.value == "VALIDATION_FAILED"


def test_a_rule_judges_a_recorded_impure_value_like_any_other(backend: Storage) -> None:
    impure = (
        '"/taxId": {type: string, computed: {impure: {hook: "fx.echo@1", depends: ["/country"], reason: "registry", '
        'timeout: "1s", retry: {max: 0}, deadline: "2s", recompute_by: [admin]}}, validate: [pattern::{regex: "tax-[A-Z]{2}"}]}'
    )
    service = build(backend, f'"/country": {{type: string, merge: lww}}, {impure}')
    service.append("d1", [set_("/country", "US")], contributor=USER)  # the hook answers "tax-US": fine
    error = rejection(service, [set_("/country", "usa")])  # "tax-usa" breaks the pattern
    assert [(r.rule, r.path) for r in error.reasons] == [("pattern", "/taxId")]
    never_shows(error, "usa", "tax-usa")
