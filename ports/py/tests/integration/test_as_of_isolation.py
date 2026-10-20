"""Feature 005 (FR-017, FR-018, scenario 5): a time-travel read calls no hook and no deferred source."""

from aduku.model import AsOf
from aduku.storage.base import Storage
from tests.deferred_helpers import PLAIN, build, deferred
from tests.service_helpers import ALICE, put, schema_text

IMPURE = (
    '"/taxId": {type: string, computed: {impure: {hook: "fx.echo@1", depends: ["/country"], reason: "registry",'
    ' timeout: "1s", retry: {max: 1, backoff: "10ms"}, deadline: "3s", recompute_by: [admin]}}}'
)
FIELDS = f'"/creditScore": {deferred()}, {IMPURE}'


def calls(built) -> tuple[int, int]:  # noqa: ANN001
    return built.calls("fx.score@1"), built.calls("fx.echo@1")


def test_no_hook_or_source_is_called_at_any_point(backend: Storage) -> None:
    built = build(backend, FIELDS)
    db = built.service
    db.append("d1", [put("/name", "Alice"), put("/country", "US")], contributor=ALICE)
    db.append("d1", [put("/country", "FR")], contributor=ALICE)
    live = db.get("d1")  # the first read fetches the deferred value
    before = calls(built)
    assert before == (1, 2)
    for point in (AsOf(seq=1), AsOf(seq=2), AsOf(seq=0), AsOf(seq=99), AsOf(tx_time=built.clock.now())):
        db.get("d1", as_of=point)
        db.explain("d1", "/taxId", as_of=point)
    assert calls(built) == before
    assert db.get("d1").document == live.document


def test_an_impure_field_uses_the_outcome_recorded_for_its_inputs_at_that_point(backend: Storage) -> None:
    built = build(backend, FIELDS)
    db = built.service
    db.append("d1", [put("/country", "US")], contributor=ALICE)
    db.append("d1", [put("/country", "FR")], contributor=ALICE)
    assert db.get("d1", as_of=AsOf(seq=1)).document["taxId"].value == "tax-US"
    assert db.get("d1", as_of=AsOf(seq=2)).document["taxId"].value == "tax-FR"


def test_an_impure_field_without_a_recorded_outcome_is_a_model_error_not_a_call(backend: Storage) -> None:
    built = build(backend, FIELDS)
    db = built.service
    db.append("d1", [put("/country", "US")], contributor=ALICE)
    # version 2 names another hook for the field: no outcome is recorded for it at any point of the history
    built.add_schema(
        schema_text(
            version=2,
            fields=PLAIN + ", " + FIELDS.replace("fx.echo@1", "fx.fail@1"),
            lenient="lenient: {allowed: [backfill]},",
        )
    )
    before = (calls(built), built.calls("fx.fail@1"))
    past = db.get("d1", as_of=AsOf(seq=1))
    assert (calls(built), built.calls("fx.fail@1")) == before  # nothing was called
    assert "taxId" not in past.document
    assert [(e.rule_id, e.path_scope) for e in past.model_errors] == [("NO_RECORDED_OUTCOME", "/taxId")]


def test_deferred_fields_are_listed_unavailable_with_no_value(backend: Storage) -> None:
    built = build(backend, FIELDS)
    db = built.service
    db.append("d1", [put("/name", "Alice"), put("/country", "US")], contributor=ALICE)
    assert db.get("d1").document["creditScore"].value == 712  # a live read stored it
    past = db.get("d1", as_of=AsOf(seq=1))
    assert "creditScore" not in past.document
    (status,) = past.deferred
    assert (status.path, status.status, status.reason) == ("/creditScore", "unavailable", "history_not_kept")
