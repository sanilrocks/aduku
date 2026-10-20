"""Feature 006 (contracts/schema-state.md): the stored schema state, on both backends."""

import sqlite3
from datetime import UTC, datetime

import pytest

from aduku.model import StateChange
from aduku.storage.base import ConstraintError, Storage
from aduku.storage.sqlite import SqliteStorage

NOW = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)


def change(**kw: object) -> StateChange:
    base = dict(
        schema_id="t", action="activate", operator="alice", at=NOW, doc=None, from_version=None, to_version=1,
        change_class=None, note="{}", active=1, pin_doc=None, pin_version=None, served=(),
    )  # fmt: skip
    base.update(kw)
    return StateChange(**base)  # type: ignore[arg-type]


def test_a_store_with_no_state_reads_as_generation_zero_and_nothing_active(backend: Storage) -> None:
    with backend.read() as tx:
        assert tx.schema_generation("t") == 0 and tx.schema_state("t") == (0, None, {})
        assert tx.served_versions("t") == {} and tx.schema_history("t") == []


def test_one_change_writes_state_fingerprints_and_history_together_and_bumps_the_generation(backend: Storage) -> None:
    served = ((1, "f" * 64, '{top:"a"}'),)
    with backend.transaction() as tx:
        assert tx.apply_schema_change(change(served=served), None) == 1
        assert (
            tx.apply_schema_change(
                change(action="pin", doc="d1", pin_doc="d1", pin_version=1, active=None, note="{pin:1}"), 1
            )
            == 2
        )
    with backend.read() as tx:
        assert tx.schema_state("t") == (2, 1, {"d1": 1}) and tx.schema_generation("t") == 2
        assert tx.served_versions("t") == {1: ("f" * 64, '{top:"a"}')}
        history = tx.schema_history("t")
        assert [(h.action, h.operator, h.doc, h.to_version) for h in history] == [
            ("activate", "alice", None, 1),
            ("pin", "alice", "d1", 1),
        ]
        assert history[0].at == NOW and history[1].id > history[0].id and history[1].note == "{pin:1}"


def test_an_unpin_removes_the_pin_and_a_second_activation_replaces_the_active_version(backend: Storage) -> None:
    with backend.transaction() as tx:
        tx.apply_schema_change(change(), None)
        tx.apply_schema_change(change(action="pin", doc="d", pin_doc="d", pin_version=1, active=None), None)
        tx.apply_schema_change(change(action="activate", active=2, from_version=1, to_version=2), None)
        tx.apply_schema_change(change(action="unpin", doc="d", pin_doc="d", pin_version=None, active=None), None)
    with backend.read() as tx:
        assert tx.schema_state("t") == (4, 2, {})


def test_a_stale_expected_generation_is_refused_and_changes_nothing(backend: Storage) -> None:
    with backend.transaction() as tx:
        tx.apply_schema_change(change(), None)
    with pytest.raises(ConstraintError):
        with backend.transaction() as tx:
            tx.apply_schema_change(change(active=2, to_version=2), 0)
    with backend.read() as tx:
        assert tx.schema_state("t") == (1, 1, {}) and len(tx.schema_history("t")) == 1


def test_a_version_is_served_once_and_a_different_fingerprint_for_it_is_refused(backend: Storage) -> None:
    with backend.transaction() as tx:
        tx.apply_schema_change(change(served=((1, "a" * 64, "{}"),)), None)
        tx.apply_schema_change(
            change(action="pin", doc="d", pin_doc="d", pin_version=1, active=None, served=((1, "a" * 64, "{}"),)), None
        )
    with pytest.raises(ConstraintError):
        with backend.transaction() as tx:
            tx.apply_schema_change(change(served=((1, "b" * 64, "{}"),)), None)


def test_an_injected_failure_inside_leaves_everything_as_before(backend: Storage) -> None:
    with backend.transaction() as tx:
        tx.apply_schema_change(change(), None)
    with pytest.raises(RuntimeError):
        with backend.transaction() as tx:
            tx.apply_schema_change(change(active=2, to_version=2), None)
            raise RuntimeError("a later step failed")
    with backend.read() as tx:
        assert tx.schema_state("t") == (1, 1, {}) and len(tx.schema_history("t")) == 1


