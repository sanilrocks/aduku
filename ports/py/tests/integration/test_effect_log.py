"""Feature 002, research R-9: the effect log is append-only, kept apart from the ledger, and prunable without effect."""

from datetime import UTC, datetime, timedelta

import pytest

from aduku.storage.base import EffectRecord, Storage
from aduku.storage.sqlite import SqliteStorage

T0 = datetime(2026, 10, 19, 12, 0, 0, tzinfo=UTC)


def record(
    doc: str = "d1", field: str = "/taxId", fingerprint: str = "f1", hook: str = "fx.echo@1", **kw: object
) -> EffectRecord:
    base: dict[str, object] = dict(outcome="value", value='"tax-US"', error=None, attempts=1, recorded_at=T0)
    base.update(kw)
    return EffectRecord(doc=doc, field=field, fingerprint=fingerprint, hook=hook, **base)  # type: ignore[arg-type]


def test_appending_assigns_increasing_sequence_numbers_across_documents(backend: Storage) -> None:
    with backend.transaction() as tx:
        first = tx.append_effects([record("d1"), record("d2", fingerprint="g")])
        second = tx.append_effects([record("d1", fingerprint="h")])
    assert first == [1, 2] and second == [3]


def test_the_latest_record_per_key_is_the_one_in_force(backend: Storage) -> None:
    with backend.transaction() as tx:
        tx.append_effects([record(value='"old"'), record(fingerprint="other"), record(value='"new"')])
    with backend.read() as tx:
        latest = {(r.fingerprint): r for r in tx.latest_effects("d1")}
    assert set(latest) == {"f1", "other"} and latest["f1"].value == '"new"' and latest["f1"].effect_seq == 3


def test_latest_effects_belong_to_one_document(backend: Storage) -> None:
    with backend.transaction() as tx:
        tx.append_effects([record("d1"), record("d2")])
    with backend.read() as tx:
        assert [r.doc for r in tx.latest_effects("d1")] == ["d1"] and tx.latest_effects("nobody") == []


def test_effects_seq_is_the_greatest_for_the_document(backend: Storage) -> None:
    with backend.transaction() as tx:
        assert tx.effects_seq("d1") == 0
        tx.append_effects([record("d1"), record("d2"), record("d1", fingerprint="z")])
    with backend.read() as tx:
        assert tx.effects_seq("d1") == 3 and tx.effects_seq("d2") == 2 and tx.effects_seq("none") == 0


def test_a_failure_is_recorded_with_its_kind_and_no_value(backend: Storage) -> None:
    failed = record(outcome="failure", value=None, error="timeout", attempts=3)
    with backend.transaction() as tx:
        tx.append_effects([failed])
    with backend.read() as tx:
        (got,) = tx.latest_effects("d1")
    assert (got.outcome, got.value, got.error, got.attempts) == ("failure", None, "timeout", 3)


def test_records_written_in_a_transaction_that_rolls_back_are_not_visible(backend: Storage) -> None:
    with pytest.raises(RuntimeError), backend.transaction() as tx:
        tx.append_effects([record()])
        raise RuntimeError("roll back")
    with backend.read() as tx:
        assert tx.latest_effects("d1") == [] and tx.effects_seq("d1") == 0
    with backend.transaction() as tx:
        assert tx.append_effects([record()]) == [1]  # and the number was not used up (memory) or reused (sqlite)


def test_list_effects_is_oldest_first_and_can_be_limited_and_filtered(backend: Storage) -> None:
    with backend.transaction() as tx:
        tx.append_effects([record(field="/a"), record(field="/b"), record(field="/a", fingerprint="x")])
    with backend.read() as tx:
        assert [r.effect_seq for r in tx.list_effects("d1")] == [1, 2, 3]
        assert [r.effect_seq for r in tx.list_effects("d1", "/a")] == [1, 3]
        assert [r.effect_seq for r in tx.list_effects("d1", limit=2)] == [2, 3]  # the newest two, oldest first


def test_pruning_keeps_the_record_in_force_and_at_most_five_superseded(backend: Storage) -> None:
    with backend.transaction() as tx:
        tx.append_effects([record(value=f'"v{n}"') for n in range(9)])  # same key: eight superseded, one in force
        removed = tx.prune_effects("d1", now=T0)
    assert removed == 3
    with backend.read() as tx:
        kept = [r.effect_seq for r in tx.list_effects("d1")]
        assert kept == [4, 5, 6, 7, 8, 9] and tx.latest_effects("d1")[0].effect_seq == 9


def test_pruning_drops_superseded_records_older_than_ninety_days_but_never_the_one_in_force(backend: Storage) -> None:
    old = T0 - timedelta(days=91)
    with backend.transaction() as tx:
        tx.append_effects(
            [record(recorded_at=old), record(recorded_at=old), record(recorded_at=T0 - timedelta(days=5))]
        )
        tx.append_effects([record(fingerprint="lonely", recorded_at=old)])  # old, but the only one: in force
        removed = tx.prune_effects("d1", now=T0)
    assert removed == 2
    with backend.read() as tx:
        assert sorted(r.effect_seq for r in tx.list_effects("d1")) == [3, 4]


def test_pruning_without_a_document_covers_all_documents(backend: Storage) -> None:
    old = T0 - timedelta(days=100)
    with backend.transaction() as tx:
        tx.append_effects([record("a", recorded_at=old), record("a"), record("b", recorded_at=old), record("b")])
        assert tx.prune_effects(None, now=T0) == 2


def test_a_record_cannot_be_changed_or_deleted_except_by_pruning(tmp_path) -> None:  # type: ignore[no-untyped-def]
    import sqlite3

    storage = SqliteStorage(str(tmp_path / "e.db"))
    with storage.transaction() as tx:
        tx.append_effects([record()])
    with pytest.raises(sqlite3.Error, match="append-only"):
        storage.connection.execute("UPDATE effects SET value = '\"x\"'")
    with pytest.raises(sqlite3.Error, match="append-only"):
        storage.connection.execute("DELETE FROM effects")
    storage.close()


def test_the_effect_log_is_separate_from_the_ledger(backend: Storage) -> None:
    from tests.helpers import make_entry

    with backend.transaction() as tx:
        tx.append_effects([record()])
        assert tx.last_seq("d1") == 0 and tx.entries("d1") == []
    with backend.transaction() as tx:
        from aduku.storage.base import RequestRecord

        tx.append_request(RequestRecord("r1", "d1", "alice"), [make_entry()])
    with backend.read() as tx:
        assert tx.effects_seq("d1") == 1 and tx.last_seq("d1") == 1


def test_a_view_remembers_the_effects_it_was_derived_with(backend: Storage) -> None:
    from aduku.storage.base import ViewRow

    view = ViewRow("d1", 3, 1, "fp", "{}", "[]", "{}", effects_seq=7)
    with backend.transaction() as tx:
        tx.put_view(view)
    with backend.read() as tx:
        got = tx.get_view("d1")
    assert got is not None and got.effects_seq == 7
