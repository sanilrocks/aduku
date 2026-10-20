"""Feature 005 (FR-031, FR-033, FR-034): `apply_purge` on both backends: one atomic step, a log that only grows."""

import sqlite3
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from aduku.ion_codec import FrozenStruct
from aduku.model import DerivedRecord, EffectRecord, Entry, PurgeApplication, PurgeRecord
from aduku.storage.base import AuditRow, RequestRecord, Storage, ViewRow
from aduku.storage.sqlite import SqliteStorage
from tests.helpers import T0, make_entry

NOW = datetime(2026, 2, 1, tzinfo=UTC)
MARK = "secret-marker"


def seed(storage: Storage) -> list[Entry]:
    rows = [
        make_entry(
            "d",
            1,
            request_id="r1",
            path="/name",
            value=MARK,
            meta=FrozenStruct({"t": MARK}),
            valid_time=T0,
            request_key="RK",
            request_hash="h1",
            idem_key="K1",
        ),
        make_entry("d", 2, request_id="r1", path="/age", value=3, request_key="RK", request_hash="h1"),
        make_entry("d", 3, request_id="r2", path="/age", value=4),
    ]
    with storage.transaction() as tx:
        tx.append_request(RequestRecord("r1", "d", "c", "RK", "h1"), rows[:2])
        tx.append_request(RequestRecord("r2", "d", "c"), rows[2:])
        tx.put_view(ViewRow("d", 3, 1, "fp", "{}", "[]", "{}"))
        tx.put_layers(
            [__import__("aduku.storage.base", fromlist=["LayerRow"]).LayerRow("d", "user", 3, 1, "fp", 0, "[]")]
        )
        tx.append_effects(
            [
                EffectRecord("d", "/shout", "fp", "h@1", "value", f'"{MARK}"', None, 1, T0),
                EffectRecord("d", "/other", "fp", "h@1", "value", "1", None, 1, T0),
            ]
        )
        tx.put_derived([DerivedRecord("d", "/score", MARK, True, T0, "fp", "h@1", "int", 1, "ok", None, 1, T0, False)])
    return rows


def application(rows: list[Entry]) -> PurgeApplication:
    erased = replace(rows[0], value=None, meta=None, valid_time=None, request_hash=None, purged=True)
    sibling = replace(rows[1], request_hash=None)
    return PurgeApplication("d", (erased, sibling), (), ("r1",), ("/shout", "/score"))


def record() -> PurgeRecord:
    return PurgeRecord("p1", "d", "path=/name", "ticket 7", "dpo", NOW, "{}")


def test_a_purge_erases_values_everywhere_in_one_step_and_logs_it(backend: Storage) -> None:
    rows = seed(backend)
    with backend.transaction() as tx:
        counts = tx.apply_purge(application(rows))
        tx.append_purge_log(record())
    assert counts == (1, 1)
    with backend.read() as tx:
        first, second, third = tx.entries("d")
        assert (first.value, first.meta, first.valid_time, first.request_hash, first.purged) == (
            None,
            None,
            None,
            None,
            True,
        )
        assert (first.idem_key, first.request_key, first.tier, first.path, first.seq, first.id) == (
            "K1",
            "RK",
            "user",
            "/name",
            1,
            "e-d-1",
        )
        assert second.request_hash is None and not second.purged and third.value == 4
        assert tx.find_request("d", "c", "RK").request_hash is None  # type: ignore[union-attr]
        assert tx.get_view("d") is None and tx.get_layers("d") == [] and tx.epoch("d") == 1
        assert [r.field for r in tx.list_effects("d")] == [
            "/other"
        ]  # only the fields downstream of the erased path went
        assert tx.get_derived("d") == []
        assert [(p.id, p.reason, p.actor) for p in tx.purge_log("d")] == [("p1", "ticket 7", "dpo")]


def test_a_failure_in_the_middle_changes_nothing(backend: Storage) -> None:
    rows = seed(backend)
    broken = replace(application(rows), live=(*application(rows).live, replace(rows[2], id="no-such-entry")))
    with pytest.raises((RuntimeError, sqlite3.Error, Exception)):
        with backend.transaction() as tx:
            tx.apply_purge(broken)
            raise RuntimeError("a later step failed")
    with backend.read() as tx:
        assert tx.entries("d")[0].value == MARK and tx.get_view("d") is not None and len(tx.get_layers("d")) == 1
        assert len(tx.list_effects("d")) == 2 and len(tx.get_derived("d")) == 1 and tx.epoch("d") == 0
        assert tx.find_request("d", "c", "RK").request_hash == "h1"  # type: ignore[union-attr]
        assert tx.purge_log("d") == []


def test_archive_rows_are_erased_too(backend: Storage) -> None:
    from aduku.behaviors.registry import BehaviorRegistry
    from aduku.engine.compaction import plan
    from aduku.engine.derive import prepare
    from aduku.schema.loader import load_schema
    from tests.service_helpers import schema_text

    rows = seed(backend)
    behaviors = BehaviorRegistry.with_builtins()
    schema = load_schema(
        schema_text(fields='"/name": {type: string, merge: lww}, "/age": {type: int, merge: max}'), behaviors
    )
    result = plan(rows, schema, behaviors, prepare(schema, behaviors), doc="d", through_seq=3, compaction_id="c1", new_id=lambda: "s1",
                  created=NOW, policy="p", base_errors="[]", base_skipped=())  # fmt: skip
    assert result is not None
    with backend.transaction() as tx:
        tx.apply_compaction(result)
    archived = tx_archive = None
    with backend.read() as tx:
        archived = tx.archived_entries("d")
    erased = replace(archived[0], value=None, meta=None, valid_time=None, request_hash=None, purged=True)
    with backend.transaction() as tx:
        tx.apply_purge(PurgeApplication("d", (), (erased,), ("r1",), ()))
    with backend.read() as tx:
        assert tx.archived_entries("d")[0].purged and tx.archived_entries("d")[0].value is None
        assert tx.archived_entries("d")[0].meta is None
    _ = tx_archive


def test_the_audit_log_can_have_attempted_values_scrubbed(backend: Storage) -> None:
    audit = backend.audit_backend()
    audit.insert(
        AuditRow("d", "c", ("user",), False, f'[{{tier:"user",path:"/name",op:"set",value:"{MARK}"}}]', "[]", 1, T0)
    )
    (row,) = audit.rows("d")
    audit.update_attempted(row.id, '[{tier:"user",path:"/name",op:"set",purged:true}]')  # type: ignore[arg-type]
    assert MARK not in audit.rows("d")[0].attempted


def test_the_purge_log_only_grows(tmp_path) -> None:  # noqa: ANN001
    storage = SqliteStorage(str(tmp_path / "t.db"))
    with storage.transaction() as tx:
        tx.append_purge_log(record())
    storage.close()
    connection = sqlite3.connect(str(tmp_path / "t.db"))
    for statement in ("UPDATE purge_log SET reason = 'x'", "DELETE FROM purge_log"):
        with pytest.raises(sqlite3.DatabaseError, match="append-only"):
            connection.execute(statement)
    connection.close()
    _ = timedelta
