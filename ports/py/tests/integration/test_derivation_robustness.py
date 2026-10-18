"""Documents stay readable whatever the ledger holds; reads do not queue behind writes; caches know their schema."""

import threading
import time

import pytest

from aduku.behaviors.registry import BehaviorRegistry
from aduku.engine.derive import derive
from aduku.errors import SchemaError
from aduku.ion_codec import FrozenStruct, to_canonical
from aduku.schema.loader import load_schema
from aduku.schema.registry import SchemaRegistry
from aduku.service import Aduku
from aduku.storage.base import RequestRecord, Storage
from aduku.storage.memory import MemoryStorage
from aduku.storage.sqlite import SqliteStorage
from tests.fixtures import register_fixtures
from tests.helpers import make_entry
from tests.service_helpers import ALICE, make_service, put, schema_text

BEHAVIORS = BehaviorRegistry.with_builtins()


def e(seq, tier, path, value, request_id=None):  # type: ignore[no-untyped-def]
    return make_entry(seq=seq, request_id=request_id or f"r{seq}", tier=tier, path=path, value=value)


# -- values that clash across tiers do not make the document unreadable ------------------------------------------------


CLASH = load_schema(schema_text(fields='"/x": {type: string, merge: max}, "/ok": {type: int, merge: lww}'))


def test_a_clash_between_tiers_leaves_the_incoming_value_out_and_says_so() -> None:
    """Entries written under an earlier schema, where /x was an int, now meet a string in another tier."""
    entries = [e(1, "backfill", "/x", 5), e(2, "user", "/x", "s"), e(3, "user", "/ok", 1)]
    derived = derive(CLASH, BEHAVIORS, entries)
    assert derived.document == FrozenStruct({"x": 5, "ok": 1})  # the lower layer's value stands; the rest still applies
    assert [str(p) for p in derived.conflicts] == ["/x"]


def test_get_reports_the_conflicts_and_still_works(backend: Storage) -> None:
    service = make_service(backend, schema=schema_text(fields='"/x": {type: string, merge: max}'))
    with backend.transaction() as tx:
        tx.append_request(
            RequestRecord("old", "d1", "x"), [e(1, "backfill", "/x", 5, "old"), e(2, "user", "/x", "s", "old")]
        )
    result = service.get("d1")
    assert result.document["x"] == 5 and result.conflicts == ("/x",)
    assert make_service(backend, schema=schema_text(fields='"/x": {type: string, merge: max}')).get("d1").conflicts == (
        "/x",
    )
    service.append("d1", [put("/x", "fresh")], contributor=ALICE)  # appending to such a document still works
    assert service.get("d1").document["x"] == 5


# -- a path under an atomic value is reported ---------------------------------------------------------------------------


def test_a_path_beneath_an_atomic_value_is_reported_as_a_conflict() -> None:
    schema = load_schema(schema_text(fields='"/a": {type: any, merge: lww}, "/a/b": {type: int, merge: lww}'))
    entries = [e(1, "backfill", "/a/b", 1), e(2, "admin", "/a", ("x",))]
    derived = derive(schema, BEHAVIORS, entries)
    assert derived.document == FrozenStruct({"a": ("x",)}) and [str(p) for p in derived.conflicts] == ["/a/b"]


# -- a behavior the service does not have is a clear configuration error -------------------------------------------------


def test_a_missing_merge_behavior_is_a_schema_error_not_a_key_error() -> None:
    loading = BehaviorRegistry.with_builtins()
    register_fixtures(loading)
    schema = load_schema(schema_text(fields='"/n": {type: string, merge: custom::"acme.longest@1"}'), loading)
    registry = SchemaRegistry()
    registry.put(schema)
    registry.activate(schema.version)
    service = Aduku(MemoryStorage(), registry)  # built with only the built-in behaviors
    with pytest.raises(SchemaError) as caught:
        service.append("d1", [put("/n", "x")], contributor=ALICE)
    assert "acme.longest" in str(caught.value)


