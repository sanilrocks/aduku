"""US6: an impure field's hook is called while a request is handled; its outcome is recorded and never called again."""

import pytest

from aduku.errors import ErrorCode, RejectedError, SchemaError
from aduku.ion_codec import Annotated
from aduku.schema.loader import load_schema
from aduku.storage.base import Storage
from tests.fixtures import CountingHook, hook_of
from tests.service_helpers import ALICE, delete, make_service, put, schema_text

IMPURE = (
    '"/taxId": {type: string%s, computed: {impure: {hook: "%s", depends: ["/country"], reason: "registry",'
    ' timeout: "1s", retry: {max: 1, backoff: "10ms"}, deadline: "3s", recompute_by: [admin]}}}'
)
BASE = (
    '"/country": {type: string, merge: lww}, "/name": {type: string, merge: lww},'
    ' "/age": {type: int, merge: lww, validate: [range::{min: 0, max: 150}]}'
)
LABEL = ', "/taxLabel": {type: string, computed: {macro: upper, args: ["/taxId"]}}'


def fields(validate: str = "", hook: str = "fx.echo@1", label: bool = True) -> str:
    return BASE + ", " + IMPURE % (validate, hook) + (LABEL if label else "")


def svc(backend: Storage, **kw):  # type: ignore[no-untyped-def]
    return make_service(backend, schema=schema_text(fields=fields(**kw)))


def computed(value: object) -> Annotated:
    return Annotated(("computed",), value)


def calls(service) -> int:  # type: ignore[no-untyped-def]
    return hook_of(service.behaviors, "fx.echo").calls


def stored(backend: Storage, doc: str = "d1") -> list:  # type: ignore[type-arg]
    with backend.read() as tx:
        return tx.list_effects(doc)


def test_1_the_hook_is_called_for_the_request_and_its_value_is_in_the_response(backend: Storage) -> None:
    service = svc(backend)
    result = service.append("d1", [put("/country", "US")], contributor=ALICE)
    assert result.view is not None and result.view.document["taxId"] == computed("tax-US")
    assert result.view.document["taxLabel"] == computed("TAX-US")  # a pure field over the impure one
    assert calls(service) == 1
    (record,) = stored(backend)
    assert (record.field, record.hook, record.outcome, record.value, record.attempts) == (
        "/taxId", "fx.echo@1", "value", '"tax-US"', 1,
    )  # fmt: skip


def test_2_reads_rebuilds_cold_services_and_retries_make_no_call(backend: Storage) -> None:
    service = svc(backend)
    service.append("d1", [put("/country", "US")], contributor=ALICE, request_key="K")
    for _ in range(100):  # SC-008: not one call across a hundred reads
        assert service.get("d1").document["taxId"] == computed("tax-US")
    assert service.rebuild("d1").identical
    again = service.append("d1", [put("/country", "US")], contributor=ALICE, request_key="K")
    assert again.duplicate
    cold = svc(backend)  # nothing cached in memory
    assert cold.get("d1").document["taxId"] == computed("tax-US") and cold.rebuild("d1").identical
    assert calls(service) == 1 and calls(cold) == 0 and len(stored(backend)) == 1


def test_3_the_same_inputs_are_not_called_again_later(backend: Storage) -> None:
    service = svc(backend)
    for country in ("US", "CA", "US", "CA"):
        service.append("d1", [put("/country", country)], contributor=ALICE)
    assert calls(service) == 2 and len(stored(backend)) == 2  # one per distinct input
    assert service.get("d1").document["taxId"] == computed("tax-CA")


def test_4_a_request_rejected_for_another_reason_leaves_no_outcome(backend: Storage) -> None:
    service = svc(backend)
    with pytest.raises(RejectedError) as caught:
        service.append("d1", [put("/country", "US"), put("/age", -5)], contributor=ALICE)
    assert caught.value.code is ErrorCode.VALIDATION_FAILED
    assert stored(backend) == [] and service.list_entries("d1") == []
    first = calls(service)
    service.append("d1", [put("/country", "US"), put("/age", 5)], contributor=ALICE)
    assert calls(service) == first + 1 and len(stored(backend)) == 1  # the earlier outcome was not kept or reused


def test_5_a_validator_can_depend_on_the_impure_value(backend: Storage) -> None:
    service = svc(backend, validate=", validate: [max_len::{n: 5}]")
    with pytest.raises(RejectedError) as caught:
        service.append("d1", [put("/country", "US")], contributor=ALICE)  # "tax-US" is six characters
    assert caught.value.code is ErrorCode.VALIDATION_FAILED
    assert caught.value.reasons[0].path == "/taxId" and service.list_entries("d1") == []


