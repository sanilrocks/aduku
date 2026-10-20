"""Feature 005 (FR-025 to FR-027): the storage side of a compaction, an expiry and the tables they keep, on both backends."""

import sqlite3
from datetime import UTC, datetime, timedelta
from itertools import count

import pytest

from aduku.behaviors.registry import BehaviorRegistry
from aduku.engine.compaction import plan
from aduku.engine.derive import prepare
from aduku.model import Entry
from aduku.schema.loader import load_schema
from aduku.storage.base import ConstraintError, EffectRecord, RequestRecord, Storage
from aduku.storage.sqlite import SqliteStorage
from tests.helpers import T0, make_entry
from tests.service_helpers import schema_text

BEHAVIORS = BehaviorRegistry.with_builtins()
SCHEMA = load_schema(
    schema_text(fields='"/name": {type: string, merge: lww}, "/age": {type: int, merge: max}'), BEHAVIORS
)
NOW = datetime(2026, 2, 1, tzinfo=UTC)


def put_history(storage: Storage) -> list[Entry]:
    rows = [
        make_entry("d", 1, request_id="r1", tier="backfill", path="/name", value="a", idem_key="K1", tx_time=T0),
        make_entry("d", 2, request_id="r1", tier="backfill", path="/age", value=3, tx_time=T0),
        make_entry("d", 3, request_id="r2", tier="user", path="/name", value="b", tx_time=T0 + timedelta(seconds=5)),
        make_entry("d", 4, request_id="r3", tier="user", path="/name", value="c", tx_time=T0 + timedelta(seconds=9)),
    ]
    with storage.transaction() as tx:
        for rid in ("r1", "r2", "r3"):
            tx.append_request(RequestRecord(rid, "d", "c"), [e for e in rows if e.request_id == rid])
    return rows


def make_plan(rows: list[Entry], through: int, cid: str = "c1", prefix: str = "s"):  # type: ignore[no-untyped-def]
    ids = count(1)
    result = plan(
        rows,
        SCHEMA,
        BEHAVIORS,
        prepare(SCHEMA, BEHAVIORS),
        doc="d",
        through_seq=through,
        compaction_id=cid,
        new_id=lambda: f"{prefix}{next(ids)}",
        created=NOW,
        policy="through_seq",
        base_errors="[]",
        base_skipped=(2,),
    )
    assert result is not None
    return result


def test_a_compaction_swaps_the_range_for_synthetic_entries_and_keeps_the_rest(backend: Storage) -> None:
    rows = put_history(backend)
    result = make_plan(rows, 3)
    with backend.transaction() as tx:
        tx.append_effects([EffectRecord("d", "/f", "fp", "h@1", "value", "1", None, 1, T0)])
        epoch = tx.epoch("d")
    with backend.transaction() as tx:
        tx.apply_compaction(result)
    with backend.read() as tx:
        live = tx.entries("d")
        assert [(e.seq, e.op, e.tier, e.synthetic) for e in live] == [
            (2, "layer", "backfill", "c1"),
            (3, "layer", "user", "c1"),
            (4, "set", "user", None),
        ]
        assert tx.last_seq("d") == 4 and tx.epoch("d") == epoch + 1
        assert [e.seq for e in tx.entries("d", 2)] == [3, 4] and [e.seq for e in tx.entries("d", 0, until_seq=3)] == [
            2,
            3,
        ]
        assert [e.id for e in tx.archived_entries("d")] == ["e-d-1", "e-d-2", "e-d-3"]
        assert [e.id for e in tx.archived_entries("d", through_seq=2)] == ["e-d-1", "e-d-2"]
        assert [e.id for e in tx.archive_of("c1")] == ["e-d-1", "e-d-2", "e-d-3"]
        (record,) = tx.compactions("d")
        assert (record.id, record.watermark_seq, record.archived, record.base_skipped) == ("c1", 3, True, (2,))
        assert record.fold_fingerprint == SCHEMA.fold_fingerprint and record.first_tx_time == T0
        assert [(s.source_id, s.seq, s.tier, s.idem_key) for s in tx.sources_of("c1")][0] == (
            "e-d-1",
            1,
            "backfill",
            "K1",
        )
        assert tx.request_entries("r1") == [("e-d-1", 1), ("e-d-2", 2)]  # the entries are gone; the answer is not
        assert tx.request_entries("r3") == [("e-d-4", 4)]
        assert tx.idem_key_seen("d", "K1") and not tx.idem_key_seen("d", "K2")
        assert tx.entries_of_request("r1") == []  # nothing live
        assert len(tx.list_effects("d")) == 1  # the effect log is not touched


