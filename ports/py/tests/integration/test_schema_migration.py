"""Feature 006 (US3, FR-015 to FR-017, SC-007): moving pinned documents to a version, one or many, safely and again."""

from __future__ import annotations

import json
import logging

import pytest

from aduku.errors import ErrorCode, RejectedError
from aduku.storage.base import Storage
from tests.integration.test_custom_rules import faulty
from tests.schema_helpers import Env, field_text, versions_text
from tests.service_helpers import ALICE, put

BASE = field_text()
MIN = BASE.replace('"/age": {type: int, merge: max}', '"/age": {type: int, merge: min}')
THIRD = BASE.replace('"/age": {type: int, merge: max}', '"/age": {type: int, merge: max, default: 0}')


def service(backend: Storage):  # type: ignore[no-untyped-def]
    """Version 2 is active; d1 and d2 are pinned to version 1; d3 follows the active version."""
    db = Env(backend, *versions_text(BASE, MIN, THIRD)).service(active=1)
    for doc in ("d1", "d2", "d3"):
        db.append(doc, [put("/name", doc), put("/age", 3), put("/age", 5)], contributor=ALICE)
    db.pin_document("d1", 1, operator="alice")
    db.pin_document("d2", 1, operator="alice")
    db.activate_schema(2, operator="alice", acknowledge_breaking=True)
    return db


def test_plan_migration_shows_one_documents_changes_and_writes_nothing(backend: Storage) -> None:
    db = service(backend)
    before = (db.schema_history(), db.schemas.version_for("d1").version, db.list_entries("d1"))
    change = db.plan_migration("d1")
    assert (change.doc, change.document_changed) == ("d1", True) and change.errors_added == ()
    assert (db.schema_history(), db.schemas.version_for("d1").version, db.list_entries("d1")) == before
    assert db.plan_migration("d1", to=1).changed is False  # to the version it is on: nothing


def test_migrating_to_the_active_version_unpins_the_document_and_leaves_its_entries(backend: Storage) -> None:
    db = service(backend)
    entries = db.list_entries("d1")
    assert db.get("d1").document["age"] == 5
    report = db.migrate(["d1"], operator="alice")
    assert report.moved == ("d1",) and report.failed == () and report.not_pinned == ()
    assert db.schemas.pins() == {"d2": 1} and db.get("d1").document["age"] == 3 and db.list_entries("d1") == entries
    record = db.schema_history(doc="d1")[-1]
    assert (record.action, record.operator, record.from_version, record.to_version) == ("migrate", "alice", 1, 2)


def test_migrating_to_another_version_moves_the_pin(backend: Storage) -> None:
    db = service(backend)
    report = db.migrate(["d1"], to=3, operator="alice")
    assert report.moved == ("d1",) and db.schemas.pins() == {"d1": 3, "d2": 1}
    assert db.get("d1").document["age"] == 5  # version 3 folds with max again, and adds a default of 0


def test_a_batch_without_a_list_moves_every_pinned_document(backend: Storage) -> None:
    db = service(backend)
    report = db.migrate(operator="alice")
    assert report.moved == ("d1", "d2") and db.schemas.pins() == {}


def test_a_document_that_cannot_be_derived_under_the_target_is_failed_and_stays_the_others_move(
    backend: Storage,
) -> None:
    state, rule = faulty("raises")
    fragile = MIN.replace(
        '"/name": {type: string, merge: lww}', '"/name": {type: string, merge: lww, validate: ["acme.fragile"]}'
    )
    env = Env(backend, *versions_text(BASE, fragile))
    env.behaviors.register_validate("acme.fragile", 1, rule, args=(), applies="field")
    db = env.service(active=1)
    db.append("good", [put("/age", 3)], contributor=ALICE)
    db.append("bad", [put("/name", "boom")], contributor=ALICE)
    db.pin_document("good", 1, operator="alice")
    db.pin_document("bad", 1, operator="alice")
    db.activate_schema(2, operator="alice", acknowledge_breaking=True)
    state["on"] = True
    report = db.migrate(operator="alice")
    assert report.moved == ("good",) and [doc for doc, _ in report.failed] == ["bad"]
    assert report.failed[0][1] and db.schemas.pins() == {"bad": 1}