def test_history_can_be_limited_and_filtered_by_document(backend: Storage) -> None:
    with backend.transaction() as tx:
        tx.apply_schema_change(change(), None)
        for doc in ("a", "b", "a"):
            tx.apply_schema_change(change(action="pin", doc=doc, pin_doc=doc, pin_version=1, active=None), None)
    with backend.read() as tx:
        assert [h.doc for h in tx.schema_history("t", doc="a")] == ["a", "a"]
        assert [h.doc for h in tx.schema_history("t", limit=2)] == ["b", "a"]  # the newest two, oldest first
        assert tx.schema_history("other") == []


def test_another_schema_id_has_its_own_state(backend: Storage) -> None:
    with backend.transaction() as tx:
        tx.apply_schema_change(change(), None)
        tx.apply_schema_change(change(schema_id="u", active=5, to_version=5), None)
    with backend.read() as tx:
        assert tx.schema_state("t")[1] == 1 and tx.schema_state("u")[1] == 5


def test_a_read_transaction_cannot_change_it(backend: Storage) -> None:
    with backend.read() as tx:
        with pytest.raises(RuntimeError):
            tx.apply_schema_change(change(), None)


def test_the_history_accepts_inserts_only(tmp_path) -> None:  # noqa: ANN001
    storage = SqliteStorage(str(tmp_path / "t.db"))
    with storage.transaction() as tx:
        tx.apply_schema_change(change(), None)
    storage.close()
    connection = sqlite3.connect(str(tmp_path / "t.db"))
    for statement in (
        "UPDATE schema_history SET operator = 'x'",
        "DELETE FROM schema_history",
        "UPDATE schema_versions SET fingerprint = 'x'",
        "DELETE FROM schema_versions",
    ):
        try:
            connection.execute(statement)
        except sqlite3.DatabaseError as error:
            assert "append-only" in str(error)
        else:
            if "schema_versions" in statement:
                continue  # an empty table: the statement matches no row and nothing is refused
            raise AssertionError(statement)
    connection.close()


def test_one_change_can_set_several_pins_and_an_injected_failure_undoes_them(backend: Storage) -> None:
    with pytest.raises(RuntimeError):
        with backend.transaction() as tx:
            tx.apply_schema_change(change(action="bootstrap", pins=(("a", 1), ("b", 2))), None)
            raise RuntimeError("later step failed")
    with backend.read() as tx:
        assert tx.schema_state("t") == (0, None, {})
    with backend.transaction() as tx:
        tx.apply_schema_change(change(action="bootstrap", pins=(("a", 1), ("b", 2))), None)
    with backend.read() as tx:
        assert tx.schema_state("t") == (1, 1, {"a": 1, "b": 2})


def _entry(doc: str, seq: int, tier: str):  # type: ignore[no-untyped-def]
    from aduku.model import Entry

    return Entry(
        id=f"e-{doc}-{seq}", doc=doc, seq=seq, request_id=f"r-{doc}-{seq}", tier=tier, path="/name", op="set",
        value="x", contributor="c", tx_time=NOW, schema_version=1,
    )  # fmt: skip


def test_the_tiers_in_use_across_the_ledger_follow_the_entries_and_a_rolled_back_append(backend: Storage) -> None:
    from aduku.storage.base import RequestRecord

    with backend.read() as tx:
        assert tx.tiers_in_use() == set()
    with backend.transaction() as tx:
        tx.append_request(RequestRecord("r-a-1", "a", "c"), [_entry("a", 1, "user")])
        tx.append_request(RequestRecord("r-b-1", "b", "c"), [_entry("b", 1, "admin"), _entry("b", 2, "user")])
    with pytest.raises(RuntimeError):
        with backend.transaction() as tx:
            tx.append_request(RequestRecord("r-c-1", "c", "c"), [_entry("c", 1, "backfill")])
            raise RuntimeError("later step failed")
    with backend.read() as tx:
        assert tx.tiers_in_use() == {"user", "admin"} and tx.tiers_in_use("a") == {"user"}


def test_the_compacted_documents_are_listed_without_reading_the_others(backend: Storage) -> None:
    with backend.read() as tx:
        assert tx.compacted_docs() == []
    with backend.transaction() as tx:
        tx.bump_epoch("b")
        tx.bump_epoch("a")
    with backend.read() as tx:
        assert tx.compacted_docs() == ["a", "b"]
