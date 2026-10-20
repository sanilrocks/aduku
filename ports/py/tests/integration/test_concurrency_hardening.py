"""Shared caches and the audit store stay correct under threads; cold derivations stay out of the locks."""

import threading
from contextlib import contextmanager

import pytest

from aduku.paths import Segment, parse_path
from aduku.schema.loader import load_schema
from aduku.storage.base import SequentialIds, Storage
from aduku.storage.memory import MemoryStorage
from tests.service_helpers import ALICE, make_service, put, schema_text


def run_threads(workers) -> list[BaseException]:  # type: ignore[no-untyped-def]
    failures: list[BaseException] = []

    def guarded(work) -> None:  # type: ignore[no-untyped-def]
        try:
            work()
        except BaseException as error:  # noqa: BLE001
            failures.append(error)

    threads = [threading.Thread(target=guarded, args=(work,)) for work in workers]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    return failures


def test_the_field_memo_survives_being_cleared_by_another_thread(monkeypatch: pytest.MonkeyPatch) -> None:
    import aduku.schema.model as model

    monkeypatch.setattr(model, "FIELD_CACHE_LIMIT", 3)  # constant clearing
    schema = load_schema(schema_text())

    def hammer() -> None:
        for number in range(3000):
            schema.field_for((Segment("name", f"k{number % 40}"),))
            schema.field_for(parse_path("/age"))

    assert run_threads([hammer] * 8) == []


def test_the_rule_cache_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    import aduku.behaviors.merge as merge
    from aduku.behaviors.registry import BehaviorRegistry

    monkeypatch.setattr(merge, "RULE_CACHE_LIMIT", 40)
    ctx = merge.SchemaMergeContext(load_schema(schema_text()), BehaviorRegistry.with_builtins())
    for number in range(500):
        ctx.rule_for((Segment("meta"), Segment(f"user{number}")))
    assert len(ctx._rules) <= 40


def test_memory_audit_settings_are_not_lost_to_a_rolled_back_block() -> None:
    audit = MemoryStorage().audit_backend()

    def setting(number: int):  # type: ignore[no-untyped-def]
        def work() -> None:
            for _ in range(300):
                audit.set_override(f"d{number}", "enabled")
                audit.add_pruned(f"d{number}", 1)

        return work

    def failing() -> None:
        for _ in range(300):
            try:
                with audit.atomic():
                    audit.set_override("scratch", "enabled")
                    raise RuntimeError("roll back")
            except RuntimeError:
                pass

    assert run_threads([setting(n) for n in range(4)] + [failing]) == []
    assert all(audit.get_override(f"d{n}") == "enabled" and audit.pruned(f"d{n}") == 300 for n in range(4))
    assert audit.get_override("scratch") is None


class Spy:
    """Notes whether a read or a write transaction is open, and what the service folds meanwhile."""

    def __init__(self, inner: Storage) -> None:
        self.inner, self.reading, self.writing = inner, False, False

    @contextmanager
    def read(self):  # type: ignore[no-untyped-def]
        self.reading = True
        try:
            with self.inner.read() as tx:
                yield tx
        finally:
            self.reading = False

    @contextmanager
    def transaction(self):  # type: ignore[no-untyped-def]
        self.writing = True
        try:
            with self.inner.transaction() as tx:
                yield tx
        finally:
            self.writing = False

    def audit_backend(self):  # type: ignore[no-untyped-def]
        return self.inner.audit_backend()

    def close(self) -> None:
        self.inner.close()


def spy_on_folds(monkeypatch: pytest.MonkeyPatch, spy: Spy) -> list[tuple[bool, bool, int]]:
    import aduku.engine.model_errors as service_module

    seen: list[tuple[bool, bool, int]] = []
    real = service_module.fold_entries

    def watching(schema, behaviors, state, entries, prepared=None):  # type: ignore[no-untyped-def]
        seen.append((spy.reading, spy.writing, len(entries)))
        return real(schema, behaviors, state, entries, prepared)

    monkeypatch.setattr(service_module, "fold_entries", watching)
    return seen


