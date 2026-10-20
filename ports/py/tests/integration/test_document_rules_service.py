"""US3: the document rules at admission, as model errors, under the no-new-errors rule, and across a schema version (FR-010 to FR-012)."""

import pytest

from aduku.errors import ErrorCode
from aduku.schema.loader import load_schema
from aduku.storage.base import Storage
from tests.rule_helpers import JOB, USER, build, never_shows, rejection, remove, schema_of, set_

STR = "{type: string, merge: lww}"
FIELDS = (
    " ".join(
        f'"/{name}": {STR},' for name in ("street", "city", "zip", "phone", "email", "personal", "company", "note")
    )
    + ' "/from": {type: int, merge: lww}, "/to": {type: int, merge: lww}'
)
RULES = (
    """'all_or_none'::{paths: ["/street", "/city", "/zip"]}, 'at_least_one'::{paths: ["/phone", "/email"]},"""
    """ 'at_most_one'::{paths: ["/personal", "/company"]}, 'ordered'::{first: "/from", second: "/to"}"""
)
GOOD = [set_("/phone", "555")]  # every document needs a phone or an email


def rules_only(rule: str) -> str:
    return {
        "all_or_none": """'all_or_none'::{paths: ["/street", "/city", "/zip"]}""",
        "at_least_one": """'at_least_one'::{paths: ["/phone", "/email"]}""",
        "at_most_one": """'at_most_one'::{paths: ["/personal", "/company"]}""",
        "ordered": """'ordered'::{first: "/from", second: "/to"}""",
    }[rule]


def test_entries_that_satisfy_a_rule_only_together_are_accepted_as_one_request(backend: Storage) -> None:
    service = build(backend, FIELDS, rules_only("all_or_none"))
    service.append("d1", [set_("/street", "s"), set_("/city", "c"), set_("/zip", "z")], contributor=USER)
    assert service.get("d1").model_errors == ()
    service.append("d1", [remove("/street"), remove("/city"), remove("/zip")], contributor=USER)  # all gone: also fine
    assert service.get("d1").model_errors == ()


@pytest.mark.parametrize(
    "rule, setup, entries, path",
    [
        ("all_or_none", [], [set_("/street", "top-secret-street")], "/street"),
        ("at_least_one", [set_("/phone", "555")], [remove("/phone"), set_("/note", "top-secret-note")], "/phone"),
        ("at_most_one", [], [set_("/personal", "top-secret-a"), set_("/company", "top-secret-b")], "/personal"),
        ("ordered", [], [set_("/from", 9), set_("/to", 1)], "/from"),
    ],
)
def test_a_request_that_breaks_a_rule_is_rejected_naming_the_rule_and_first_path(
    backend: Storage, rule: str, setup: list, entries: list, path: str
) -> None:
    service = build(backend, FIELDS, rules_only(rule))
    if setup:
        service.append("d1", setup, contributor=USER)
    recorded = len(service.list_entries("d1"))
    error = rejection(service, entries)
    assert error.code is ErrorCode.VALIDATION_FAILED and [(r.rule, r.path) for r in error.reasons] == [(rule, path)]
    never_shows(error, "top-secret")
    assert len(service.list_entries("d1")) == recorded


def test_a_permitted_lenient_write_records_the_violation_as_a_model_error(backend: Storage) -> None:
    service = build(backend, FIELDS, RULES)
    # an empty document already lacks a phone or an email, so that error is not new; the lone street is
    assert [(e.rule_id, e.path_scope) for e in service.get("d1").model_errors] == [("at_least_one", "/phone")]
    result = service.append("d1", [set_("/street", "s", "backfill")], contributor=JOB, lenient=True)
    assert [(e.rule_id, e.path_scope) for e in result.model_errors_added] == [("all_or_none", "/street")]
    errors = {e.rule_id: e for e in service.get("d1").model_errors}
    assert errors["all_or_none"].introduced_by_request == result.request_id
    assert errors["at_least_one"].introduced_by_schema_version == 1


def test_the_no_new_errors_rule_applies_to_the_document_rules(backend: Storage) -> None:
    service = build(backend, FIELDS, RULES)
    service.append(
        "d1", [set_("/street", "s", "backfill"), set_("/phone", "1", "backfill")], contributor=JOB, lenient=True
    )
    assert [(e.rule_id, e.path_scope) for e in service.get("d1").model_errors] == [("all_or_none", "/street")]
    service.append("d1", [set_("/note", "x")], contributor=USER)  # adds no new error: accepted although one remains
    error = rejection(service, [set_("/personal", "a"), set_("/company", "b")])  # a different, new error
    assert error.code is ErrorCode.MODEL_ERROR_INTRODUCED
    fixed = service.append("d1", [set_("/city", "c"), set_("/zip", "z")], contributor=USER)  # reduces the set: accepted
    assert [e.rule_id for e in fixed.model_errors_resolved] == ["all_or_none"] and service.get("d1").model_errors == ()
    lenient = service.append(
        "d1", [set_("/personal", "a", "backfill"), set_("/company", "b", "backfill")], contributor=JOB, lenient=True
    )
    assert [(e.rule_id, e.path_scope) for e in lenient.model_errors_added] == [("at_most_one", "/personal")]


def test_a_schema_version_that_adds_a_rule_shows_the_violation_attributed_to_the_version(backend: Storage) -> None:
    service = build(backend, FIELDS, "")
    service.append("d1", [set_("/street", "s"), set_("/phone", "1")], contributor=USER)
    assert service.get("d1").model_errors == ()
    entries_before = service.list_entries("d1")
    text = schema_of(FIELDS, rules_only("all_or_none"), "").replace("version: 1", "version: 2")
    service.schemas.put(load_schema(text, service.behaviors))
    service.schemas.activate(2)
    (error,) = service.get("d1").model_errors
    assert (error.rule_id, error.path_scope, error.introduced_by_schema_version) == ("all_or_none", "/street", 2)
    assert service.list_entries("d1") == entries_before  # the ledger is unchanged
    again = build(backend, FIELDS, "", prefix="z")  # a service that never saw the first version derives the same
    again.schemas.put(load_schema(text, again.behaviors))
    again.schemas.activate(2)
    assert again.get("d1").model_errors == service.get("d1").model_errors and service.rebuild("d1").identical
