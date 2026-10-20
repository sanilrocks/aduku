"""Feature 006 (US2, FR-008 to FR-012): the dry run says what activating a version would change and changes nothing."""

from __future__ import annotations

import hashlib

import pytest

from aduku.errors import ErrorCode, RejectedError
from aduku.schema.diff import schema_changes
from aduku.storage.base import Storage
from tests.schema_helpers import Env, field_text, versions_text
from tests.service_helpers import ALICE, put

V1 = field_text()
V2 = field_text().replace('"/age": {type: int, merge: max}', '"/age": {type: int, merge: min}')
HOOK = ', "/taxId": {type: string, computed: {impure: {hook: "fx.echo@1", depends: ["/name"], reason: "registry", timeout: "1s", retry: {max: 0, backoff: "1ms"}, deadline: "3s", recompute_by: [admin]}}}'
V3 = V1 + HOOK


def service(backend: Storage, *bodies: str):  # type: ignore[no-untyped-def]
    env = Env(backend, *versions_text(*(bodies or (V1, V2))))
    db = env.service(active=1)
    db.append("d1", [put("/name", "A"), put("/age", 3), put("/age", 5)], contributor=ALICE)  # max 5, min 3
    db.append("d2", [put("/name", "B"), put("/age", 4)], contributor=ALICE)  # the same under both
    db.append("d3", [put("/name", "C")], contributor=ALICE)
    return db


def stored(db) -> list:  # type: ignore[no-untyped-def,type-arg]
    with db.storage.read() as tx:
        docs = tx.doc_ids()
        return [
            [(d, tx.get_view(d), tx.get_layers(d), tx.epoch(d), tx.latest_effects(d), tx.get_derived(d)) for d in docs],
            tx.schema_state("t"),
            tx.schema_history("t"),
            [db.list_entries(d) for d in docs],
        ]


def test_the_report_counts_changed_and_unchanged_documents_and_names_what_differs(backend: Storage) -> None:
    db = service(backend)
    report = db.plan_schema(2)
    assert (report.target, report.examined, report.left_out) == (2, 3, 0)
    assert (report.changed, report.unchanged, report.failed, report.would_call_hook) == (1, 2, 0, 0)
    assert [c.doc for c in report.documents] == ["d1"] and report.documents[0].document_changed
    assert report.change_list == schema_changes(db.schemas.get(1), db.schemas.get(2))
    assert report.generation == db.schemas.generation()


def test_the_dry_run_writes_and_calls_nothing(backend: Storage) -> None:
    db = service(backend, V1, V3)
    before = stored(db)
    calls = db.behaviors.get("fx.echo", 1).impl.calls  # type: ignore[union-attr]
    report = db.plan_schema(2)
    assert stored(db) == before and db.behaviors.get("fx.echo", 1).impl.calls == calls  # type: ignore[union-attr]
    assert report.would_call_hook == 3 and all(c.would_call_hook for c in report.documents)


def test_a_dry_run_of_the_active_version_changes_nothing(backend: Storage) -> None:
    report = service(backend).plan_schema(1)
    assert (report.changed, report.unchanged, report.change_list.changes) == (0, 3, ())


def test_a_list_of_documents_limits_what_is_examined_and_says_how_many_were_left_out(backend: Storage) -> None:
    db = service(backend)
    report = db.plan_schema(2, docs=["d1", "d3", "ghost"])
    assert (report.examined, report.left_out, report.changed, report.unchanged) == (3, 1, 1, 2)  # d2 is left out
    one = db.plan_schema(2, docs=["d2"])
    assert (one.examined, one.left_out, one.unchanged) == (1, 2, 1)


def test_a_document_with_no_entries_is_unchanged(backend: Storage) -> None:
    report = service(backend).plan_schema(2, docs=["ghost"])
    assert (report.examined, report.unchanged, report.changed) == (1, 1, 0)


