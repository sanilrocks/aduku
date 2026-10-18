"""FR-002, FR-029, SC-005: sequence numbers, atomic requests, conflicts and storage failures."""

import threading
from collections.abc import Iterator, Sequence
from contextlib import contextmanager

import pytest

from aduku.errors import ErrorCode, RejectedError, StorageError
from aduku.model import Entry
from aduku.storage.base import RequestRecord, SequentialIds, Storage, Tx
from aduku.storage.memory import MemoryStorage
from tests.service_helpers import ALICE, BOB, delete, make_service, put


def snapshot(service, doc: str = "d1"):  # type: ignore[no-untyped-def]
    return (service.list_entries(doc), service.last_seq(doc))


def test_entries_get_consecutive_sequence_numbers_and_one_request_id(backend: Storage) -> None:
    service = make_service(backend)
    first = service.append("d1", [put(), put("/age", 30)], contributor=ALICE)
    second = service.append("d1", [put("/tags", ("a",))], contributor=BOB)
    assert [seq for _, seq in first.entries] == [1, 2] and [seq for _, seq in second.entries] == [3]
    stored = service.list_entries("d1")
    assert [e.seq for e in stored] == [1, 2, 3]
    assert stored[0].request_id == stored[1].request_id == first.request_id != second.request_id
    assert stored[0].contributor == "alice" and stored[2].contributor == "bob" and stored[0].tier == "user"
    assert all(e.schema_version == 1 and not e.lenient for e in stored)


def test_documents_are_independent(backend: Storage) -> None:
    service = make_service(backend)
    service.append("d1", [put()], contributor=ALICE)
    result = service.append("d2", [put()], contributor=ALICE)
    assert [seq for _, seq in result.entries] == [1]


def test_a_rejected_request_leaves_nothing_and_consumes_no_sequence_number(backend: Storage) -> None:
    service = make_service(backend)
    service.append("d1", [put()], contributor=ALICE)
    before = snapshot(service)
    with pytest.raises(RejectedError) as caught:
        service.append("d1", [put("/age", 30), put("/nope"), put("/age", "x")], contributor=ALICE)
    assert [r.entry_index for r in caught.value.reasons] == [1, 2]
    assert snapshot(service) == before
    after = service.append("d1", [put("/age", 30)], contributor=ALICE)
    assert [seq for _, seq in after.entries] == [2]


def test_entries_of_one_request_may_depend_on_each_other(backend: Storage) -> None:
    service = make_service(backend)
    result = service.append("d1", [put(), delete(), put(value="Bob")], contributor=ALICE)
    assert [seq for _, seq in result.entries] == [1, 2, 3]


def test_stale_expected_seq_is_a_conflict(backend: Storage) -> None:
    service = make_service(backend)
    service.append("d1", [put()], contributor=ALICE)
    service.append("d1", [put("/age", 1)], contributor=ALICE, expected_seq=1)
    before = snapshot(service)
    with pytest.raises(RejectedError) as caught:
        service.append("d1", [put("/age", 2)], contributor=ALICE, expected_seq=1)
    assert caught.value.code is ErrorCode.SEQ_CONFLICT
    assert snapshot(service) == before


def test_empty_request_is_a_programming_error(backend: Storage) -> None:
    with pytest.raises(ValueError):
        make_service(backend).append("d1", [], contributor=ALICE)


def test_concurrent_appends_get_distinct_increasing_sequence_numbers(backend: Storage) -> None:
    service = make_service(backend)
    seen: list[int] = []
    lock = threading.Lock()

    def worker(n: int) -> None:
        for i in range(5):
            result = service.append("d1", [put("/age", n * 10 + i)], contributor=ALICE)
            with lock:
                seen.extend(seq for _, seq in result.entries)

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(seen) == list(range(1, 41))
    assert [e.seq for e in service.list_entries("d1")] == list(range(1, 41))


class FailingStorage:
    """Delegates to a real storage but fails every append, as a disk failure mid-commit would."""

    def __init__(self, inner: Storage) -> None:
        self.inner = inner

    @contextmanager
    def transaction(self) -> Iterator[Tx]:
        with self.inner.transaction() as tx:
            yield _FailingTx(tx)

    def read(self):  # type: ignore[no-untyped-def]
        return self.inner.read()

    def audit_backend(self):  # type: ignore[no-untyped-def]
        return self.inner.audit_backend()

    def close(self) -> None:
        self.inner.close()


class _FailingTx:
    def __init__(self, tx: Tx) -> None:
        self._tx = tx

    def __getattr__(self, name: str):  # type: ignore[no-untyped-def]
        return getattr(self._tx, name)

    def append_request(self, request: RequestRecord, entries: Sequence[Entry]) -> None:
        self._tx.append_request(request, entries[:1])  # part of the request reaches the database ...
        raise StorageError("disk full")  # ... and then the commit fails


def test_a_storage_failure_makes_no_entry_visible_and_is_not_a_rejection(backend: Storage) -> None:
    service = make_service(backend)
    service.append("d1", [put()], contributor=ALICE)
    before = snapshot(service)
    broken = make_service(FailingStorage(backend), ids=SequentialIds("broken"))
    with pytest.raises(StorageError) as caught:
        broken.append("d1", [put("/age", 1), put("/age", 2)], contributor=ALICE)
    assert not isinstance(caught.value, RejectedError)
    assert caught.value.code is ErrorCode.STORAGE_ERROR
    assert snapshot(service) == before


def test_memory_storage_is_the_default_backend_type() -> None:
    assert isinstance(make_service().storage, MemoryStorage)


def test_list_entries_limit_is_applied_by_the_store(backend: Storage) -> None:
    service = make_service(backend)
    service.append("d1", [put("/age", n) for n in range(1, 6)], contributor=ALICE)
    assert [e.seq for e in service.list_entries("d1", limit=2)] == [1, 2]
    assert [e.seq for e in service.list_entries("d1", since_seq=3, limit=10)] == [4, 5]
