"""Both storage backends obey the same transactional contract (plan.md, research R-6)."""

import pathlib
from collections.abc import Iterator
from decimal import Decimal

import pytest

from aduku.ion_codec import TOMBSTONE, FrozenStruct
from aduku.storage.base import ConstraintError, RequestRecord, Storage, ViewRow
from aduku.storage.memory import MemoryStorage
from aduku.storage.sqlite import SqliteStorage
from tests.helpers import make_entry


@pytest.fixture(params=["memory", "sqlite"])
def storage(request: pytest.FixtureRequest, tmp_path: pathlib.Path) -> Iterator[Storage]:
    backend: Storage = MemoryStorage() if request.param == "memory" else SqliteStorage(str(tmp_path / "t.db"))
    yield backend
    backend.close()


def req(request_id: str = "r1", doc: str = "d1", contributor: str = "alice", key: str | None = None) -> RequestRecord:
    return RequestRecord(
        request_id=request_id,
        doc=doc,
        contributor=contributor,
        request_key=key,
        request_hash="h-" + key if key else None,
    )


def test_empty_document(storage: Storage) -> None:
    with storage.transaction() as tx:
        assert tx.last_seq("d1") == 0
        assert tx.entries("d1") == []


def test_append_and_read_in_seq_order(storage: Storage) -> None:
    entries = [make_entry(seq=1), make_entry(seq=2, path="/age", value=30)]
    with storage.transaction() as tx:
        tx.append_request(req(), entries)
    with storage.transaction() as tx:
        assert tx.last_seq("d1") == 2
        assert tx.entries("d1") == entries
        assert tx.entries("d1", since_seq=1) == entries[1:]
        assert tx.entries_of_request("r1") == entries


def test_values_survive_storage(storage: Storage) -> None:
    value = FrozenStruct({"a": Decimal("1.50"), "b": (1, 2), "c": None, "d": TOMBSTONE})
    with storage.transaction() as tx:
        tx.append_request(req(), [make_entry(value=value), make_entry(seq=2, op="delete", value=TOMBSTONE)])
    with storage.transaction() as tx:
        stored = tx.entries("d1")
    assert stored[0].value == value
    assert stored[1].value is TOMBSTONE and stored[1].op == "delete"


def test_sequence_must_be_consecutive(storage: Storage) -> None:
    with storage.transaction() as tx:
        with pytest.raises(ConstraintError):
            tx.append_request(req(), [make_entry(seq=2)])
    with storage.transaction() as tx:
        assert tx.last_seq("d1") == 0


def test_documents_have_independent_sequences(storage: Storage) -> None:
    with storage.transaction() as tx:
        tx.append_request(req("r1", "d1"), [make_entry("d1", 1)])
        tx.append_request(req("r2", "d2"), [make_entry("d2", 1, request_id="r2")])
        assert tx.last_seq("d1") == 1 and tx.last_seq("d2") == 1


def test_exception_rolls_back_everything(storage: Storage) -> None:
    with pytest.raises(RuntimeError):
        with storage.transaction() as tx:
            tx.append_request(req(), [make_entry()])
            raise RuntimeError("boom")
    with storage.transaction() as tx:
        assert tx.last_seq("d1") == 0 and tx.entries("d1") == []


def test_failed_append_leaves_nothing_behind(storage: Storage) -> None:
    with storage.transaction() as tx:
        tx.append_request(req("r1"), [make_entry(seq=1, idem_key="k")])
    with storage.transaction() as tx:
        with pytest.raises(ConstraintError):
            tx.append_request(
                req("r2"), [make_entry(seq=2, request_id="r2"), make_entry(seq=3, request_id="r2", idem_key="k")]
            )
    with storage.transaction() as tx:
        assert tx.last_seq("d1") == 1


def test_idempotency_key_is_unique_per_document(storage: Storage) -> None:
    with storage.transaction() as tx:
        tx.append_request(req("r1"), [make_entry(seq=1, idem_key="k")])
        assert tx.idem_key_seen("d1", "k") and not tx.idem_key_seen("d1", "other")
        assert not tx.idem_key_seen("d2", "k")
        tx.append_request(req("r2", "d2"), [make_entry("d2", 1, request_id="r2", idem_key="k")])


