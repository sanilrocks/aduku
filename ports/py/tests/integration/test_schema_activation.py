"""Feature 006 (US2, FR-012, FR-004): an activation is classified, a breaking one needs an acknowledgement, and none touches a document."""

from __future__ import annotations

import json
import logging

import pytest

from aduku.errors import ErrorCode, RejectedError, SchemaError
from aduku.storage.base import Storage
from tests.schema_helpers import Env, field_text, versions_text
from tests.service_helpers import ALICE, ROOT, put

BASE = field_text()
BREAKING = BASE.replace('"/age": {type: int, merge: max}', '"/age": {type: int, merge: min}')
VISIBLE = BASE.replace('"/age": {type: int, merge: max}', '"/age": {type: int, merge: max, default: 0}')
COMPATIBLE = BASE + ', "/nick": {type: string, merge: lww}'
TEXTS = versions_text(BASE, BREAKING, VISIBLE, COMPATIBLE)


def service(backend: Storage):  # type: ignore[no-untyped-def]
    db = Env(backend, *TEXTS).service(active=1)
    db.append("d1", [put("/name", "A"), put("/age", 3), put("/age", 5)], contributor=ALICE)
    db.append("d2", [put("/name", "B")], contributor=ALICE)
    return db


def lines(caplog: pytest.LogCaptureFixture, event: str) -> list[dict]:  # type: ignore[type-arg]
    found = [json.loads(r.getMessage()) for r in caplog.records if r.name == "aduku" and r.getMessage().startswith("{")]
    return [item for item in found if item["event"] == event]


def test_a_breaking_target_is_refused_without_the_acknowledgement_naming_the_changes(backend: Storage) -> None:
    db = service(backend)
    before = db.schema_history()
    with pytest.raises(RejectedError) as caught:
        db.activate_schema(2, operator="alice")
    assert caught.value.code is ErrorCode.BREAKING_CHANGE_NOT_ACKNOWLEDGED
    assert "breaking" in str(caught.value) and "merge_changed" in str(caught.value) and "/age" in str(caught.value)
    assert db.schemas.active().version == 1 and db.schema_history() == before


def test_a_breaking_target_goes_with_the_acknowledgement_and_is_recorded_with_its_class(backend: Storage) -> None:
    db = service(backend)
    report = db.activate_schema(2, operator="alice", acknowledge_breaking=True)
    assert (report.version, report.previous, report.change_class, report.rollback) == (2, 1, "breaking", False)
    assert db.schemas.active().version == 2 and db.schema_history()[-1].change_class == "breaking"


def test_a_visible_or_compatible_target_needs_no_acknowledgement(backend: Storage) -> None:
    db = service(backend)
    assert db.activate_schema(3, operator="alice").change_class == "visible"
    assert db.activate_schema(1, operator="alice").rollback
    assert db.activate_schema(4, operator="alice").change_class == "compatible"
    assert [r.change_class for r in db.schema_history()[-3:]] == ["visible", "visible", "compatible"]


def test_a_rollback_is_recorded_as_one_and_needs_no_acknowledgement_even_for_a_breaking_difference(
    backend: Storage,
) -> None:
    db = service(backend)
    db.activate_schema(2, operator="alice", acknowledge_breaking=True)
    report = db.activate_schema(1, operator="alice")
    assert (report.rollback, report.change_class) == (True, "breaking")
    assert db.schema_history()[-1].action == "rollback" and db.schemas.active().version == 1


def test_a_stale_expected_generation_is_refused_as_schema_state_changed_and_records_nothing(backend: Storage) -> None:
    db = service(backend)
    seen = db.schemas.generation()
    db.pin_document("d2", 1, operator="bob")
    with pytest.raises(SchemaError) as caught:
        db.activate_schema(4, operator="alice", expect_generation=seen)
    assert caught.value.codes == ("SCHEMA_STATE_CHANGED",) and db.schemas.active().version == 1
    db.activate_schema(4, operator="alice", expect_generation=db.schemas.generation())
    assert db.schemas.active().version == 4


