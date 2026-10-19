"""Feature 003, research R-2 and R-9: the derived store, scheduler runs and leases, on both backends (FR-004)."""

import pathlib
import sqlite3
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from aduku.model import DerivedRecord, SchedulerRun
from aduku.storage.base import Storage
from aduku.storage.sqlite import SqliteStorage

T0 = datetime(2026, 10, 19, 12, 0, tzinfo=UTC)


def record(doc: str = "d1", field: str = "/creditScore", **changes: object) -> DerivedRecord:
    base = DerivedRecord(
        doc=doc,
        field=field,
        value="712",
        has_value=True,
        as_of=T0,
        input_fingerprint="f" * 64,
        hook="fx.score@1",
        shape="s" * 64,
        schema_version=1,
        status="ok",
        attempts=1,
        last_attempt=T0,
    )
    return replace(base, **changes)  # type: ignore[arg-type]


def test_a_record_is_stored_and_read_back_exactly(backend: Storage) -> None:
    with backend.transaction() as tx:
        tx.put_derived([record(), record(field="/fxRate", value="0.92d0", status="stale", error="timeout", attempts=3)])
    with backend.read() as tx:
        found = tx.get_derived("d1")
    assert [r.field for r in found] == ["/creditScore", "/fxRate"]
    assert found[0] == record()
    assert (found[1].status, found[1].error, found[1].attempts) == ("stale", "timeout", 3)
    with backend.read() as tx:
        assert tx.get_derived("nobody") == []


def test_a_new_write_replaces_the_record(backend: Storage) -> None:
    with backend.transaction() as tx:
        tx.put_derived([record()])
        tx.put_derived([record(value="800", as_of=T0 + timedelta(hours=1))])
    with backend.read() as tx:
        (only,) = tx.get_derived("d1")
    assert (only.value, only.as_of) == ("800", T0 + timedelta(hours=1))


def test_a_record_without_a_value_yet_and_an_invalidated_one_round_trip(backend: Storage) -> None:
    failed = record(value=None, has_value=False, as_of=None, status="error", error="breaker_open", invalidated=True)
    with backend.transaction() as tx:
        tx.put_derived([failed])
    with backend.read() as tx:
        assert tx.get_derived("d1") == [failed]


def test_records_are_listed_across_documents_by_field(backend: Storage) -> None:
    with backend.transaction() as tx:
        tx.put_derived([record("d1"), record("d2"), record("d2", "/fxRate"), record("d3", "/fxRate")])
    with backend.read() as tx:
        assert [(r.doc, r.field) for r in tx.list_derived(["/fxRate"])] == [("d2", "/fxRate"), ("d3", "/fxRate")]
        assert [(r.doc, r.field) for r in tx.list_derived(["/creditScore", "/fxRate"], ["d2", "d9"])] == [
            ("d2", "/creditScore"),
            ("d2", "/fxRate"),
        ]
        assert tx.list_derived([]) == []


def test_marking_out_of_date_counts_the_matching_records_and_is_repeatable(backend: Storage) -> None:
    with backend.transaction() as tx:
        tx.put_derived([record("d1"), record("d2"), record("d2", "/fxRate")])
    with backend.transaction() as tx:
        assert tx.mark_invalidated(["/creditScore"], ["d2"]) == 1
        assert tx.mark_invalidated(["/creditScore"], ["d2"]) == 1  # again: nothing further changes
        assert tx.mark_invalidated(["/creditScore"], ["d9"]) == 0  # no such record
    with backend.read() as tx:
        flags = {(r.doc, r.field): r.invalidated for r in tx.list_derived(["/creditScore", "/fxRate"])}
    assert flags == {("d1", "/creditScore"): False, ("d2", "/creditScore"): True, ("d2", "/fxRate"): False}
    with backend.transaction() as tx:
        assert tx.mark_invalidated(["/creditScore"]) == 2  # no documents named: every record of the field


def test_records_can_be_deleted_by_document_and_field(backend: Storage) -> None:
    with backend.transaction() as tx:
        tx.put_derived([record("d1"), record("d1", "/fxRate"), record("d2")])
    with backend.transaction() as tx:
        assert tx.delete_derived(["d1"], ["/fxRate"]) == 1
        assert tx.delete_derived(["d2"]) == 1
    with backend.read() as tx:
        assert [(r.doc, r.field) for r in tx.list_derived(["/creditScore", "/fxRate"])] == [("d1", "/creditScore")]
    with backend.transaction() as tx:
        assert tx.delete_derived() == 1