def test_request_key_lookup_and_uniqueness(storage: Storage) -> None:
    with storage.transaction() as tx:
        tx.append_request(req("r1", key="K"), [make_entry(seq=1, request_id="r1", request_key="K", request_hash="h-K")])
    with storage.transaction() as tx:
        found = tx.find_request("d1", "alice", "K")
        assert found == req("r1", key="K")
        assert tx.find_request("d1", "bob", "K") is None
        assert tx.find_request("d2", "alice", "K") is None
        with pytest.raises(ConstraintError):
            tx.append_request(req("r2", key="K"), [make_entry(seq=2, request_id="r2", request_key="K")])
    with storage.transaction() as tx:
        tx.append_request(
            req("r3", contributor="bob", key="K"), [make_entry(seq=2, request_id="r3", contributor="bob")]
        )


def test_view_cache(storage: Storage) -> None:
    view = ViewRow(
        doc="d1",
        watermark_seq=2,
        schema_version=7,
        rules_fingerprint="f",
        document="{a:1}",
        model_errors="[]",
        provenance="{}",
    )
    with storage.transaction() as tx:
        assert tx.get_view("d1") is None
        tx.put_view(view)
    with storage.transaction() as tx:
        assert tx.get_view("d1") == view
        tx.put_view(ViewRow("d1", 3, 7, "f", "{a:2}", "[]", "{}"))
    with storage.transaction() as tx:
        assert tx.get_view("d1") is not None and tx.get_view("d1").watermark_seq == 3  # type: ignore[union-attr]
        tx.drop_view("d1")
    with storage.transaction() as tx:
        assert tx.get_view("d1") is None


def test_transaction_handle_is_unusable_after_the_block(storage: Storage) -> None:
    with storage.transaction() as tx:
        tx.append_request(req(), [make_entry()])
    with pytest.raises(RuntimeError):
        tx.append_request(req("r2"), [make_entry(seq=2, request_id="r2")])
    with pytest.raises(RuntimeError):
        tx.entries("d1")
    with storage.transaction() as fresh:
        assert fresh.last_seq("d1") == 1


def test_transaction_handle_is_unusable_after_a_rollback(storage: Storage) -> None:
    with pytest.raises(RuntimeError):
        with storage.transaction() as tx:
            raise RuntimeError("boom")
    with pytest.raises(RuntimeError):
        tx.last_seq("d1")


def test_entries_limit(storage: Storage) -> None:
    entries = [make_entry(seq=n, path="/age", value=n) for n in range(1, 6)]
    with storage.transaction() as tx:
        tx.append_request(req(), entries)
    with storage.read() as tx:
        assert [e.seq for e in tx.entries("d1", limit=2)] == [1, 2]
        assert [e.seq for e in tx.entries("d1", since_seq=2, limit=2)] == [3, 4]
        assert [e.seq for e in tx.entries("d1", since_seq=9, limit=2)] == []
        assert len(tx.entries("d1", limit=None)) == 5


def test_read_transactions_see_data_and_refuse_writes(storage: Storage) -> None:
    with storage.transaction() as tx:
        tx.append_request(req(), [make_entry()])
    with storage.read() as tx:
        assert tx.last_seq("d1") == 1 and len(tx.entries("d1")) == 1 and tx.get_view("d1") is None
        for write in (
            lambda: tx.append_request(req("r2"), [make_entry(seq=2, request_id="r2")]),
            lambda: tx.put_view(ViewRow("d1", 1, 1, "f", "{}", "[]", "{}")),
            lambda: tx.drop_view("d1"),
        ):
            with pytest.raises(RuntimeError):
                write()
    with pytest.raises(RuntimeError):
        tx.last_seq("d1")  # the handle ends with the block


def test_a_read_does_not_wait_for_a_writer(tmp_path: pathlib.Path) -> None:
    import threading

    path = str(tmp_path / "shared.db")
    writer, reader = SqliteStorage(path), SqliteStorage(path)
    done = threading.Event()
    seen: list[int] = []

    def read() -> None:
        with reader.read() as tx:
            seen.append(tx.last_seq("d1"))
        done.set()

    with writer.transaction() as tx:  # holds the database write lock
        tx.append_request(req(), [make_entry()])
        thread = threading.Thread(target=read)
        thread.start()
        assert done.wait(timeout=5), "the read waited for the writer's lock"
    thread.join()
    assert seen == [0]  # the reader saw the last committed state, not the uncommitted append
    writer.close()
    reader.close()