def test_the_seq_of_the_last_entry_survives_a_compaction_of_everything(backend: Storage) -> None:
    rows = put_history(backend)
    with backend.transaction() as tx:
        tx.apply_compaction(make_plan(rows, 4))
    with backend.read() as tx:
        assert tx.last_seq("d") == 4 and [e.seq for e in tx.entries("d")] == [2, 4]
    with backend.transaction() as tx:  # and the next request continues at seq 5
        tx.append_request(RequestRecord("r9", "d", "c"), [make_entry("d", 5, request_id="r9", tier="user")])
    with backend.read() as tx:
        assert tx.last_seq("d") == 5


def test_a_failure_in_the_middle_of_a_compaction_changes_nothing(backend: Storage) -> None:
    rows = put_history(backend)
    bad = make_plan(rows, 3)
    clash = rows[3].id  # a synthetic entry whose id is already a live entry's: fails after the swap has begun
    broken = type(bad)(
        bad.record,
        bad.replaced,
        (bad.synthetic[0], type(bad.synthetic[1])(**{**bad.synthetic[1].__dict__, "id": clash})),
        bad.sources,
        bad.request_index,
        bad.retired_keys,
    )
    with pytest.raises((ConstraintError, sqlite3.Error)):
        with backend.transaction() as tx:
            tx.apply_compaction(broken)
    with backend.read() as tx:
        assert [(e.seq, e.op) for e in tx.entries("d")] == [(1, "set"), (2, "set"), (3, "set"), (4, "set")]
        assert tx.archived_entries("d") == [] and tx.compactions("d") == [] and tx.epoch("d") == 0
        assert tx.request_entries("r1") == [("e-d-1", 1), ("e-d-2", 2)] and not tx.idem_key_seen("d", "KX")


def test_a_rolled_back_transaction_undoes_a_whole_compaction(backend: Storage) -> None:
    rows = put_history(backend)
    with pytest.raises(RuntimeError):
        with backend.transaction() as tx:
            tx.apply_compaction(make_plan(rows, 3))
            raise RuntimeError("later step failed")
    with backend.read() as tx:
        assert len(tx.entries("d")) == 4 and tx.archived_entries("d") == [] and tx.compactions("d") == []


def test_expiry_removes_only_the_archive_and_is_recorded(backend: Storage) -> None:
    rows = put_history(backend)
    with backend.transaction() as tx:
        tx.apply_compaction(make_plan(rows, 2))
    with backend.transaction() as tx:
        tx.apply_compaction(make_plan(_live(backend), 4, "c2", "t"))
    with backend.transaction() as tx:
        assert tx.expire_archive("d", 2, NOW) == 2  # only the first compaction's originals (watermark 2)
    with backend.read() as tx:
        assert [e.id for e in tx.archived_entries("d")] == ["s1", "e-d-3", "e-d-4"]  # the second compaction's archive
        assert [c.archived for c in tx.compactions("d")] == [False, True]
        assert tx.archive_of("c1") == [] and [e.id for e in tx.archive_of("c2")] == ["s1", "e-d-3", "e-d-4"]
        assert len(tx.sources_of("c1")) == 2 and tx.request_entries("r1") == [("e-d-1", 1), ("e-d-2", 2)]
        assert tx.idem_key_seen("d", "K1")
    with backend.transaction() as tx:
        assert tx.expire_archive("d", 2, NOW) == 0  # nothing more to expire


def _live(storage: Storage) -> list[Entry]:
    with storage.read() as tx:
        return tx.entries("d")


def test_the_new_tables_refuse_ordinary_updates_and_deletes(tmp_path) -> None:  # noqa: ANN001
    storage = SqliteStorage(str(tmp_path / "t.db"))
    rows = put_history(storage)
    with storage.transaction() as tx:
        tx.apply_compaction(make_plan(rows, 3))
    storage.close()
    connection = sqlite3.connect(str(tmp_path / "t.db"))
    for statement in (
        "DELETE FROM archive_entries",
        "UPDATE archive_entries SET path = '/x'",
        "DELETE FROM compaction_sources",
        "UPDATE compaction_sources SET seq = 0",
    ):
        with pytest.raises(sqlite3.DatabaseError, match="append-only"):
            connection.execute(statement)
    connection.close()