def test_a_limit_takes_the_same_documents_every_time_ordered_by_a_hash_of_the_name(backend: Storage) -> None:
    db = service(backend)
    first = db.plan_schema(2, limit=2)
    again = db.plan_schema(2, limit=2)
    assert (first.examined, first.left_out) == (2, 1) and first == again
    ranked = sorted(["d1", "d2", "d3"], key=lambda d: hashlib.sha256(d.encode()).hexdigest())
    assert {c.doc for c in first.documents} <= set(ranked[:2])
    wanted = db.plan_schema(2, docs=ranked[:2])
    assert (wanted.changed, wanted.unchanged) == (first.changed, first.unchanged)


def test_a_limit_does_not_depend_on_the_order_the_documents_were_written(backend: Storage) -> None:
    env = Env(backend, *versions_text(V1, V2))
    db = env.service(active=1)
    for doc in ("d3", "d1", "d2"):
        db.append(doc, [put("/name", doc)], contributor=ALICE)
    db.append("d1", [put("/age", 3), put("/age", 5)], contributor=ALICE)
    once = db.plan_schema(2, limit=1)
    other = Env(backend, *versions_text(V1, V2)).service(active=1).plan_schema(2, limit=1)
    assert once == other


def test_pinned_documents_are_left_out_and_counted(backend: Storage) -> None:
    db = service(backend)
    db.pin_document("d1", 1, operator="alice")
    report = db.plan_schema(2)
    assert (report.examined, report.left_out, report.changed, report.unchanged) == (2, 1, 0, 2)
    assert all(c.doc != "d1" for c in report.documents)


def test_a_target_the_registry_lacks_is_refused(backend: Storage) -> None:
    with pytest.raises(RejectedError) as caught:
        service(backend).plan_schema(9)
    assert caught.value.code is ErrorCode.SCHEMA_INACTIVE


def test_a_document_whose_history_cannot_be_derived_under_the_target_is_failed_with_a_code(backend: Storage) -> None:
    db = service(backend)
    db.compact("d1", through_seq=3)
    db.expire_archive("d1", through_seq=3)
    report = db.plan_schema(2)
    assert (report.failed, report.changed, report.unchanged, report.examined) == (1, 0, 2, 3)
    (failed,) = report.documents
    assert (failed.doc, failed.failure_code) == ("d1", "HISTORY_COMPACTED")
    assert report.changed + report.unchanged + report.failed == report.examined


def test_the_dry_run_predicts_the_read_after_the_activation(backend: Storage) -> None:
    db = service(backend)
    report = db.plan_schema(2)
    now = {d: db.get(d) for d in ("d1", "d2", "d3")}
    db.activate_schema(2, operator="alice", acknowledge_breaking=True)
    for doc, before in now.items():
        after = db.get(doc)
        planned = next((c for c in report.documents if c.doc == doc), None)
        assert (planned is not None) == (after.document != before.document or after.model_errors != before.model_errors)
        if planned is not None:
            assert planned.skipped_before == before.skipped and planned.skipped_after == after.skipped


def test_a_limit_takes_the_document_first_in_hash_order_not_name_order(backend: Storage) -> None:
    names = [f"n{number}" for number in range(8)]
    by_hash = sorted(names, key=lambda d: hashlib.sha256(d.encode()).hexdigest())
    assert by_hash[0] != min(names)
    db = Env(backend, *versions_text(V1, V2)).service(active=1)
    for name in names:
        ages = [put("/age", 3), put("/age", 5)] if name == by_hash[0] else [put("/age", 4)]
        db.append(name, [put("/name", name), *ages], contributor=ALICE)
    report = db.plan_schema(2, limit=1)
    assert [c.doc for c in report.documents] == [by_hash[0]] and report.changed == 1


def test_the_service_lists_the_changes_between_two_registered_versions(backend: Storage) -> None:
    db = service(backend)
    assert db.schema_changes(1, 2) == schema_changes(db.schemas.get(1), db.schemas.get(2))
    assert db.schema_changes(1, 2).overall == "breaking" and db.schema_changes(1, 1).changes == ()
    with pytest.raises(RejectedError) as caught:
        db.schema_changes(1, 9)
    assert caught.value.code is ErrorCode.SCHEMA_INACTIVE
