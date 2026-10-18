"""FR-008: retries are no-ops (SPEC 4.2 item 4)."""

import threading

import pytest

from aduku.errors import ErrorCode, RejectedError
from aduku.schema.loader import load_schema
from aduku.storage.base import Storage
from tests.service_helpers import ALICE, BOB, make_service, put, schema_text

AUDIT = "rejection_audit: {enabled: true, mode: full, retention: {max_entries: 100}},"


def test_same_key_payload_and_identity_is_a_no_op_duplicate(backend: Storage) -> None:
    service = make_service(backend, audit=AUDIT)
    first = service.append("d1", [put(), put("/age", 3)], contributor=ALICE, request_key="K")
    again = service.append("d1", [put(), put("/age", 3)], contributor=ALICE, request_key="K")
    assert not first.duplicate and again.duplicate
    assert again.entries == first.entries and again.request_id == first.request_id
    assert [e.seq for e in service.list_entries("d1")] == [1, 2]
    assert service.list_rejections("d1") == []


def test_same_key_with_another_payload_is_rejected_and_the_original_kept(backend: Storage) -> None:
    service = make_service(backend)
    service.append("d1", [put()], contributor=ALICE, request_key="K")
    with pytest.raises(RejectedError) as caught:
        service.append("d1", [put(value="Mallory")], contributor=ALICE, request_key="K")
    assert caught.value.code is ErrorCode.REQUEST_KEY_REUSED
    assert [e.value for e in service.list_entries("d1")] == ["Alice"]


def test_without_a_key_a_repeat_is_applied_again(backend: Storage) -> None:
    service = make_service(backend)
    service.append("d1", [put(value="Alice")], contributor=ALICE)
    service.append("d1", [put(value="Bob")], contributor=BOB)
    result = service.append("d1", [put(value="Alice")], contributor=ALICE)
    assert not result.duplicate
    assert [e.value for e in service.list_entries("d1")] == ["Alice", "Bob", "Alice"]


def test_a_key_is_scoped_to_document_and_contributor(backend: Storage) -> None:
    service = make_service(backend)
    service.append("d1", [put()], contributor=ALICE, request_key="K")
    assert not service.append("d2", [put()], contributor=ALICE, request_key="K").duplicate
    assert not service.append("d1", [put()], contributor=BOB, request_key="K").duplicate


def test_a_key_whose_original_request_was_rejected_is_processed_as_new(backend: Storage) -> None:
    service = make_service(backend)
    with pytest.raises(RejectedError):
        service.append("d1", [put("/nope")], contributor=ALICE, request_key="K")
    result = service.append("d1", [put()], contributor=ALICE, request_key="K")
    assert not result.duplicate and [seq for _, seq in result.entries] == [1]


def test_expected_seq_is_not_part_of_the_request_hash(backend: Storage) -> None:
    service = make_service(backend)
    service.append("d1", [put()], contributor=ALICE, request_key="K", expected_seq=0)
    service.append("d1", [put("/age", 1)], contributor=BOB)
    again = service.append("d1", [put()], contributor=ALICE, request_key="K", expected_seq=99)
    assert again.duplicate  # not SEQ_CONFLICT: a retry never conflicts


def test_a_duplicate_is_not_validated_again(backend: Storage) -> None:
    service = make_service(backend, audit=AUDIT)
    service.append("d1", [put("/name", "x")], contributor=ALICE, request_key="K")
    # schema 2 makes /name derived, so the same request would now be rejected if it were validated
    service.schemas.put(load_schema(schema_text(version=2, fields='"/name": {type: string, computed: {macro: upper}}')))
    service.schemas.activate(2)
    again = service.append("d1", [put("/name", "x")], contributor=ALICE, request_key="K")
    assert again.duplicate and service.list_rejections("d1") == []


def test_reusing_an_entry_idempotency_key_rejects_the_request(backend: Storage) -> None:
    service = make_service(backend)
    service.append("d1", [put(idem_key="E1")], contributor=ALICE)
    before = service.list_entries("d1")
    with pytest.raises(RejectedError) as caught:
        service.append("d1", [put("/age", 1), put("/age", 2, idem_key="E1")], contributor=BOB)
    assert caught.value.code is ErrorCode.DUPLICATE_IDEM_KEY
    assert caught.value.reasons[0].entry_index == 1
    assert service.list_entries("d1") == before
    # the same key on another document is independent
    service.append("d2", [put(idem_key="E1")], contributor=ALICE)


def test_two_simultaneous_requests_with_one_key_commit_exactly_one(backend: Storage) -> None:
    service = make_service(backend)
    results = []
    barrier = threading.Barrier(2)

    def worker() -> None:
        barrier.wait()
        results.append(service.append("d1", [put()], contributor=ALICE, request_key="K"))

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(r.duplicate for r in results) == [False, True]
    assert len(service.list_entries("d1")) == 1
    assert results[0].entries == results[1].entries


@pytest.mark.parametrize("value", [b"raw", object(), 1 + 2j])
def test_an_unstorable_value_with_a_request_key_is_rejected_cleanly(backend: Storage, value: object) -> None:
    service = make_service(backend)
    with pytest.raises(RejectedError) as caught:
        service.append("d1", [put("/name", value)], contributor=ALICE, request_key="K")
    assert caught.value.code is ErrorCode.TYPE_MISMATCH
    assert service.list_entries("d1") == []
    # and the key is free to use for a valid request afterwards
    assert not service.append("d1", [put()], contributor=ALICE, request_key="K").duplicate


def test_the_hash_depends_on_the_value_of_every_non_delete_operation() -> None:
    from aduku.engine.request_hash import request_hash
    from aduku.model import EntryInput

    def digest(op: str, value: object) -> str:
        return request_hash("d1", "alice", False, [EntryInput("user", "/n", op, value)])  # type: ignore[arg-type]

    assert digest("increment", 1) != digest("increment", 2)
    assert digest("set", 1) != digest("set", 2)
    assert digest("delete", 1) == digest("delete", 2)
