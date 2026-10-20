"""Feature 005 (edge case: very large documents, FR-027, FR-034): a failure at any step of a compaction, an expiry or a purge,
injected into the storage underneath the service, leaves the ledger and every store as they were, and the retry succeeds."""

import pytest

from aduku.model import Identity, PurgeScope
from aduku.storage.base import Storage
from aduku.storage.memory import MemoryStorage
from aduku.storage.sqlite import SqliteStorage
from tests.service_helpers import ALICE, FIELDS, JOB, make_service, put, schema_text
from tests.snapshot_helpers import FaultyStorage

SCHEMA = schema_text(fields=FIELDS).replace("tiers:", "purge_by: [dpo], roles: [dpo], tiers:")
DPO = Identity("officer", ("admin",), ("dpo",))


def state(storage: Storage):  # type: ignore[no-untyped-def]
    if isinstance(storage, SqliteStorage):
        return [
            [tuple(row) for row in storage.connection.execute(f"SELECT * FROM {table} ORDER BY 1, 2")]
            for table in (
                "entries",
                "archive_entries",
                "compactions",
                "compaction_sources",
                "layers",
                "views",
                "purge_log",
                "doc_epoch",
            )
        ]
    assert isinstance(storage, MemoryStorage)
    inner = storage._state  # noqa: SLF001
    epochs = {key: value for key, value in inner.epochs.items() if value}
    return [
        dict(inner.entries),
        {k: v for k, v in inner.archive.items() if v},  # an emptied list and a missing key are the same state
        {k: v for k, v in inner.compactions.items() if v},
        dict(inner.layers),
        dict(inner.views),
        list(inner.purges),
        epochs,
    ]


def seeded(storage: Storage):  # type: ignore[no-untyped-def]
    db = make_service(storage, schema=SCHEMA)
    for i in range(4):
        db.append("d", [put("/name", f"n{i}"), put("/age", i)], contributor=ALICE)
        db.append("d", [put("/tags", (f"t{i}",), "backfill")], contributor=JOB)
    return db


@pytest.mark.parametrize("step", ["apply_compaction", "drop_layers", "put_view"])
def test_a_compaction_that_fails_at_any_step_changes_nothing_and_can_be_retried(backend: Storage, step: str) -> None:
    flaky = FaultyStorage(backend, [step], nth=1)
    flaky.armed = False
    db = seeded(flaky)  # type: ignore[arg-type]
    before_entries = db.list_entries("d")
    flaky.calls, flaky.armed = 0, True
    mark = state(backend)
    with pytest.raises(FaultyStorage.Injected):
        db.compact("d", through_seq=db.last_seq("d"))
    flaky.armed = False
    assert state(backend) == mark and db.list_entries("d") == before_entries
    assert db.compact("d", through_seq=db.last_seq("d")).synthetic == 2  # the retry goes through


@pytest.mark.parametrize("step", ["apply_purge", "append_purge_log"])
def test_a_purge_that_fails_at_any_step_changes_nothing_and_can_be_retried(backend: Storage, step: str) -> None:
    flaky = FaultyStorage(backend, [step], nth=1)
    flaky.armed = False
    db = seeded(flaky)  # type: ignore[arg-type]
    flaky.calls, flaky.armed = 0, True
    mark = state(backend)
    with pytest.raises(FaultyStorage.Injected):
        db.purge("d", PurgeScope(path="/name"), reason="ticket", caller=DPO)
    assert state(backend) == mark and db.purge_log("d") == []
    flaky.armed = False
    assert db.purge("d", PurgeScope(path="/name"), reason="ticket", caller=DPO).entries_erased == 4


def test_a_layer_that_cannot_be_saved_does_not_fail_a_compaction(backend: Storage) -> None:
    flaky = FaultyStorage(backend, ["put_layers"], nth=1)
    db = seeded(flaky)  # type: ignore[arg-type]
    flaky.calls = 0
    assert db.compact("d", through_seq=db.last_seq("d")).synthetic == 2  # layers are a cache (FR-005)


def test_an_expiry_that_fails_changes_nothing(backend: Storage) -> None:
    flaky = FaultyStorage(backend, ["expire_archive"], nth=1)
    flaky.armed = False
    db = seeded(flaky)  # type: ignore[arg-type]
    db.compact("d", through_seq=db.last_seq("d"))
    mark = state(backend)
    flaky.calls, flaky.armed = 0, True
    with pytest.raises(FaultyStorage.Injected):
        db.expire_archive("d", through_seq=db.last_seq("d"))
    assert state(backend) == mark
    flaky.armed = False
    assert db.expire_archive("d", through_seq=db.last_seq("d")).expired > 0