def test_a_rolled_back_transaction_leaves_the_store_unchanged(backend: Storage) -> None:
    with pytest.raises(RuntimeError):
        with backend.transaction() as tx:
            tx.put_derived([record()])
            raise RuntimeError("abort")
    with backend.read() as tx:
        assert tx.get_derived("d1") == []


def test_a_read_transaction_refuses_every_write(backend: Storage) -> None:
    with backend.read() as tx:
        for call in (
            lambda: tx.put_derived([record()]),
            lambda: tx.mark_invalidated(["/creditScore"]),
            lambda: tx.delete_derived(),
            lambda: tx.append_run(SchedulerRun("r", "fx.score@1", T0, T0)),
            lambda: tx.take_lease("fx.score@1", "me", T0, T0),
        ):
            with pytest.raises(Exception):  # noqa: B017 - the two backends refuse in their own way
                call()


def test_scheduler_runs_are_listed_oldest_first_by_source(backend: Storage) -> None:
    runs = [
        SchedulerRun(f"r{n}", "fx.rate@1" if n % 2 else "fx.score@1", T0 + timedelta(minutes=n), T0 + timedelta(minutes=n),
                     attempted=n, succeeded=n - 1, failed=1, skipped=0, calls=1, reason="time limit" if n == 4 else None)
        for n in range(1, 6)
    ]  # fmt: skip
    with backend.transaction() as tx:
        for run in runs:
            tx.append_run(run)
    with backend.read() as tx:
        assert [r.run_id for r in tx.list_runs("fx.score@1")] == ["r2", "r4"]
        assert tx.list_runs("fx.score@1")[1].reason == "time limit"
        assert [r.run_id for r in tx.list_runs("fx.rate@1", limit=2)] == ["r3", "r5"]  # the newest two
        assert tx.list_runs("fx.nobody@1") == []


def test_a_lease_is_held_until_it_expires_or_is_released(backend: Storage) -> None:
    hook = "fx.score@1"
    with backend.transaction() as tx:
        assert tx.take_lease(hook, "run-a", T0 + timedelta(minutes=15), T0) is True
        assert tx.take_lease(hook, "run-b", T0 + timedelta(minutes=30), T0 + timedelta(minutes=1)) is False  # held
        assert tx.take_lease(hook, "run-a", T0 + timedelta(minutes=20), T0 + timedelta(minutes=1)) is True  # its own
        assert tx.renew_lease(hook, "run-a", T0 + timedelta(minutes=40)) is True
        assert tx.renew_lease(hook, "run-b", T0 + timedelta(minutes=50)) is False  # not its lease
        assert tx.take_lease(hook, "run-b", T0 + timedelta(hours=2), T0 + timedelta(minutes=39)) is False
        assert tx.take_lease(hook, "run-b", T0 + timedelta(hours=2), T0 + timedelta(minutes=41)) is True  # expired
        tx.release_lease(hook, "run-a")  # not the holder: no effect
        assert tx.take_lease(hook, "run-c", T0 + timedelta(hours=3), T0 + timedelta(minutes=42)) is False
        tx.release_lease(hook, "run-b")
        assert tx.take_lease(hook, "run-c", T0 + timedelta(hours=3), T0 + timedelta(minutes=42)) is True
        assert tx.take_lease("fx.rate@1", "run-d", T0 + timedelta(hours=1), T0) is True  # leases are per source


def test_a_database_made_by_feature_002_opens_and_gains_the_tables(tmp_path: pathlib.Path) -> None:
    path = str(tmp_path / "old.db")
    first = SqliteStorage(path)
    with first.transaction() as tx:
        tx.put_derived([record()])
    first.close()
    raw = sqlite3.connect(path)
    for table in ("derived_values", "scheduler_runs", "scheduler_leases"):
        raw.execute(f"DROP TABLE {table}")  # what a feature 002 database looks like
    raw.commit()
    raw.close()
    reopened = SqliteStorage(path)
    with reopened.transaction() as tx:
        assert tx.get_derived("d1") == []
        tx.put_derived([record()])
    reopened.close()
    again = SqliteStorage(path)  # a second open is a no-op
    with again.read() as tx:
        assert tx.get_derived("d1") == [record()]
    again.close()
