"""US7 and SPEC 10: lenient writes keep rule violations as model errors; unflagged writes may never add one."""

import pytest

from aduku.errors import ErrorCode, RejectedError
from aduku.model import Identity
from aduku.storage.base import Storage
from tests.service_helpers import ALICE, JOB, ROOT, make_service, put, schema_text

FIELDS = """
  "/name": {type: string, merge: lww, validate: [max_len::{n: 20}]},
  "/age": {type: int, merge: lww, validate: [range::{min: 0, max: 150}]},
  "/country": {type: string, merge: lww},
  "/spouses": {type: "list<string>", merge: lww},
  "/note": {type: string, merge: lww}
"""
SCHEMA = schema_text(fields=FIELDS).replace("fields:", 'document_validate: ["acme.max_spouses_by_country@1"], fields:')
SPOUSES = ("max_spouses_by_country", "/spouses")
AGE = ("range", "/age")


def svc(backend: Storage):  # type: ignore[no-untyped-def]
    return make_service(backend, schema=SCHEMA)


def errors(service, doc: str = "d1") -> list[tuple[str, str]]:  # type: ignore[no-untyped-def]
    return sorted((e.rule_id, e.path_scope) for e in service.get(doc).model_errors)


def two_spouses(tier: str = "backfill"):  # type: ignore[no-untyped-def]
    return [put("/country", "US", tier=tier), put("/spouses", ("A", "B"), tier=tier)]


def with_e1(backend: Storage):  # type: ignore[no-untyped-def]
    service = svc(backend)
    first = service.append("d1", two_spouses(), contributor=JOB, lenient=True)
    return service, first


# -- the eight scenarios of US7 -----------------------------------------------------------------------------------------


def test_1_a_flagged_request_is_recorded_with_a_model_error(backend: Storage) -> None:
    service, first = with_e1(backend)
    assert [e.seq for e in service.list_entries("d1")] == [1, 2] and errors(service) == [SPOUSES]
    (added,) = first.model_errors_added
    assert (added.rule_id, added.path_scope, added.introduced_by_request) == (*SPOUSES, first.request_id)
    assert added.message and first.model_errors_resolved == ()
    assert first.view is not None and [e.rule_id for e in first.view.model_errors] == ["max_spouses_by_country"]
    assert all(entry.lenient for entry in service.list_entries("d1"))


def test_2_the_same_request_without_the_flag_is_rejected_and_nothing_is_recorded(backend: Storage) -> None:
    service = svc(backend)
    with pytest.raises(RejectedError) as caught:
        service.append("d1", two_spouses(), contributor=JOB)
    assert caught.value.code is ErrorCode.VALIDATION_FAILED
    assert (caught.value.reasons[0].rule, caught.value.reasons[0].path) == SPOUSES
    assert service.list_entries("d1") == [] and errors(service) == []


def test_3_a_permitted_flagged_request_may_add_errors_to_a_document_that_has_some(backend: Storage) -> None:
    service, _ = with_e1(backend)
    result = service.append("d1", [put("/age", -5, tier="backfill")], contributor=JOB, lenient=True)
    assert errors(service) == sorted([AGE, SPOUSES])
    assert [(e.rule_id, e.path_scope) for e in result.model_errors_added] == [AGE]


def test_4_the_flag_is_refused_to_writers_the_schema_does_not_permit_even_for_valid_data(backend: Storage) -> None:
    service = svc(backend)
    with pytest.raises(RejectedError) as caught:
        service.append("d1", [put("/name", "Pat")], contributor=ALICE, lenient=True)
    assert caught.value.code is ErrorCode.LENIENT_NOT_PERMITTED and service.list_entries("d1") == []


def test_5_a_request_that_reduces_the_errors_and_adds_none_is_accepted(backend: Storage) -> None:
    service, first = with_e1(backend)
    result = service.append("d1", [put("/spouses", ("A",))], contributor=ALICE)
    assert errors(service) == [] and [(e.rule_id, e.path_scope) for e in result.model_errors_resolved] == [SPOUSES]
    assert result.model_errors_added == () and first.model_errors_added  # the resolved error is the one the first added