def test_6_a_declaration_missing_a_required_key_is_refused_at_load() -> None:
    text = schema_text(
        fields=BASE + ', "/t": {type: string, computed: {impure: {hook: "fx.echo@1", depends: ["/country"]}}}'
    )
    with pytest.raises(SchemaError) as caught:
        load_schema(text)
    assert "IMPURE_DECLARATION_INCOMPLETE" in caught.value.codes


def test_7_an_unregistered_hook_is_refused_at_load() -> None:
    text = schema_text(fields=fields(hook="nobody.home@1"))
    with pytest.raises(SchemaError) as caught:
        load_schema(text)
    assert "UNKNOWN_BEHAVIOR" in caught.value.codes


def test_8_explain_names_the_hook_the_inputs_and_the_record(backend: Storage) -> None:
    service = svc(backend)
    service.append("d1", [put("/country", "US")], contributor=ALICE)
    why = service.explain("d1", "/taxId")
    (record,) = stored(backend)
    assert (why.winner, why.rule, why.value) == ("computed", "fx.echo@1", "tax-US")
    assert why.dependencies == (("/country", "US"),) and why.effect_seq == record.effect_seq


def test_9_the_hook_is_given_only_its_declared_dependencies(backend: Storage) -> None:
    service = svc(backend)
    service.append("d1", [put("/country", "US"), put("/name", "Pat"), put("/age", 3)], contributor=ALICE)
    assert hook_of(service.behaviors, "fx.echo").inputs == [{"/country": "US"}]


def test_10_a_missing_input_means_no_call(backend: Storage) -> None:
    service = svc(backend)
    service.append("d1", [put("/name", "Pat")], contributor=ALICE)
    assert calls(service) == 0 and service.get("d1").document["taxId"] == computed(None)
    service.append("d1", [put("/country", "US")], contributor=ALICE)
    service.append("d1", [delete("/country")], contributor=ALICE)
    assert calls(service) == 1 and service.get("d1").document["taxId"] == computed(None)


def test_11_a_read_that_finds_no_outcome_calls_the_hook_once_and_records_it(backend: Storage) -> None:
    service = make_service(backend, schema=schema_text(fields=BASE))
    service.append("d1", [put("/country", "US")], contributor=ALICE)  # written before the field existed
    service.schemas.put(load_schema(schema_text(version=2, fields=fields()), service.behaviors))
    service.schemas.activate(2)
    result = service.get("d1")
    assert result.document["taxId"] == computed("tax-US") and calls(service) == 1 and len(stored(backend)) == 1
    service.get("d1")
    assert calls(service) == 1


def test_12_a_new_hook_version_in_a_new_schema_version_calls_the_new_hook(backend: Storage) -> None:
    service = svc(backend)
    service.append("d1", [put("/country", "US")], contributor=ALICE)
    service.behaviors.register_hook("fx.echo", 2, CountingHook("fx.echo", lambda i, n: "v2-" + str(i["/country"])))
    service.schemas.put(load_schema(schema_text(version=2, fields=fields(hook="fx.echo@2")), service.behaviors))
    service.schemas.activate(2)
    assert service.get("d1").document["taxId"] == computed("v2-US")
    assert hook_of(service.behaviors, "fx.echo", 2).calls == 1 and calls(service) == 1
    assert sorted(r.hook for r in stored(backend)) == ["fx.echo@1", "fx.echo@2"]
    service.schemas.activate(1)
    assert service.get("d1").document["taxId"] == computed("tax-US") and calls(service) == 1  # the old outcome is back


def test_13_a_lost_effect_log_costs_one_new_call_and_no_other_change(tmp_path) -> None:  # type: ignore[no-untyped-def]
    from aduku.storage.sqlite import SqliteStorage

    path = str(tmp_path / "x.db")
    first = SqliteStorage(path)
    service = svc(first)
    service.append("d1", [put("/country", "US")], contributor=ALICE)
    before = service.get("d1").document
    first.close()
    copy = SqliteStorage(str(tmp_path / "y.db"))  # the ledger without its effect log
    source = SqliteStorage(path)
    with source.read() as tx:
        entries, request = tx.entries("d1"), tx.entries_of_request(tx.entries("d1")[0].request_id)
    from aduku.storage.base import RequestRecord

    with copy.transaction() as tx:
        tx.append_request(RequestRecord(entries[0].request_id, "d1", "alice"), request)
    fresh = svc(copy)
    assert fresh.get("d1").document == before and calls(fresh) == 1
    source.close()
    copy.close()