def test_a_transaction_survives_the_database_rolling_back_underneath_it(tmp_path: pathlib.Path) -> None:
    storage = SqliteStorage(str(tmp_path / "rb.db"))
    with pytest.raises(RuntimeError, match="boom"):
        with storage.transaction():
            storage.connection.execute("ROLLBACK")  # as SQLite does itself on some errors
            raise RuntimeError("boom")
    with storage.transaction() as tx:  # and the storage is still usable
        assert tx.last_seq("d1") == 0
    storage.close()


def test_memory_transactions_share_untouched_documents() -> None:
    storage = MemoryStorage()
    with storage.transaction() as tx:
        tx.append_request(req("r1", "d1"), [make_entry("d1", 1)])
    untouched = storage._state.entries["d1"]
    with storage.transaction() as tx:
        tx.append_request(req("r2", "d2"), [make_entry("d2", 1, request_id="r2")])
    assert storage._state.entries["d1"] is untouched  # not copied


def test_a_failed_transaction_undoes_everything_it_did(storage: Storage) -> None:
    with storage.transaction() as tx:
        tx.append_request(req("r0", key="K0"), [make_entry(seq=1, request_id="r0", idem_key="I0", request_key="K0")])
        tx.put_view(ViewRow("d1", 1, 1, "f", "{a:1}", "[]", "{}"))
    with pytest.raises(RuntimeError):
        with storage.transaction() as tx:
            tx.append_request(
                req("r1", key="K1"), [make_entry(seq=2, request_id="r1", idem_key="I1", request_key="K1")]
            )
            tx.put_view(ViewRow("d1", 2, 1, "f", "{a:2}", "[]", "{}"))
            tx.drop_view("other")
            raise RuntimeError("boom")
    with storage.read() as tx:
        assert tx.last_seq("d1") == 1 and [e.seq for e in tx.entries("d1")] == [1]
        assert tx.entries_of_request("r1") == [] and len(tx.entries_of_request("r0")) == 1
        assert not tx.idem_key_seen("d1", "I1") and tx.idem_key_seen("d1", "I0")
        assert tx.find_request("d1", "alice", "K1") is None and tx.find_request("d1", "alice", "K0") is not None
        assert tx.get_view("d1").watermark_seq == 1  # type: ignore[union-attr]


def test_memory_writes_do_not_copy_the_whole_ledger() -> None:
    storage = MemoryStorage()
    with storage.transaction() as tx:
        tx.append_request(req("r0", key="K0"), [make_entry(seq=1, request_id="r0", idem_key="I0")])
    held = (storage._state.idem_keys, storage._state.requests, storage._state.request_keys, storage._state.entries)
    with storage.transaction() as tx:
        tx.append_request(req("r1", key="K1"), [make_entry(seq=2, request_id="r1", idem_key="I1")])
    assert held == (
        storage._state.idem_keys,
        storage._state.requests,
        storage._state.request_keys,
        storage._state.entries,
    )
    assert all(
        a is b
        for a, b in zip(
            held,
            (storage._state.idem_keys, storage._state.requests, storage._state.request_keys, storage._state.entries),
            strict=True,
        )
    )


@pytest.mark.parametrize("keep", [0, -1, -50])
def test_keeping_no_audit_rows_or_a_negative_number_deletes_them_all_in_every_backend(
    backend: Storage, keep: int
) -> None:
    from datetime import UTC, datetime

    from aduku.storage.base import AuditRow

    audit = backend.audit_backend()
    for n in range(3):
        audit.insert(
            AuditRow("d1", "alice", ("user",), False, "[]", "[]", 1, datetime(2026, 1, 1, 0, 0, n, tzinfo=UTC))
        )
    assert audit.delete_oldest("d1", keep) == 3 and audit.count("d1") == 0


def test_keeping_some_audit_rows_keeps_the_newest(backend: Storage) -> None:
    from datetime import UTC, datetime

    from aduku.storage.base import AuditRow

    audit = backend.audit_backend()
    for n in range(3):
        audit.insert(
            AuditRow("d1", "alice", ("user",), False, "[]", "[]", 1, datetime(2026, 1, 1, 0, 0, n, tzinfo=UTC))
        )
    assert audit.delete_oldest("d1", 2) == 1
    assert [r.recorded_at.second for r in audit.rows("d1")] == [1, 2]
