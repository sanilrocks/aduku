"""Regressions for the feature 006 code review: construction, first start, cost of reads, and opening a database."""

from __future__ import annotations

import sqlite3
import threading
import time

from aduku.storage.base import Storage
from aduku.storage.memory import MemoryStorage
from aduku.storage.sqlite import SCHEMA_VERSION, SqliteStorage
from tests.integration.test_schema_activation_cost import fill
from tests.schema_helpers import Env, field_text, versions_text
from tests.service_helpers import ALICE, put
from tests.snapshot_helpers import CountingStorage

BASE = field_text()
V2 = BASE.replace('"/age": {type: int, merge: max}', '"/age": {type: string, merge: lww}')
TEXTS = versions_text(BASE, V2)


def test_left_out_is_counted_in_linear_time() -> None:
    storage = MemoryStorage()
    fill(storage, 20_000)
    db = Env(storage, *TEXTS).service(active=1)
    started = time.perf_counter()
    report = db.plan_schema(2, docs=[f"doc-{n:06d}" for n in range(19_000)])  # many examined, many stored
    assert (report.examined, report.left_out) == (19_000, 1_000)
    assert time.perf_counter() - started < 8.0  # about 2 s; counting left_out quadratically takes about 15 s


def test_the_first_activation_can_ask_for_a_rebuild(backend: Storage) -> None:
    db = Env(backend, *TEXTS).service()  # nothing is active yet
    report = db.activate_schema(1, operator="alice", rebuild=True)
    assert (report.version, report.previous, report.changed) == (1, None, ()) and report.rebuild is not None


def test_two_processes_starting_on_an_empty_store_agree_on_what_the_first_stored(backend: Storage) -> None:
    from datetime import UTC, datetime

    from aduku.schema.registry import SchemaStateSnapshot, StateConflict
    from aduku.schema_state import StorageSchemaState

    env = Env(backend, *TEXTS)
    loser = env.registry(active=2)
    state = StorageSchemaState(backend, "t")
    real_read, real_generation, real_apply = state.read, state.generation, state.apply
    seen = {"reads": 0, "conflict": False}

    def stale_first_read() -> SchemaStateSnapshot:
        seen["reads"] += 1
        return SchemaStateSnapshot(0, None, {}, {}) if seen["reads"] == 1 else real_read()

    def apply(change, *, expect_generation, check=None):  # type: ignore[no-untyped-def]
        try:
            return real_apply(change, expect_generation=expect_generation, check=check)
        except StateConflict:
            seen["conflict"] = True
            raise

    state.read, state.apply = stale_first_read, apply  # type: ignore[method-assign]
    state.generation = lambda: 0 if seen["reads"] == 0 else real_generation()  # type: ignore[method-assign]
    env.service(active=1)  # the winner writes version 1 first
    loser.watch(lambda doc: set())
    loser.bind(state, lambda: datetime.now(UTC))
    assert seen["conflict"] and loser.active().version == 1  # it adopted the stored state instead of failing


def test_explain_uses_one_version_for_the_answer_and_the_shadow_reasons(backend: Storage) -> None:
    db = Env(backend, *TEXTS).service(active=1)
    db.append("d", [put("/name", "A")], contributor=ALICE)
    calls = []
    real = db.schemas.version_for
    db.schemas.version_for = lambda doc: (calls.append(doc), real(doc))[1]  # type: ignore[method-assign]
    db.explain("d", "/name")
    assert calls == ["d"]


def test_shadow_reasons_are_not_worked_out_again_while_the_ledger_is_unchanged(backend: Storage) -> None:
    counting = CountingStorage(backend)
    db = Env(counting, *TEXTS).service(active=1)  # type: ignore[arg-type]
    db.append("d", [put("/name", "A"), put("/age", 3)], contributor=ALICE)
    db.activate_schema(2, operator="alice", acknowledge_breaking=True)
    first = db.get("d")
    assert [s.reason for s in first.shadowed] == ["no_longer_conforms"]
    counting.reset()
    assert db.get("d").shadowed == first.shadowed and counting.entries_read == 0  # served from memory
    db.append("d", [put("/name", "B")], contributor=ALICE)  # the ledger moved: worked out again
    assert [s.seq for s in db.get("d").shadowed] == [2]


def test_a_batch_reads_the_pins_once_per_batch_not_once_per_document(backend: Storage) -> None:
    db = Env(backend, *TEXTS).service(active=1)
    for number in range(250):
        db.pin_document(f"p{number:03d}", 1, operator="alice")
    db.activate_schema(2, operator="alice", acknowledge_breaking=True)
    calls = {"n": 0}
    real = db.schemas.pins
    db.schemas.pins = lambda: (calls.__setitem__("n", calls["n"] + 1), real())[1]  # type: ignore[method-assign]
    db.migrate(operator="alice", batch=100)
    assert calls["n"] <= 1 + 3  # the names, and once for each of the three batches


def test_opening_a_current_database_runs_no_ddl_and_does_not_wait_for_a_writer(tmp_path) -> None:  # type: ignore[no-untyped-def]
    path = str(tmp_path / "t.db")
    SqliteStorage(path).close()
    assert sqlite3.connect(path).execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    holder = sqlite3.connect(path, isolation_level=None)
    holder.execute("BEGIN IMMEDIATE")  # a writer is in the middle of a transaction
    opened: list[SqliteStorage] = []
    thread = threading.Thread(target=lambda: opened.append(SqliteStorage(path)), daemon=True)
    thread.start()
    thread.join(timeout=5)
    holder.execute("ROLLBACK")
    assert opened, "opening a current database waited for the writer"
    opened[0].close()