def test_a_cold_read_folds_the_ledger_outside_the_read_transaction(
    monkeypatch: pytest.MonkeyPatch, backend: Storage
) -> None:
    warm = make_service(backend)
    for index in range(30):
        warm.append("d1", [put("/age", index)], contributor=ALICE)
    with backend.transaction() as tx:
        tx.drop_view("d1")
    spy = Spy(backend)
    seen = spy_on_folds(monkeypatch, spy)
    make_service(spy).get("d1")  # type: ignore[arg-type]
    assert seen == [(False, False, 30)]  # folded all 30 entries, with no transaction open


def test_a_cold_append_does_not_fold_the_whole_ledger_inside_the_write_transaction(
    monkeypatch: pytest.MonkeyPatch, backend: Storage
) -> None:
    warm = make_service(backend)
    for index in range(30):
        warm.append("d1", [put("/age", index)], contributor=ALICE)
    spy = Spy(backend)
    seen = spy_on_folds(monkeypatch, spy)
    cold = make_service(spy, ids=SequentialIds("cold"))  # type: ignore[arg-type]
    cold.append("d1", [put("/age", 99)], contributor=ALICE)
    # feature 005: the saved layers cover the 30 old entries, so nothing is folded outside the lock and only the new entry inside
    assert seen == [(False, True, 1)]
    cold.append("d1", [put("/age", 100)], contributor=ALICE)
    assert seen[1:] == [(False, True, 1)]  # a warm service never warms again


def test_memory_storage_default_is_unchanged() -> None:
    assert isinstance(make_service().storage, MemoryStorage)


# -- a ledger another process moved on, a retry, and a validator that faults stay out of the write lock ---------------


def _validated_service(storage, ids):  # type: ignore[no-untyped-def]
    from tests.integration.test_model_errors import SCHEMA

    return make_service(storage, schema=SCHEMA, ids=ids)


def test_a_state_left_far_behind_is_refolded_outside_the_write_transaction(
    monkeypatch: pytest.MonkeyPatch, backend: Storage
) -> None:
    ours = _validated_service(backend, SequentialIds("ours"))
    ours.append("d1", [put("/name", "x")], contributor=ALICE)  # our state is now cached
    theirs = _validated_service(backend, SequentialIds("theirs"))
    for index in range(100):  # another process moves the ledger on by more than the inline catch-up limit
        theirs.append("d1", [put("/age", index)], contributor=ALICE)
    spy = Spy(backend)
    seen = spy_on_folds(monkeypatch, spy)
    ours.storage = spy  # type: ignore[assignment]
    ours.append("d1", [put("/name", "y")], contributor=ALICE)
    assert seen and not any(writing for _, writing, _ in seen[:-1]) and seen[-1][1:] == (True, 1)


def test_a_retry_replays_the_ledger_outside_the_write_transaction(
    monkeypatch: pytest.MonkeyPatch, backend: Storage
) -> None:
    service = _validated_service(backend, SequentialIds("a"))
    for index in range(5):
        service.append("d1", [put("/age", index)], contributor=ALICE)
    service.append("d1", [put("/name", "x")], contributor=ALICE, request_key="K")
    spy = Spy(backend)
    seen = spy_on_folds(monkeypatch, spy)
    service.storage = spy  # type: ignore[assignment]
    assert service.append("d1", [put("/name", "x")], contributor=ALICE, request_key="K").duplicate
    assert seen and not any(writing for _, writing, _ in seen)


def test_a_validator_that_faults_while_catching_up_is_a_rejection_not_a_crash(backend: Storage) -> None:
    from aduku.behaviors.registry import BehaviorRegistry
    from aduku.errors import ErrorCode, RejectedError
    from aduku.schema.loader import load_schema
    from aduku.schema.registry import SchemaRegistry
    from aduku.service import Aduku
    from tests.fixtures import register_fixtures

    def explode(subject):  # type: ignore[no-untyped-def]
        if subject.document.get("note") == "boom":
            raise RuntimeError("validator bug")
        return []

    behaviors = BehaviorRegistry.with_builtins()
    register_fixtures(behaviors)
    behaviors.register_validate("acme.explode", 1, explode)
    text = schema_text(fields='"/note": {type: string, merge: lww}').replace(
        "fields:", 'document_validate: ["acme.explode@1"], fields:'
    )
    registry = SchemaRegistry()
    registry.put(load_schema(text, behaviors))
    registry.activate(1)
    first = Aduku(backend, registry, behaviors=behaviors, ids=SequentialIds("x"))
    first.append("d1", [put("/note", "warm")], contributor=ALICE)  # its state is now cached, and then falls behind
    quiet = BehaviorRegistry.with_builtins()
    quiet.register_validate("acme.explode", 1, lambda subject: [])
    registry2 = SchemaRegistry()
    registry2.put(load_schema(text, quiet))
    registry2.activate(1)
    Aduku(backend, registry2, behaviors=quiet, ids=SequentialIds("y")).append(
        "d1", [put("/note", "boom")], contributor=ALICE
    )  # accepted where the validator is quiet
    with pytest.raises(RejectedError) as caught:
        first.append("d1", [put("/note", "other")], contributor=ALICE)
    assert caught.value.code is ErrorCode.VALIDATION_FAILED


