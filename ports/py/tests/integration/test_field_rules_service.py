"""US1: the new field rules at admission, as model errors under the lenient flag, and on computed values (FR-010, FR-011, FR-016)."""

import pytest

from aduku.errors import ErrorCode
from aduku.storage.base import Storage
from tests.rule_helpers import ADMIN, JOB, USER, build, never_shows, rejection, remove, set_

FIELDS = """
  "/country": {type: string, merge: lww, validate: [pattern::{regex: "[A-Z]{2}"}]},
  "/plan": {type: string, merge: lww, validate: [one_of::{values: ["free", "pro", "team"]}]},
  "/name": {type: string, merge: lww, validate: [min_len::{n: 2}]},
  "/tags": {type: "list<string>", merge: lww, validate: [unique_items]},
  "/age": {type: int, merge: max, validate: [min_len::{n: 5}, pattern::{regex: "x"}, unique_items]},
  "/ref": {type: string, merge: lww},
  "/code": {type: string, computed: {macro: upper, args: ["/ref"]}, validate: [pattern::{regex: "[A-Z]{2}"}]}
"""

GOOD = [("/country", "US"), ("/plan", "pro"), ("/name", "Al"), ("/tags", ("a", "b")), ("/tags", ())]
BAD = [
    ("/country", "usa", "pattern"),
    ("/plan", "gold", "one_of"),
    ("/name", "§", "min_len"),
    ("/tags", ("a", "a"), "unique_items"),
]


@pytest.mark.parametrize("path, value", GOOD)
def test_a_good_value_is_accepted(backend: Storage, path: str, value: object) -> None:
    service = build(backend, FIELDS)
    service.append("d1", [set_(path, value)], contributor=USER)
    assert service.get("d1").model_errors == ()


@pytest.mark.parametrize("path, value, rule", BAD)
def test_a_bad_value_is_rejected_with_the_field_and_the_rule_and_without_the_value(
    backend: Storage, path: str, value: object, rule: str
) -> None:
    service = build(backend, FIELDS)
    error = rejection(service, [set_(path, value)])
    assert error.code is ErrorCode.VALIDATION_FAILED and [(r.rule, r.path) for r in error.reasons] == [(rule, path)]
    never_shows(error, value if isinstance(value, str) else "zzz-never")
    assert service.list_entries("d1") == []


def test_two_bad_values_are_listed_together_and_nothing_is_recorded(backend: Storage) -> None:
    service = build(backend, FIELDS)
    error = rejection(service, [set_("/country", "usa"), set_("/plan", "gold")])
    assert sorted((r.rule, r.path) for r in error.reasons) == [("one_of", "/plan"), ("pattern", "/country")]
    assert service.list_entries("d1") == []


def test_a_request_is_judged_on_the_document_it_makes_not_entry_by_entry(backend: Storage) -> None:
    service = build(backend, FIELDS)
    service.append("d1", [set_("/country", "usa"), set_("/country", "US")], contributor=USER)  # the later entry wins
    assert service.get("d1").model_errors == ()


def test_a_lenient_write_by_a_permitted_tier_is_recorded_with_one_model_error_per_violation(backend: Storage) -> None:
    service = build(backend, FIELDS)
    entries = [
        set_("/country", "usa", "backfill"),
        set_("/plan", "gold", "backfill"),
        set_("/tags", ("a", "a"), "backfill"),
    ]
    result = service.append("d1", entries, contributor=JOB, lenient=True)
    assert sorted((e.rule_id, e.path_scope) for e in result.model_errors_added) == [
        ("one_of", "/plan"), ("pattern", "/country"), ("unique_items", "/tags"),
    ]  # fmt: skip
    assert all("usa" not in e.message and "gold" not in e.message for e in service.get("d1").model_errors)
    assert all(entry.lenient for entry in service.list_entries("d1"))


def test_the_flag_from_a_tier_that_may_not_use_it_is_refused_even_for_a_valid_value(backend: Storage) -> None:
    service = build(backend, FIELDS)
    error = rejection(service, [set_("/country", "US")], lenient=True)
    assert error.code is ErrorCode.LENIENT_NOT_PERMITTED


def test_a_rule_does_not_judge_a_kind_of_value_it_does_not_apply_to(backend: Storage) -> None:
    service = build(backend, FIELDS)
    service.append("d1", [set_("/age", 3)], contributor=USER)  # min_len, pattern and unique_items on a number
    assert service.get("d1").model_errors == ()


def test_a_rule_on_a_computed_field_judges_the_computed_value(backend: Storage) -> None:
    service = build(backend, FIELDS)
    service.append("d1", [set_("/ref", "us")], contributor=USER)  # code = "US": fine
    error = rejection(service, [set_("/ref", "usa")])  # code = "USA" breaks the pattern on /code
    assert [(r.rule, r.path) for r in error.reasons] == [("pattern", "/code")]
    never_shows(error, "usa", "USA")


def test_a_lenient_write_records_a_violation_of_a_computed_value(backend: Storage) -> None:
    service = build(backend, FIELDS)
    result = service.append("d1", [set_("/ref", "usa", "backfill")], contributor=JOB, lenient=True)
    assert [(e.rule_id, e.path_scope) for e in result.model_errors_added] == [("pattern", "/code")]


def test_a_computed_field_without_a_value_adds_no_error(backend: Storage) -> None:
    service = build(backend, FIELDS)
    service.append("d1", [set_("/plan", "free")], contributor=USER)  # no country: /code is null, nothing to check
    assert service.get("d1").model_errors == ()
    service.append("d1", [set_("/country", "DE"), remove("/country")], contributor=ADMIN)
    assert service.get("d1").model_errors == ()