def test_a_version_that_folds_differently_from_expired_history_is_refused_and_records_nothing(backend: Storage) -> None:
    db = service(backend)
    db.compact("d1", through_seq=3)
    db.expire_archive("d1", through_seq=3)
    before = db.schema_history()
    with pytest.raises(SchemaError) as caught:
        db.activate_schema(2, operator="alice", acknowledge_breaking=True)
    assert caught.value.codes == ("HISTORY_COMPACTED",) and db.schema_history() == before
    assert db.activate_schema(3, operator="alice").change_class == "visible"  # a default folds the same way: allowed


def test_a_version_that_drops_a_used_tier_is_refused_and_records_nothing(backend: Storage) -> None:
    texts = list(TEXTS)
    texts[1] = texts[1].replace("tiers: [backfill, user, admin]", "tiers: [backfill, admin]")
    db = Env(backend, *texts).service(active=1)
    db.append("d1", [put("/name", "A")], contributor=ALICE)
    before = db.schema_history()
    with pytest.raises(SchemaError) as caught:
        db.activate_schema(2, operator="alice", acknowledge_breaking=True)
    assert caught.value.codes == ("TIER_IN_USE",) and db.schema_history() == before


def test_an_unregistered_version_is_refused_before_anything_else(backend: Storage) -> None:
    with pytest.raises(RejectedError) as caught:
        service(backend).activate_schema(9, operator="alice")
    assert caught.value.code is ErrorCode.SCHEMA_INACTIVE


def test_an_activation_writes_no_view_layer_or_entry(backend: Storage) -> None:
    db = service(backend)

    def stored() -> list:  # type: ignore[type-arg]
        with backend.read() as tx:
            return [(d, tx.get_view(d), tx.get_layers(d), tx.entries(d, 0), tx.latest_effects(d)) for d in tx.doc_ids()]

    before = stored()
    db.activate_schema(4, operator="alice")
    db.activate_schema(3, operator="alice")
    assert stored() == before


def test_the_first_read_after_an_activation_derives_the_document_under_the_new_version(backend: Storage) -> None:
    db = service(backend)
    assert db.get("d1").document["age"] == 5
    db.activate_schema(2, operator="alice", acknowledge_breaking=True)
    assert db.get("d1").document["age"] == 3 and db.get("d1").document["name"] == "A"


def test_a_refused_and_an_accepted_activation_each_write_a_log_line(
    backend: Storage, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="aduku")
    db = service(backend)
    with pytest.raises(RejectedError):
        db.activate_schema(2, operator="alice")
    refused = lines(caplog, "schema_activation_refused")
    assert len(refused) == 1 and refused[0]["operator"] == "alice" and refused[0]["version"] == 2
    assert refused[0]["code"] == "BREAKING_CHANGE_NOT_ACKNOWLEDGED" and refused[0]["class"] == "breaking"
    db.activate_schema(2, operator="alice", acknowledge_breaking=True)
    (accepted,) = lines(caplog, "schema_activated")
    assert (accepted["operator"], accepted["previous"], accepted["version"], accepted["class"]) == (
        "alice",
        1,
        2,
        "breaking",
    )
    assert "A" not in json.dumps(accepted.get("first", "")) and "d1" not in json.dumps(accepted)


def test_an_entry_that_arrives_between_the_check_and_the_write_still_stops_the_activation(backend: Storage) -> None:
    """FR-003: the tier check runs inside the write transaction, so a racing append cannot slip past it."""
    texts = list(TEXTS)
    texts[1] = texts[1].replace("tiers: [backfill, user, admin]", "tiers: [backfill, user]")
    env = Env(backend, *texts)
    db, other = env.service(active=1), env.service(active=1)
    db.append("d1", [put("/name", "A")], contributor=ALICE)  # only the user tier is in use so far
    state = db.schemas._state
    assert state is not None
    real = state.apply

    def racing(change, *, expect_generation, check=None):  # type: ignore[no-untyped-def]
        other.append("d2", [put("/name", "B", tier="admin")], contributor=ROOT)  # lands after the outer check passed
        return real(change, expect_generation=expect_generation, check=check)

    state.apply = racing  # type: ignore[method-assign]
    before = db.schema_history()
    with pytest.raises(SchemaError) as caught:
        db.activate_schema(2, operator="alice", acknowledge_breaking=True)
    assert caught.value.codes == ("TIER_IN_USE",) and db.schema_history() == before
    assert db.schemas.active_version() == 1