def test_running_a_batch_again_reports_the_moved_ones_and_touches_nothing(backend: Storage) -> None:
    db = service(backend)
    db.migrate(["d1"], operator="alice")
    before = db.schema_history()
    again = db.migrate(["d1", "d2"], operator="alice")
    assert again.already_moved == ("d1",) and again.moved == ("d2",)
    assert [r.doc for r in db.schema_history()[len(before) :]] == ["d2"]
    third = db.migrate(["d1", "d2"], operator="alice")
    assert third.already_moved == ("d1", "d2") and third.moved == () and len(db.schema_history()) == len(before) + 1


def test_a_document_that_is_not_pinned_is_reported_in_a_batch_and_refused_alone(backend: Storage) -> None:
    db = service(backend)
    report = db.migrate(["d3", "d1"], operator="alice")
    assert report.not_pinned == ("d3",) and report.moved == ("d1",)
    for call in (lambda: db.migrate(["d3"], operator="alice"), lambda: db.plan_migration("d3")):
        with pytest.raises(RejectedError) as caught:
            call()
        assert caught.value.code is ErrorCode.NOT_PINNED


def test_the_operator_is_required(backend: Storage) -> None:
    db = service(backend)
    with pytest.raises(ValueError):
        db.migrate(["d1"], operator=" ")
    assert db.schemas.pins() == {"d1": 1, "d2": 1}


def test_a_target_the_registry_lacks_is_refused(backend: Storage) -> None:
    with pytest.raises(RejectedError) as caught:
        service(backend).migrate(["d1"], to=9, operator="alice")
    assert caught.value.code is ErrorCode.SCHEMA_INACTIVE


def test_batches_report_progress(backend: Storage) -> None:
    db = Env(backend, *versions_text(BASE, MIN)).service(active=1)
    for number in range(250):
        db.pin_document(f"p{number:03d}", 1, operator="alice")
    db.activate_schema(2, operator="alice", acknowledge_breaking=True)
    seen = []
    report = db.migrate(operator="alice", batch=100, progress=seen.append)
    assert report.batches == 3 and len(report.moved) == 250
    assert [(p.batch, p.processed, p.total, p.moved) for p in seen] == [
        (1, 100, 250, 100),
        (2, 200, 250, 200),
        (3, 250, 250, 250),
    ]


def test_a_batch_stopped_part_way_and_run_again_moves_each_document_once(backend: Storage) -> None:
    db = Env(backend, *versions_text(BASE, MIN)).service(active=1)
    for number in range(1000):
        db.pin_document(f"p{number:04d}", 1, operator="alice")
    db.activate_schema(2, operator="alice", acknowledge_breaking=True)

    def stop_after_three(progress) -> None:  # type: ignore[no-untyped-def]
        if progress.batch == 3:
            raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        db.migrate(operator="alice", batch=100, progress=stop_after_three)
    assert len(db.schemas.pins()) == 700  # three batches were moved before the stop
    rest = db.migrate(operator="alice", batch=100)
    assert len(rest.moved) == 700 and db.schemas.pins() == {}
    moved = [r.doc for r in db.schema_history() if r.action == "migrate"]
    assert len(moved) == 1000 and len(set(moved)) == 1000  # each exactly once


def test_each_document_moved_or_failed_and_each_batch_writes_a_log_line(
    backend: Storage, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="aduku")
    db = service(backend)
    db.migrate(["d1", "d3"], operator="alice")
    events = [
        json.loads(r.getMessage()) for r in caplog.records if r.name == "aduku" and r.getMessage().startswith("{")
    ]
    moved = [e for e in events if e["event"] == "schema_migrated"]
    assert [(e["doc"], e["operator"], e["from"], e["to"]) for e in moved] == [("d1", "alice", 1, 2)]
    (batch,) = [e for e in events if e["event"] == "schema_migration_batch"]
    assert (batch["moved"], batch["not_pinned"], batch["failed"]) == (1, 1, 0) and "age" not in json.dumps(batch)


def test_a_document_already_pinned_at_a_chosen_target_is_reported_already_moved_and_not_recorded_again(
    backend: Storage,
) -> None:
    db = service(backend)
    db.migrate(["d1"], to=3, operator="alice")
    before = db.schema_history()
    again = db.migrate(["d1"], to=3, operator="alice")
    assert again.already_moved == ("d1",) and again.moved == () and db.schema_history() == before
