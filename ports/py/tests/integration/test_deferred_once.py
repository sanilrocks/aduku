"""US1: a `once` value is fetched on the first read, stored, shown as `deferred::`, and kept (FR-003, FR-005, FR-006,
FR-019, FR-024, FR-030)."""

from aduku.behaviors.registry import BehaviorRegistry
from aduku.errors import RejectedError
from aduku.ion_codec import Annotated, to_canonical
from aduku.storage.base import Storage
from tests.deferred_helpers import START, build, deferred, deferred_value
from tests.fixtures import CountingHook
from tests.service_helpers import ALICE, ROOT, put

SCORE = f'"/creditScore": {deferred()}'


def test_the_first_read_calls_the_source_once_stores_the_answer_and_shows_it(backend: Storage) -> None:
    built = build(backend, SCORE)
    built.service.append("d1", [put("/name", "Alice")], contributor=ALICE)
    assert built.calls("fx.score@1") == 0  # a write never calls a source
    result = built.service.get("d1")
    assert result.document["creditScore"] == Annotated(("deferred",), 712)
    (status,) = result.deferred
    assert (status.path, status.status, status.as_of, status.error, status.attempts) == (
        "/creditScore", "ok", START, None, 1,
    )  # fmt: skip
    assert built.calls("fx.score@1") == 1
    record = built.stored()["/creditScore"]
    assert (record.value, record.has_value, record.status, record.hook) == ("712", True, "ok", "fx.score@1")


def test_later_reads_serve_the_stored_value_without_a_call(backend: Storage) -> None:
    built = build(backend, SCORE)
    built.service.append("d1", [put("/name", "Alice")], contributor=ALICE)
    first = built.service.get("d1")
    built.clock.advance(86400)
    assert built.service.get("d1") == first and built.service.get("d1") == first
    assert built.calls("fx.score@1") == 1


def test_a_changed_input_keeps_a_stored_once_value(backend: Storage) -> None:
    built = build(backend, SCORE)
    built.service.append("d1", [put("/name", "Alice")], contributor=ALICE)
    built.service.get("d1")
    built.service.append("d1", [put("/name", "Alicia")], contributor=ALICE)
    assert deferred_value(built.service.get("d1"), "creditScore") == 712 and built.calls("fx.score@1") == 1


def test_with_the_opt_in_a_changed_input_fetches_one_new_value(backend: Storage) -> None:
    built = build(backend, f'"/creditScore": {deferred(on_dependency_change="recompute")}')
    built.service.append("d1", [put("/name", "Alice")], contributor=ALICE)
    built.service.get("d1")
    built.service.append("d1", [put("/name", "Alicia")], contributor=ALICE)
    built.service.get("d1")
    built.service.get("d1")
    assert built.calls("fx.score@1") == 2  # once for each distinct input, not once per read


def test_missing_inputs_make_the_field_pending_and_nothing_is_called(backend: Storage) -> None:
    built = build(backend, SCORE)
    built.service.append("d1", [put("/country", "US")], contributor=ALICE)
    result = built.service.get("d1")
    assert (result.deferred[0].status, built.calls("fx.score@1")) == ("pending", 0)
    assert result.document["creditScore"] == Annotated(("deferred",), None)  # on_missing: null
    built.service.append("d1", [put("/name", "Alice")], contributor=ALICE)
    assert deferred_value(built.service.get("d1"), "creditScore") == 712 and built.calls("fx.score@1") == 1


def test_a_write_makes_no_call_whether_it_is_accepted_or_rejected(backend: Storage) -> None:
    built = build(backend, SCORE)
    built.service.append("d1", [put("/name", "Alice")], contributor=ALICE)
    try:
        built.service.append("d1", [put("/creditScore", 5, tier="admin")], contributor=ROOT)
    except RejectedError:
        pass
    built.service.append("d1", [put("/name", "Alicia")], contributor=ALICE)
    assert built.calls("fx.score@1") == 0 and built.stored() == {}


def test_an_empty_document_calls_nothing(backend: Storage) -> None:
    built = build(backend, SCORE)
    result = built.service.get("nobody")
    assert built.calls("fx.score@1") == 0 and [d.status for d in result.deferred] == ["pending"]


def test_the_core_does_not_depend_on_the_store_and_a_rebuild_makes_no_call(backend: Storage) -> None:
    built = build(backend, SCORE)
    built.service.append("d1", [put("/name", "Alice")], contributor=ALICE)
    full = built.service.get("d1")
    core = {k: v for k, v in full.document.items() if k != "creditScore"}
    with backend.transaction() as tx:
        tx.delete_derived()  # the store is lost
    emptied = built.service.get("d1")
    assert {k: v for k, v in emptied.document.items() if k != "creditScore"} == core
    assert emptied.model_errors == full.model_errors == ()
    before = built.calls("fx.score@1")
    report = built.service.rebuild("d1")
    assert report.identical and built.calls("fx.score@1") == before  # rebuild never calls a source
    assert to_canonical(built.service.get("d1").document) == to_canonical(emptied.document)


def test_a_wrong_type_or_no_answer_is_a_failure_that_is_not_stored(backend: Storage) -> None:
    def register(behaviors: BehaviorRegistry) -> None:
        behaviors.register_hook("fx.text", 1, CountingHook("fx.text", lambda i, n: "seven"))  # not an int
        behaviors.register_hook("fx.none", 1, CountingHook("fx.none", lambda i, n: None))  # no answer

    fields = f'"/text": {deferred(hook="fx.text@1")}, "/nothing": {deferred(hook="fx.none@1")}'
    built = build(backend, fields, extra=register)
    built.service.append("d1", [put("/name", "Alice")], contributor=ALICE)
    result = built.service.get("d1")
    assert [(d.path, d.status) for d in result.deferred] == [("/nothing", "error"), ("/text", "error")]
    assert deferred_value(result, "text") is None and deferred_value(result, "nothing") is None
    for path in ("/text", "/nothing"):
        record = built.stored()[path]
        assert (record.has_value, record.value, record.error) == (False, None, "bad_value")


def test_a_schema_version_changes_what_is_fetched_only_if_it_changes_the_declaration(backend: Storage) -> None:
    from tests.deferred_helpers import PLAIN
    from tests.service_helpers import schema_text

    built = build(backend, SCORE)
    built.service.append("d1", [put("/name", "Alice")], contributor=ALICE)
    built.service.get("d1")
    other = '"/note": {type: string, merge: lww}'
    built.add_schema(schema_text(version=2, fields=f"{PLAIN}, {SCORE}, {other}"))
    built.service.get("d1")
    assert built.calls("fx.score@1") == 1  # the declaration is the same: the stored value is kept
    built.behaviors.register_hook("fx.score", 2, lambda inputs, timeout: 800)
    newer = f'"/creditScore": {deferred(hook="fx.score@2")}'
    built.add_schema(schema_text(version=3, fields=f"{PLAIN}, {newer}"))
    assert deferred_value(built.service.get("d1"), "creditScore") == 800  # a new source version: fetched again
