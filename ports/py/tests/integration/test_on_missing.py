"""US5: `on_missing` decides what a computed field does without an input; the error counts like any model error."""

import pytest

from aduku.errors import ErrorCode, RejectedError
from aduku.ion_codec import Annotated
from aduku.storage.base import Storage
from tests.service_helpers import ALICE, JOB, delete, make_service, put, schema_text

BASE = (
    '"/name": {type: string, merge: lww}, "/nickname": {type: string, merge: lww},'
    ' "/plan": {type: string, merge: lww, default: "free", on_delete: default},'
    ' "/seat": {type: string, merge: lww, default: "none"}'
)


def svc(backend: Storage, setting: str = "null", extra: str = ""):  # type: ignore[no-untyped-def]
    shout = f'"/shout": {{type: string, on_missing: {setting}, computed: {{macro: upper, args: ["/name"]}}}}'
    fields = BASE + ", " + shout + (", " + extra if extra else "")
    return make_service(backend, schema=schema_text(fields=fields))


def computed(value: object) -> Annotated:
    return Annotated(("computed",), value)


def test_1_the_default_setting_gives_an_explicit_null(backend: Storage) -> None:
    document = svc(backend).get("d1").document
    assert document["shout"] == computed(None)


def test_2_absent_leaves_the_field_out(backend: Storage) -> None:
    result = svc(backend, "absent").get("d1")
    assert "shout" not in result.document and result.model_errors == ()


def test_3_error_leaves_it_out_and_returns_a_model_error(backend: Storage) -> None:
    service = svc(backend, "error")
    service.append("d1", [put("/nickname", "p", tier="backfill")], contributor=JOB, lenient=True)
    result = service.get("d1")
    assert "shout" not in result.document
    (error,) = result.model_errors
    assert (error.rule_id, error.path_scope) == ("computed:/shout", "/shout") and error.message
    # nothing is set, so the document had this error before any request: it belongs to the schema version
    assert (error.introduced_by_request, error.introduced_by_schema_version) == (None, 1)


def test_4_a_missing_computed_dependency_makes_the_dependent_follow_its_own_setting(backend: Storage) -> None:
    extra = '"/loud": {type: string, on_missing: absent, computed: {macro: lower, args: ["/shout"]}}'
    document = svc(backend, "null", extra).get("d1").document
    assert document["shout"] == computed(None) and "loud" not in document


def test_5_a_defaulted_dependency_counts_as_having_its_default(backend: Storage) -> None:
    extra = '"/seatUp": {type: string, computed: {macro: upper, args: ["/seat"]}}'
    assert svc(backend, "null", extra).get("d1").document["seatUp"] == computed("NONE")


def test_6_a_deleted_dependency_is_missing_unless_it_falls_back_to_a_default(backend: Storage) -> None:
    extra = '"/planUp": {type: string, computed: {macro: upper, args: ["/plan"]}}'
    service = svc(backend, "null", extra)
    service.append("d1", [put("/name", "pat"), put("/plan", "pro")], contributor=ALICE)
    service.append("d1", [delete("/name"), delete("/plan")], contributor=ALICE)
    document = service.get("d1").document
    assert document["shout"] == computed(None)  # deleted, no fallback: missing
    assert document["planUp"] == computed("FREE")  # deleted, falls back to its default: has a value


def test_7_a_macro_that_accepts_missing_inputs_runs_whatever_the_setting(backend: Storage) -> None:
    extra = '"/who": {type: string, on_missing: error, computed: {macro: coalesce, args: ["/nickname", "/name"]}}'
    service = svc(backend, "absent", extra)
    assert service.get("d1").document["who"] == computed(None)  # both missing: coalesce's own null, no error
    assert service.get("d1").model_errors == ()
    service.append("d1", [put("/name", "pat")], contributor=ALICE)
    assert service.get("d1").document["who"] == computed("pat")


def test_8_a_request_that_makes_an_input_missing_is_rejected_when_the_setting_is_error(backend: Storage) -> None:
    service = svc(backend, "error")
    service.append("d1", [put("/name", "pat", tier="backfill")], contributor=JOB)
    with pytest.raises(RejectedError) as caught:
        service.append("d1", [delete("/name", tier="backfill")], contributor=JOB)
    assert caught.value.code is ErrorCode.VALIDATION_FAILED  # the document had no errors
    assert (caught.value.reasons[0].rule, caught.value.reasons[0].path) == ("computed:/shout", "/shout")
    assert service.get("d1").document["shout"] == computed("PAT") and service.last_seq("d1") == 1


def test_9_the_code_is_model_error_introduced_when_the_document_already_has_errors(backend: Storage) -> None:
    extra = '"/other": {type: string, on_missing: error, computed: {macro: upper, args: ["/nickname"]}}'
    service = svc(backend, "error", extra)
    service.append("d1", [put("/name", "pat", tier="backfill")], contributor=JOB, lenient=True)  # /other is missing
    with pytest.raises(RejectedError) as caught:
        service.append("d1", [delete("/name", tier="backfill")], contributor=JOB)
    assert caught.value.code is ErrorCode.MODEL_ERROR_INTRODUCED


def test_10_a_permitted_flagged_request_is_accepted_and_the_error_is_attributed_to_it(backend: Storage) -> None:
    service = svc(backend, "error")
    service.append("d1", [put("/name", "pat", tier="backfill")], contributor=JOB)
    result = service.append("d1", [delete("/name", tier="backfill")], contributor=JOB, lenient=True)
    assert [e.rule_id for e in result.model_errors_added] == ["computed:/shout"]
    (error,) = service.get("d1").model_errors
    assert error.introduced_by_request == result.request_id
    fixed = service.append("d1", [put("/name", "bo", tier="backfill")], contributor=JOB)
    assert [e.rule_id for e in fixed.model_errors_resolved] == ["computed:/shout"]
    assert service.get("d1").model_errors == () and service.get("d1").document["shout"] == computed("BO")
