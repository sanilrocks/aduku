"""US7: what an impure failure does (reject or record), how it reads, and how it clears (FR-023, FR-024)."""

import pytest

from aduku.behaviors.registry import BehaviorRegistry
from aduku.errors import ErrorCode, RejectedError
from aduku.ion_codec import Annotated
from aduku.schema.loader import load_schema
from aduku.schema.registry import SchemaRegistry
from aduku.service import Aduku
from aduku.storage.base import SequentialIds, Storage
from tests.fixtures import CountingHook, register_fixtures
from tests.service_helpers import ALICE, JOB, make_service, put, schema_text

BASE = (
    '"/country": {type: string, merge: lww}, "/name": {type: string, merge: lww},'
    ' "/age": {type: int, merge: lww, validate: [range::{min: 0, max: 150}]}'
)
TAIL = ', "/taxLabel": {type: string, on_missing: absent, computed: {macro: upper, args: ["/taxId"]}}'


def impure(hook: str = "fx.fail@1", mode: str = "", retry: int = 1, on_missing: str = "", validate: str = "") -> str:
    inside = f", failure_mode: {mode}" if mode else ""
    outside = f", on_missing: {on_missing}" if on_missing else ""
    return (
        f'"/taxId": {{type: string{outside}{validate}, computed: {{impure: {{hook: "{hook}", depends: ["/country"],'
        f' reason: "registry", timeout: "1s", retry: {{max: {retry}, backoff: "1ms"}}, deadline: "3s",'
        f" recompute_by: [admin]{inside}}}}}}}"
    )


def build(backend: Storage, field: str, top: str = "", prefix: str = "s") -> Aduku:
    behaviors = BehaviorRegistry.with_builtins()
    register_fixtures(behaviors)
    registry = SchemaRegistry()
    registry.put(
        load_schema(
            schema_text(fields=BASE + ", " + field + TAIL, lenient=top or "lenient: {allowed: [backfill]},"), behaviors
        )
    )
    registry.activate(1)
    return Aduku(backend, registry, behaviors=behaviors, ids=SequentialIds(prefix))


def computed(value: object) -> Annotated:
    return Annotated(("computed",), value)


def stored(backend: Storage) -> list:  # type: ignore[type-arg]
    with backend.read() as tx:
        return tx.list_effects("d1")


def test_1_reject_on_failure_rejects_the_request_and_records_nothing(backend: Storage) -> None:
    service = build(backend, impure(mode="reject_on_failure"))
    with pytest.raises(RejectedError) as caught:
        service.append("d1", [put("/country", "US")], contributor=ALICE)
    assert caught.value.code is ErrorCode.IMPURE_COMPUTE_FAILED
    assert [(r.rule, r.path) for r in caught.value.reasons] == [("impure:/taxId", "/taxId")]
    assert service.list_entries("d1") == [] and stored(backend) == []
    assert "tax" not in str(caught.value).replace("taxId", "").replace("fx.fail", "")  # no value in the message


def test_2_the_default_is_reject_on_failure(backend: Storage) -> None:
    service = build(backend, impure())
    with pytest.raises(RejectedError) as caught:
        service.append("d1", [put("/country", "US")], contributor=ALICE)
    assert caught.value.code is ErrorCode.IMPURE_COMPUTE_FAILED


def test_3_a_failure_on_a_read_is_a_model_error_and_never_a_rejection(backend: Storage) -> None:
    plain = make_service(backend, schema=schema_text(fields=BASE))
    plain.append("d1", [put("/country", "US")], contributor=ALICE)  # written before the impure field existed
    behaviors = BehaviorRegistry.with_builtins()
    register_fixtures(behaviors)
    plain.schemas.put(load_schema(schema_text(version=2, fields=BASE + ", " + impure() + TAIL), behaviors))
    later = Aduku(backend, plain.schemas, behaviors=behaviors, ids=SequentialIds("later"))
    plain.schemas.activate(2)
    result = later.get("d1")
    (error,) = result.model_errors
    assert (error.rule_id, error.path_scope) == ("impure:/taxId", "/taxId") and error.message
    assert error.introduced_by_schema_version == 2 and result.document["taxId"] == computed(None)
    assert "taxLabel" not in result.document  # its input is missing, so it follows its own on_missing
    assert len(stored(backend)) == 1 and stored(backend)[0].outcome == "failure" and stored(backend)[0].error == "error"
    again = later.get("d1")
    assert again == result and behaviors.get("fx.fail", 1).impl.calls == 2  # one run of two attempts, never again  # type: ignore[union-attr]


def test_4_model_error_on_failure_accepts_the_request_and_attributes_the_error_to_it(backend: Storage) -> None:
    service = build(backend, impure(mode="model_error_on_failure"))
    result = service.append("d1", [put("/country", "US")], contributor=ALICE)
    assert [e.rule_id for e in result.model_errors_added] == ["impure:/taxId"]
    assert result.model_errors_added[0].introduced_by_request == result.request_id
    assert result.view is not None and result.view.document["taxId"] == computed(None)
    assert [e.rule_id for e in service.get("d1").model_errors] == ["impure:/taxId"]
    assert [r.outcome for r in stored(backend)] == ["failure"] and service.list_entries("d1")[0].seq == 1


def test_5_that_error_does_not_count_against_the_request_but_another_new_error_does(backend: Storage) -> None:
    service = build(backend, impure(mode="model_error_on_failure"))
    with pytest.raises(RejectedError) as caught:
        service.append("d1", [put("/country", "US"), put("/age", -5)], contributor=ALICE)
    assert caught.value.code is ErrorCode.VALIDATION_FAILED
    assert [r.rule for r in caught.value.reasons] == ["range"]  # the impure failure is not among the reasons
    assert service.list_entries("d1") == [] and stored(backend) == []