def test_an_append_with_no_active_schema_is_logged_as_rejected(caplog: pytest.LogCaptureFixture) -> None:
    import logging

    from aduku.errors import ErrorCode, RejectedError
    from aduku.schema.registry import SchemaRegistry
    from aduku.service import Aduku

    service = Aduku(MemoryStorage(), SchemaRegistry())
    with caplog.at_level(logging.INFO, logger="aduku"), pytest.raises(RejectedError) as caught:
        service.append("d1", [put("/name", "x")], contributor=ALICE)
    assert caught.value.code is ErrorCode.SCHEMA_INACTIVE
    assert any('"rejected"' in r.getMessage() and "SCHEMA_INACTIVE" in r.getMessage() for r in caplog.records)


def test_a_threads_reader_connection_is_released_when_the_thread_ends(tmp_path) -> None:  # type: ignore[no-untyped-def]
    import gc

    from aduku.storage.sqlite import SqliteStorage

    storage = SqliteStorage(str(tmp_path / "t.db"))
    service = make_service(storage)
    service.append("d1", [put("/name", "x")], contributor=ALICE)

    def read_once() -> None:
        service.get("d1")
        with storage.read() as tx:
            tx.last_seq("d1")

    mine = len(storage._reader_connections)  # this thread's own reader, from the append above
    for _ in range(5):
        assert run_threads([read_once]) == []
    gc.collect()
    assert len(storage._reader_connections) == mine
    storage.close()


def test_a_retry_takes_no_write_transaction_and_its_effects_are_worked_out_once(
    monkeypatch: pytest.MonkeyPatch, backend: Storage
) -> None:
    from tests.integration.test_resilience import Counting

    counting = Counting(backend)
    service = _validated_service(counting, SequentialIds("a"))
    for index in range(5):
        service.append("d1", [put("/age", index)], contributor=ALICE)
    service.append("d1", [put("/name", "x")], contributor=ALICE, request_key="K")
    writes = counting.writes
    folds: list[int] = []
    import aduku.engine.model_errors as engine

    real = engine.fold_entries
    monkeypatch.setattr(engine, "fold_entries", lambda *a, **k: (folds.append(1), real(*a, **k))[1])
    first = service.append("d1", [put("/name", "x")], contributor=ALICE, request_key="K")
    worked = len(folds)
    second = service.append("d1", [put("/name", "x")], contributor=ALICE, request_key="K")
    assert first.duplicate and second.duplicate and counting.writes == writes
    assert worked > 0 and len(folds) == worked  # the second retry used what the first worked out
    assert first.model_errors_added == second.model_errors_added


def test_a_reader_finalizer_does_not_keep_the_storage_alive(tmp_path) -> None:  # type: ignore[no-untyped-def]
    import gc
    import weakref

    from aduku.storage.sqlite import SqliteStorage

    storage = SqliteStorage(str(tmp_path / "t.db"))
    with storage.read() as tx:
        tx.last_seq("d1")
    watcher = weakref.ref(storage)
    del tx, storage
    gc.collect()
    assert watcher() is None


def test_closing_a_storage_while_a_read_is_open_does_not_replace_the_result_with_a_raw_sqlite_error(tmp_path) -> None:  # type: ignore[no-untyped-def]
    from aduku.storage.sqlite import SqliteStorage

    storage = SqliteStorage(str(tmp_path / "t.db"))
    with storage.read() as tx:
        assert tx.last_seq("d1") == 0
        storage.close()  # the reader's connection is closed under the read; leaving the block must not raise