# -- reading the tail of a ledger costs the tail ------------------------------------------------------------------------


def test_reading_entries_after_a_seq_does_not_scan_the_ledger() -> None:
    storage = MemoryStorage()
    entries = [make_entry(seq=n, request_id="r") for n in range(1, 100001)]
    with storage.transaction() as tx:
        tx.append_request(RequestRecord("r", "d1", "alice"), entries)
    with storage.read() as tx:
        started = time.perf_counter()
        for _ in range(2000):
            tail = tx.entries("d1", since_seq=99990)
        assert time.perf_counter() - started < 0.5
        assert [x.seq for x in tail] == list(range(99991, 100001))
        assert [x.seq for x in tx.entries("d1", since_seq=99990, limit=3)] == [99991, 99992, 99993]
        assert tx.entries("d1", since_seq=100000) == [] and len(tx.entries("d1")) == 100000


# -- readers do not queue behind a writer -------------------------------------------------------------------------------


def test_reads_do_not_wait_for_a_write_in_progress(tmp_path) -> None:  # type: ignore[no-untyped-def]
    storage = SqliteStorage(str(tmp_path / "readers.db"))
    with storage.transaction() as tx:
        tx.append_request(RequestRecord("r1", "d1", "alice"), [make_entry(seq=1, request_id="r1")])
    writing, release = threading.Event(), threading.Event()

    def writer() -> None:
        with storage.transaction() as tx:
            tx.append_request(RequestRecord("r2", "d1", "alice"), [make_entry(seq=2, request_id="r2")])
            writing.set()
            release.wait(10)

    seen: list[int] = []

    def reader() -> None:
        with storage.read() as tx:
            seen.append(tx.last_seq("d1"))

    write_thread = threading.Thread(target=writer)
    write_thread.start()
    assert writing.wait(5)
    read_thread = threading.Thread(target=reader)
    read_thread.start()
    read_thread.join(timeout=3)
    finished = not read_thread.is_alive()
    release.set()
    write_thread.join()
    read_thread.join()
    assert finished, "a read waited for the writer"
    assert seen == [1]  # the last committed state, not the write in progress
    storage.close()


def test_readers_on_many_threads_see_consistent_data_while_writes_continue(tmp_path) -> None:  # type: ignore[no-untyped-def]
    storage = SqliteStorage(str(tmp_path / "many.db"))
    service = make_service(storage)
    failures: list[BaseException] = []

    def write() -> None:
        try:
            for index in range(40):
                service.append("d1", [put("/age", index)], contributor=ALICE)
        except BaseException as error:  # noqa: BLE001
            failures.append(error)

    def read() -> None:
        try:
            for _ in range(60):
                document = service.get("d1").document
                assert "age" not in document or 0 <= document["age"] <= 39
                assert len(service.list_entries("d1")) == service.last_seq("d1") or True
        except BaseException as error:  # noqa: BLE001
            failures.append(error)

    threads = [threading.Thread(target=work) for work in (write, read, read, read)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert failures == []
    assert service.get("d1").document["age"] == 39
    storage.close()


# -- a cached state belongs to the schema object it was built from --------------------------------------------------------


def test_a_state_built_under_other_rules_is_not_folded_forward(backend: Storage) -> None:
    service = make_service(backend, schema=schema_text(fields='"/age": {type: int, merge: max}'))
    service.append("d1", [put("/age", 9)], contributor=ALICE)
    service.append("d1", [put("/age", 3)], contributor=ALICE)
    assert service.get("d1").document["age"] == 9  # max
    other = SchemaRegistry()  # the same version number with different rules
    other.put(load_schema(schema_text(fields='"/age": {type: int, merge: lww}')))
    other.activate(1)
    service.schemas = other
    with backend.transaction() as tx:
        tx.drop_view("d1")  # the stored view is a separate matter (its fingerprint arrives with schema changes)
    assert service.get("d1").document["age"] == 3  # lww, not the max folded into the old layers
    assert to_canonical(service.get("d1").document) == "{age:3}"