def test_6_dependents_follow_their_own_setting_and_validators_on_the_missing_value_add_nothing(
    backend: Storage,
) -> None:
    field = impure(mode="model_error_on_failure", validate=", validate: [max_len::{n: 3}]")
    service = build(backend, field)
    result = service.append("d1", [put("/country", "US")], contributor=ALICE)
    assert [e.rule_id for e in result.model_errors_added] == ["impure:/taxId"]  # the failure is the one error
    assert "taxLabel" not in service.get("d1").document


def test_7_the_field_setting_overrides_the_schema_setting(backend: Storage) -> None:
    both = build(
        backend,
        impure(mode="reject_on_failure"),
        top="impure_failure_mode: model_error_on_failure, lenient: {allowed: [backfill]},",
    )
    with pytest.raises(RejectedError) as caught:
        both.append("d1", [put("/country", "US")], contributor=ALICE)
    assert caught.value.code is ErrorCode.IMPURE_COMPUTE_FAILED
    schema_wide = build(
        backend,
        impure(),
        top="impure_failure_mode: model_error_on_failure, lenient: {allowed: [backfill]},",
        prefix="w",
    )
    schema_wide.append("d2", [put("/country", "US")], contributor=ALICE)  # the schema's setting applies
    assert [e.rule_id for e in schema_wide.get("d2").model_errors] == ["impure:/taxId"]


def test_8_a_changed_input_calls_the_hook_again_and_a_good_answer_clears_the_error(backend: Storage) -> None:
    flaky = impure(hook="fx.flaky@1", mode="model_error_on_failure", retry=0)
    service = build(backend, flaky)
    service.append("d1", [put("/country", "US")], contributor=ALICE)  # the first call for US fails
    assert [e.rule_id for e in service.get("d1").model_errors] == ["impure:/taxId"]
    result = service.append(
        "d1", [put("/country", "CA")], contributor=ALICE
    )  # a new input is a new call: fails (first)
    assert [e.rule_id for e in result.model_errors_added] == []  # the same error, still there
    service.append("d1", [put("/country", "US")], contributor=ALICE)  # back to US: its recorded failure is reused
    assert [e.rule_id for e in service.get("d1").model_errors] == ["impure:/taxId"]


def test_9_a_failure_after_an_earlier_success_replaces_the_value_only_for_new_inputs(backend: Storage) -> None:
    behaviors = BehaviorRegistry.with_builtins()
    register_fixtures(behaviors)
    behaviors.register_hook(
        "fx.pick", 1, CountingHook("fx.pick", lambda i, n: "ok" if i["/country"] == "US" else 1 / 0)
    )
    registry = SchemaRegistry()
    registry.put(
        load_schema(
            schema_text(fields=BASE + ", " + impure(hook="fx.pick@1", mode="model_error_on_failure", retry=0)),
            behaviors,
        )
    )
    registry.activate(1)
    service = Aduku(backend, registry, behaviors=behaviors, ids=SequentialIds("p"))
    service.append("d1", [put("/country", "US")], contributor=ALICE)
    assert service.get("d1").document["taxId"] == computed("ok") and service.get("d1").model_errors == ()
    service.append("d1", [put("/country", "DE")], contributor=ALICE)
    assert service.get("d1").document["taxId"] == computed(None)
    assert [e.rule_id for e in service.get("d1").model_errors] == ["impure:/taxId"]
    service.append("d1", [put("/country", "US")], contributor=ALICE)
    assert service.get("d1").document["taxId"] == computed("ok") and service.get("d1").model_errors == ()


def test_10_a_cold_read_attributes_the_error_as_the_service_that_wrote_it_did(backend: Storage) -> None:
    service = build(backend, impure(mode="model_error_on_failure"))
    result = service.append("d1", [put("/country", "US")], contributor=ALICE)
    warm = service.get("d1").model_errors
    with backend.transaction() as tx:
        tx.drop_view("d1")
    cold = build(backend, impure(mode="model_error_on_failure"), prefix="cold").get("d1").model_errors
    assert cold == warm and cold[0].introduced_by_request == result.request_id


def test_11_a_permitted_flagged_request_is_unchanged_by_the_policy(backend: Storage) -> None:
    service = build(backend, impure(mode="model_error_on_failure"))
    result = service.append("d1", [put("/country", "US", tier="backfill")], contributor=JOB, lenient=True)
    assert [e.rule_id for e in result.model_errors_added] == ["impure:/taxId"]


def test_12_a_fault_in_what_the_hook_answered_is_a_compute_fault_not_a_sequence_conflict(backend: Storage) -> None:
    behaviors = BehaviorRegistry.with_builtins()
    behaviors.register_hook("fx.words", 1, CountingHook("fx.words", lambda i, n: "words"))
    registry = SchemaRegistry()
    fields = (
        BASE
        + ", "
        + impure(hook="fx.words@1")
        + ', "/rounded": {type: int, computed: {macro: round, args: ["/taxId", lit::2]}}'
    )
    registry.put(load_schema(schema_text(fields=fields), behaviors))
    registry.activate(1)
    service = Aduku(backend, registry, behaviors=behaviors, ids=SequentialIds("f"))
    with pytest.raises(RejectedError) as caught:  # round(taxId) faults once the hook has answered with a string
        service.append("d1", [put("/country", "US")], contributor=ALICE)
    assert caught.value.code is ErrorCode.COMPUTE_FAULT and "round" in str(caught.value)
    assert service.list_entries("d1") == [] and stored(backend) == []  # nothing, outcomes included, is kept