def test_6_a_request_that_leaves_the_errors_unchanged_is_accepted(backend: Storage) -> None:
    service, first = with_e1(backend)
    result = service.append("d1", [put("/note", "unrelated")], contributor=ALICE)
    assert errors(service) == [SPOUSES] and result.model_errors_added == result.model_errors_resolved == ()
    (kept,) = service.get("d1").model_errors
    assert kept.introduced_by_request == first.request_id  # still attributed to the request that introduced it


def test_7_an_unflagged_request_that_would_add_an_error_is_rejected(backend: Storage) -> None:
    service, _ = with_e1(backend)
    before = service.list_entries("d1")
    with pytest.raises(RejectedError) as caught:
        service.append("d1", [put("/age", -5)], contributor=ALICE)
    assert caught.value.code is ErrorCode.MODEL_ERROR_INTRODUCED
    assert (caught.value.reasons[0].rule, caught.value.reasons[0].path) == AGE
    assert service.list_entries("d1") == before and errors(service) == [SPOUSES]


def test_8_a_fix_leaves_the_document_without_errors(backend: Storage) -> None:
    service, _ = with_e1(backend)
    service.append("d1", [put("/country", "FR")], contributor=ALICE)  # the rule only applies in the US
    assert errors(service) == [] and service.get("d1").model_errors == ()


# -- more rules of SPEC 10 ----------------------------------------------------------------------------------------------


def test_fixing_one_error_while_adding_another_is_rejected(backend: Storage) -> None:
    """The net effect of the whole request counts (SPEC 10.3)."""
    service, _ = with_e1(backend)
    with pytest.raises(RejectedError) as caught:
        service.append("d1", [put("/spouses", ("A",)), put("/age", -5)], contributor=ALICE)
    assert caught.value.code is ErrorCode.MODEL_ERROR_INTRODUCED and errors(service) == [SPOUSES]


def test_only_the_result_of_the_whole_request_is_checked(backend: Storage) -> None:
    service = svc(backend)
    result = service.append(
        "d1", [put("/country", "US"), put("/spouses", ("A", "B")), put("/spouses", ("A",))], contributor=ALICE
    )
    assert result.model_errors_added == () and errors(service) == []  # the intermediate state is never evaluated


def test_a_schema_error_is_never_bypassed_by_the_flag(backend: Storage) -> None:
    service = svc(backend)
    for entry, code in (
        (put("/nope", 1, tier="backfill"), ErrorCode.UNKNOWN_PATH),
        (put("/age", "x", tier="backfill"), ErrorCode.TYPE_MISMATCH),
    ):
        with pytest.raises(RejectedError) as caught:
            service.append("d1", [entry], contributor=JOB, lenient=True)
        assert caught.value.code is code
    assert service.list_entries("d1") == []


def test_a_flag_on_a_valid_request_changes_nothing(backend: Storage) -> None:
    service = svc(backend)
    result = service.append("d1", [put("/name", "Pat", tier="backfill")], contributor=JOB, lenient=True)
    assert result.model_errors_added == () and errors(service) == []


def test_a_permitted_contributor_may_use_the_flag_at_any_tier(backend: Storage) -> None:
    service = make_service(
        backend,
        schema=SCHEMA.replace(
            "lenient: {allowed: [backfill]},", "lenient: {allowed: [backfill], contributors: [loader]},"
        ),
    )
    service.append("d1", [put("/age", 999)], contributor=Identity("loader", ("user",)), lenient=True)
    assert errors(service) == [AGE]


def test_the_errors_are_returned_on_every_read_until_fixed(backend: Storage) -> None:
    service, first = with_e1(backend)
    for _ in range(3):
        assert errors(service) == [SPOUSES]
    assert service.get("d1").model_errors == first.view.model_errors  # type: ignore[union-attr]
    service.append("d1", [put("/spouses", ("A",))], contributor=ALICE)
    assert errors(service) == []


# -- attribution comes from the ledger, so a cold service says the same -----------------------------------------------------


