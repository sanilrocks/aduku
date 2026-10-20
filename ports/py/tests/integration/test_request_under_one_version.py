"""Feature 006 (FR-005, research R-8): a request is admitted wholly under the version that served when it started."""

from __future__ import annotations

from aduku.storage.base import Storage
from tests.schema_helpers import Env, field_text, versions_text
from tests.service_helpers import ALICE, put


def texts() -> list[str]:
    return versions_text(field_text(), field_text(', "/nick": {type: string, merge: lww}'))


def test_every_entry_of_a_request_carries_the_version_it_was_admitted_under(backend: Storage) -> None:
    env = Env(backend, *texts())
    db = env.service(active=1)
    db.append("d", [put("/name", "A"), put("/age", 3)], contributor=ALICE)
    db.activate_schema(2, operator="alice")
    db.append("d", [put("/name", "B"), put("/nick", "b")], contributor=ALICE)
    assert [(e.seq, e.schema_version) for e in db.list_entries("d")] == [(1, 1), (2, 1), (3, 2), (4, 2)]


def test_an_activation_between_two_requests_does_not_change_entries_already_written(backend: Storage) -> None:
    db = Env(backend, *texts()).service(active=1)
    db.append("d", [put("/name", "A")], contributor=ALICE)
    before = db.list_entries("d")
    db.activate_schema(2, operator="alice")
    assert db.list_entries("d") == before


def test_a_request_that_started_before_an_activation_is_admitted_wholly_under_the_old_version(backend: Storage) -> None:
    env = Env(backend, *texts())
    db = env.service(active=1)
    other = env.service(active=1)
    original = db.schemas.version_for
    seen = []

    def version_then_activate(doc: str):  # type: ignore[no-untyped-def]
        schema = original(doc)
        if not seen:  # another process activates right after this request took its version
            seen.append(schema.version)
            other.activate_schema(2, operator="bob")
        return schema

    db.schemas.version_for = version_then_activate  # type: ignore[method-assign]
    db.append("d", [put("/name", "A"), put("/age", 3)], contributor=ALICE)
    assert {e.schema_version for e in db.list_entries("d")} == {1}
    db.append("d", [put("/name", "B")], contributor=ALICE)
    assert [e.schema_version for e in db.list_entries("d")] == [1, 1, 2]
