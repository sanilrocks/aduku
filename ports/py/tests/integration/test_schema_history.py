"""Feature 006 (US1): every change of the schema state leaves one history record, in one transaction with the change."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import pytest

from aduku.errors import ErrorCode, RejectedError, SchemaError
from aduku.storage.base import Storage
from tests.schema_helpers import Env, field_text, versions_text
from tests.service_helpers import ALICE, put


def texts() -> list[str]:
    return versions_text(
        field_text(), field_text(', "/nick": {type: string, merge: lww}'), field_text(', "/z": {type: int, merge: max}')
    )


def shape(db: Any) -> list[tuple[str, str, str | None, int | None, int | None]]:
    return [(r.action, r.operator, r.doc, r.from_version, r.to_version) for r in db.schema_history()]


def test_the_first_service_leaves_a_bootstrap_record(backend: Storage) -> None:
    db = Env(backend, *texts()).service(active=1)
    assert shape(db) == [("bootstrap", "bootstrap", None, None, 1)]


def test_activate_rollback_pin_and_unpin_each_leave_one_record(backend: Storage) -> None:
    db = Env(backend, *texts()).service(active=1)
    db.activate_schema(2, operator="alice", acknowledge_breaking=True)
    db.pin_document("d", 1, operator="bob")
    db.unpin_document("d", operator="bob")
    db.activate_schema(1, operator="carol")
    assert shape(db)[1:] == [
        ("activate", "alice", None, 1, 2),
        ("pin", "bob", "d", None, 1),
        ("unpin", "bob", "d", 1, None),
        ("rollback", "carol", None, 2, 1),
    ]
    first, *_, last = db.schema_history()
    assert first.at < last.at and last.id > first.id and db.schemas.active().version == 1


def test_a_record_carries_the_time_of_the_service_clock(backend: Storage) -> None:
    db = Env(backend, *texts()).service(active=1)
    db.activate_schema(2, operator="alice", acknowledge_breaking=True)
    record = db.schema_history()[-1]
    assert record.at.tzinfo is not None and record.at <= db.clock.now()


def test_activating_the_active_version_records_nothing(backend: Storage) -> None:
    db = Env(backend, *texts()).service(active=2)
    before = db.schema_history()
    report = db.activate_schema(2, operator="alice", acknowledge_breaking=True)
    assert db.schema_history() == before and report.version == 2 and report.previous == 2 and report.rollback is False


def test_the_operator_is_required_and_not_empty(backend: Storage) -> None:
    db = Env(backend, *texts()).service(active=1)
    for operator in ("", "   "):
        with pytest.raises(ValueError):
            db.activate_schema(2, operator=operator)
        with pytest.raises(ValueError):
            db.pin_document("d", 1, operator=operator)
        with pytest.raises(ValueError):
            db.unpin_document("d", operator=operator)
    assert db.schemas.active().version == 1 and len(db.schema_history()) == 1


def test_an_unregistered_version_is_refused(backend: Storage) -> None:
    db = Env(backend, *texts()).service(active=1, only=[1, 2])
    with pytest.raises(RejectedError) as caught:
        db.activate_schema(3, operator="alice")
    assert caught.value.code is ErrorCode.SCHEMA_INACTIVE


def test_a_version_that_drops_a_used_tier_is_refused_and_leaves_no_record(backend: Storage) -> None:
    env = Env(backend, *versions_text(field_text(), field_text(), tiers="backfill, user, admin"))
    env.texts[1] = env.texts[1].replace("tiers: [backfill, user, admin]", "tiers: [backfill, admin]")
    db = env.service(active=1)
    db.append("d", [put("/name", "A")], contributor=ALICE)
    with pytest.raises(SchemaError) as caught:
        db.activate_schema(2, operator="alice", acknowledge_breaking=True)
    assert caught.value.codes == ("TIER_IN_USE",) and len(db.schema_history()) == 1


def test_a_stale_expected_generation_is_refused_and_changes_nothing(backend: Storage) -> None:
    db = Env(backend, *texts()).service(active=1)
    seen = db.schemas.generation()
    db.pin_document("d", 1, operator="bob")
    with pytest.raises(SchemaError) as caught:
        db.activate_schema(2, operator="alice", expect_generation=seen)
    assert caught.value.codes == ("SCHEMA_STATE_CHANGED",) and db.schemas.active().version == 1


class FaultyStorage:
    """Applies a schema change and then fails, as a disk error at commit would."""

    def __init__(self, inner: Storage) -> None:
        self.inner = inner
        self.fail = False

    @contextmanager
    def transaction(self) -> Iterator[Any]:
        with self.inner.transaction() as tx:
            yield _FaultyTx(tx, self)

    def read(self) -> Any:
        return self.inner.read()

    def audit_backend(self) -> Any:
        return self.inner.audit_backend()

    def close(self) -> None:
        self.inner.close()


class _FaultyTx:
    def __init__(self, inner: Any, owner: FaultyStorage) -> None:
        self.inner, self.owner = inner, owner

    def __getattr__(self, name: str) -> Any:
        return getattr(self.inner, name)

    def apply_schema_change(self, change: Any, expect: Any) -> int:
        generation = self.inner.apply_schema_change(change, expect)
        if self.owner.fail:
            raise RuntimeError("disk error")
        return generation


def test_a_failure_inside_an_activation_leaves_the_active_version_and_the_history_as_before(backend: Storage) -> None:
    faulty = FaultyStorage(backend)
    db = Env(faulty, *texts()).service(active=1)  # type: ignore[arg-type]
    before = shape(db)
    faulty.fail = True
    with pytest.raises(RuntimeError):
        db.activate_schema(2, operator="alice", acknowledge_breaking=True)
    with pytest.raises(RuntimeError):
        db.pin_document("d", 2, operator="alice")
    faulty.fail = False
    assert shape(db) == before and db.schemas.active().version == 1 and db.schemas.version_for("d").version == 1


def test_history_can_be_limited_and_filtered_by_document(backend: Storage) -> None:
    db = Env(backend, *texts()).service(active=1)
    db.pin_document("a", 2, operator="x")
    db.pin_document("b", 2, operator="x")
    db.unpin_document("a", operator="x")
    assert [r.action for r in db.schema_history(doc="a")] == ["pin", "unpin"]
    assert [r.doc for r in db.schema_history(limit=2)] == ["b", "a"]


def test_the_history_is_shared_by_every_service_on_the_store(backend: Storage) -> None:
    env = Env(backend, *texts())
    env.service(active=1).activate_schema(2, operator="alice", acknowledge_breaking=True)
    assert [r.operator for r in env.service(active=1).schema_history()] == ["bootstrap", "alice"]