def test_a_service_with_nothing_cached_attributes_errors_exactly_as_the_one_that_wrote_them(backend: Storage) -> None:
    service, first = with_e1(backend)
    second = service.append(
        "d1", [put("/note", "x", tier="backfill"), put("/age", -5, tier="backfill")], contributor=JOB, lenient=True
    )
    warm = service.get("d1").model_errors
    with backend.transaction() as tx:
        tx.drop_view("d1")
    cold = svc(backend).get("d1").model_errors
    assert cold == warm
    origin = {(e.rule_id, e.path_scope): e.introduced_by_request for e in cold}
    assert origin == {SPOUSES: first.request_id, AGE: second.request_id}


def test_an_error_that_is_fixed_and_comes_back_is_a_new_error(backend: Storage) -> None:
    service, first = with_e1(backend)
    service.append("d1", [put("/spouses", ("A",), tier="backfill")], contributor=JOB)  # same tier, so it can win back
    again = service.append("d1", [put("/spouses", ("A", "B"), tier="backfill")], contributor=JOB, lenient=True)
    (error,) = service.get("d1").model_errors
    assert error.introduced_by_request == again.request_id != first.request_id


# -- a retry reports what the original did --------------------------------------------------------------------------------


def test_a_retry_reports_the_same_model_error_changes_as_the_original(backend: Storage) -> None:
    service = svc(backend)
    original = service.append("d1", two_spouses(), contributor=JOB, lenient=True, request_key="K")
    service.append("d1", [put("/spouses", ("A",))], contributor=ALICE)  # the error is fixed by now
    retry = service.append("d1", two_spouses(), contributor=JOB, lenient=True, request_key="K")
    assert retry.duplicate and retry.model_errors_added == original.model_errors_added
    assert retry.model_errors_resolved == original.model_errors_resolved == ()
    assert retry.view is not None and retry.view.model_errors == ()  # the view is the latest one


def test_a_retry_of_a_fixing_request_reports_what_it_resolved(backend: Storage) -> None:
    service, _ = with_e1(backend)
    fix = service.append("d1", [put("/spouses", ("A",))], contributor=ALICE, request_key="F")
    retry = service.append("d1", [put("/spouses", ("A",))], contributor=ALICE, request_key="F")
    assert retry.duplicate and retry.model_errors_resolved == fix.model_errors_resolved != ()


def test_other_documents_are_not_affected(backend: Storage) -> None:
    service, _ = with_e1(backend)
    service.append("d2", [put("/name", "ok")], contributor=ALICE)
    assert errors(service, "d2") == [] and errors(service, "d1") == [SPOUSES]
    assert ROOT  # the shared identity import is used by other tests


def test_a_service_that_catches_up_across_a_schema_version_attributes_as_a_cold_replay_does(backend: Storage) -> None:
    from aduku.behaviors.registry import BehaviorRegistry
    from aduku.schema.loader import load_schema
    from aduku.schema.registry import SchemaRegistry
    from aduku.service import Aduku
    from aduku.storage.base import SequentialIds
    from tests.fixtures import register_fixtures

    def at(version: int, top: int) -> str:
        fields = FIELDS.replace("max: 150", f"max: {top}")
        return schema_text(fields=fields, version=version).replace(
            "fields:", 'document_validate: ["acme.max_spouses_by_country@1"], fields:'
        )

    def service(active: int, prefix: str) -> Aduku:
        behaviors = BehaviorRegistry.with_builtins()
        register_fixtures(behaviors)
        registry = SchemaRegistry()
        for version, top in ((1, 150), (2, 100)):
            registry.put(load_schema(at(version, top), behaviors))
        registry.activate(active)
        return Aduku(backend, registry, behaviors=behaviors, ids=SequentialIds(prefix))

    old, new = service(1, "old"), service(2, "new")
    new.append("d1", [put("/name", "x")], contributor=ALICE)  # warm, under version 2
    old.append("d1", [put("/age", 120)], contributor=ALICE)  # valid under version 1, a model error under version 2
    caught_up = new.get("d1").model_errors
    with backend.transaction() as tx:
        tx.drop_view("d1")
    assert service(2, "cold").get("d1").model_errors == caught_up
