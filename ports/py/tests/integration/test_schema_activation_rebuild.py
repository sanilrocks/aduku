"""Feature 006 (FR-013, clarification 5): an activation may ask for an eager rebuild; the activation stands whatever it finds."""

from __future__ import annotations

from aduku.storage.base import Storage
from tests.integration.test_custom_rules import faulty
from tests.schema_helpers import Env, field_text, versions_text
from tests.service_helpers import ALICE, put

BASE = field_text()
V2 = BASE.replace('"/age": {type: int, merge: max}', '"/age": {type: int, merge: min}')


def service(backend: Storage, *bodies: str):  # type: ignore[no-untyped-def]
    db = Env(backend, *versions_text(*(bodies or (BASE, V2)))).service(active=1)
    db.append("d1", [put("/name", "A"), put("/age", 3), put("/age", 5)], contributor=ALICE)
    db.append("d2", [put("/age", 4)], contributor=ALICE)
    db.append("d3", [put("/name", "C"), put("/age", 2)], contributor=ALICE)
    return db


def views(storage: Storage) -> dict[str, int]:
    with storage.read() as tx:
        return {d: v.schema_version for d in tx.doc_ids() if (v := tx.get_view(d)) is not None}


def test_an_eager_rebuild_refreshes_the_views_of_unpinned_documents_under_the_new_version(backend: Storage) -> None:
    db = service(backend)
    db.pin_document("d3", 1, operator="alice")
    assert views(backend) == {"d1": 1, "d2": 1, "d3": 1}
    report = db.activate_schema(2, operator="alice", acknowledge_breaking=True, rebuild=True)
    assert views(backend) == {"d1": 2, "d2": 2, "d3": 1}  # the pinned document keeps its version
    assert report.rebuild is not None and sorted(report.rebuild.checked) == ["d1", "d2"]
    derived = db.derivations
    assert db.get("d1").document["age"] == 3 and db.derivations == derived  # served from the refreshed view


def test_the_report_names_the_documents_whose_answer_changed(backend: Storage) -> None:
    db = service(backend)
    report = db.activate_schema(2, operator="alice", acknowledge_breaking=True, rebuild=True)
    assert report.changed == ("d1",)


def test_without_the_option_nothing_is_rebuilt(backend: Storage) -> None:
    db = service(backend)
    report = db.activate_schema(2, operator="alice", acknowledge_breaking=True)
    assert report.rebuild is None and report.changed == () and views(backend) == {"d1": 1, "d2": 1, "d3": 1}


def test_the_rebuild_writes_nothing_to_the_ledger(backend: Storage) -> None:
    db = service(backend)
    before = [db.list_entries(d) for d in ("d1", "d2", "d3")]
    db.activate_schema(2, operator="alice", acknowledge_breaking=True, rebuild=True)
    assert [db.list_entries(d) for d in ("d1", "d2", "d3")] == before


def test_the_activation_stands_when_a_document_cannot_be_derived_and_the_rest_are_rebuilt(backend: Storage) -> None:
    state, rule = faulty("raises")
    fragile = BASE.replace(
        '"/name": {type: string, merge: lww}', '"/name": {type: string, merge: lww, validate: ["acme.fragile"]}'
    )
    env = Env(backend, *versions_text(BASE, fragile))
    env.behaviors.register_validate("acme.fragile", 1, rule, args=(), applies="field")
    db = env.service(active=1)
    db.append("d1", [put("/name", "boom"), put("/age", 3)], contributor=ALICE)
    db.append("d2", [put("/age", 4)], contributor=ALICE)
    db.append("d3", [put("/name", "boom")], contributor=ALICE)
    state["on"] = True  # from now on the rule of version 2 crashes on the name "boom"
    report = db.activate_schema(2, operator="alice", rebuild=True)
    assert db.schemas.active().version == 2 and db.schema_history()[-1].action == "activate"
    assert report.rebuild is not None
    assert sorted(doc for doc, _ in report.rebuild.failed) == ["d1", "d3"]
    assert views(backend)["d2"] == 2  # the others went on
    assert db.activate_schema(1, operator="alice").rollback  # a rollback is a separate activation
