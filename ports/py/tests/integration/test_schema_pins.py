"""Feature 006 (US3, FR-014): a pinned document is read and written under its own version, and a pin is refused for the reasons
an activation is."""

from __future__ import annotations

import pytest

from aduku.errors import ErrorCode, RejectedError, SchemaError
from aduku.storage.base import Storage
from tests.schema_helpers import Env, field_text, versions_text
from tests.service_helpers import ALICE, ROOT, put

BASE = field_text()
NICK = BASE + ', "/nick": {type: string, merge: lww}'
MIN = BASE.replace('"/age": {type: int, merge: max}', '"/age": {type: int, merge: min}')


def env(backend: Storage, *bodies: str, tiers: str = "backfill, user, admin") -> Env:
    return Env(backend, *versions_text(*(bodies or (BASE, NICK)), tiers=tiers))


def test_a_pinned_document_is_read_and_written_under_its_version_while_others_follow_the_active_one(
    backend: Storage,
) -> None:
    db = env(backend).service(active=2)
    db.pin_document("d1", 1, operator="alice")
    with pytest.raises(RejectedError) as caught:
        db.append("d1", [put("/nick", "n")], contributor=ALICE)  # version 1 does not declare /nick
    assert caught.value.code is ErrorCode.UNKNOWN_PATH
    db.append("d1", [put("/name", "A")], contributor=ALICE)
    db.append("d2", [put("/name", "B"), put("/nick", "n")], contributor=ALICE)
    assert [e.schema_version for e in db.list_entries("d1")] == [1]
    assert [e.schema_version for e in db.list_entries("d2")] == [2, 2]
    assert db.schemas.version_for("d1").version == 1 and db.schemas.version_for("d2").version == 2


def test_a_pinned_document_reads_the_same_when_the_active_version_changes(backend: Storage) -> None:
    db = env(backend, BASE, MIN).service(active=1)
    db.append("d1", [put("/age", 3), put("/age", 5)], contributor=ALICE)
    db.pin_document("d1", 1, operator="alice")
    before = db.get("d1")
    db.activate_schema(2, operator="alice", acknowledge_breaking=True)
    assert db.get("d1").document == before.document and db.get("d1").document["age"] == 5
    db.unpin_document("d1", operator="alice")
    assert db.get("d1").document["age"] == 3


def test_a_pin_is_refused_for_a_used_tier_the_version_drops(backend: Storage) -> None:
    texts = versions_text(BASE, BASE)
    texts[1] = texts[1].replace("tiers: [backfill, user, admin]", "tiers: [backfill, user]")
    db = Env(backend, *texts).service(active=1)
    db.append("uses_admin", [put("/name", "A", tier="admin")], contributor=ROOT)
    db.append("only_user", [put("/name", "B")], contributor=ALICE)
    with pytest.raises(SchemaError) as caught:
        db.pin_document("uses_admin", 2, operator="alice")
    assert caught.value.codes == ("TIER_IN_USE",) and db.schemas.version_for("uses_admin").version == 1
    db.pin_document("only_user", 2, operator="alice")  # that document does not use the tier
    assert db.schemas.version_for("only_user").version == 2


def test_a_pin_is_refused_when_the_version_folds_differently_from_expired_history(backend: Storage) -> None:
    db = env(backend, BASE, MIN).service(active=1)
    db.append("d1", [put("/age", 3), put("/age", 5)], contributor=ALICE)
    db.compact("d1", through_seq=2)
    db.expire_archive("d1", through_seq=2)
    with pytest.raises(SchemaError) as caught:
        db.pin_document("d1", 2, operator="alice")
    assert caught.value.codes == ("HISTORY_COMPACTED",)


def test_a_pin_to_an_unregistered_version_is_refused(backend: Storage) -> None:
    with pytest.raises(RejectedError) as caught:
        env(backend).service(active=1).pin_document("d1", 9, operator="alice")
    assert caught.value.code is ErrorCode.SCHEMA_INACTIVE


def test_a_pin_made_by_one_service_is_seen_by_another_and_an_unpin_returns_the_document(backend: Storage) -> None:
    e = env(backend)
    first, second = e.service(active=2), e.service(active=2)
    first.pin_document("d1", 1, operator="alice")
    assert second.schemas.version_for("d1").version == 1
    second.unpin_document("d1", operator="bob")
    assert first.schemas.version_for("d1").version == 2


def test_every_pin_and_unpin_is_in_the_history_with_its_document_and_operator(backend: Storage) -> None:
    db = env(backend).service(active=2)
    db.pin_document("d1", 1, operator="alice")
    db.unpin_document("d1", operator="bob")
    assert [(r.action, r.operator, r.doc, r.from_version, r.to_version) for r in db.schema_history(doc="d1")] == [
        ("pin", "alice", "d1", None, 1),
        ("unpin", "bob", "d1", 1, None),
    ]


def test_repeating_a_pin_or_unpinning_a_document_that_is_not_pinned_records_nothing(backend: Storage) -> None:
    db = env(backend).service(active=2)
    db.pin_document("d1", 1, operator="alice")
    db.pin_document("d1", 1, operator="alice")
    db.unpin_document("other", operator="alice")
    assert [r.action for r in db.schema_history()] == ["bootstrap", "pin"]
