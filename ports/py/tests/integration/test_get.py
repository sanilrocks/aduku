"""FR-017: reading the merged document, with the view cache and the incremental derivation behind it."""

import pytest

from aduku.errors import RejectedError
from aduku.ion_codec import FrozenStruct, to_canonical
from aduku.model import DocumentResult
from aduku.storage.base import Storage
from aduku.storage.memory import MemoryStorage
from tests.service_helpers import ALICE, BOB, JOB, ROOT, delete, make_service, put, schema_text


def test_an_unknown_document_is_empty(backend: Storage) -> None:
    result = make_service(backend).get("nobody")
    assert result == DocumentResult(document=FrozenStruct(), model_errors=(), status="ok")


def test_get_reflects_every_accepted_request(backend: Storage) -> None:
    service = make_service(backend)
    service.append("d1", [put("/name", "A. Smith", tier="backfill"), put("/age", 30, tier="backfill")], contributor=JOB)
    service.append("d1", [put("/name", "Alice S."), put("/age", 28)], contributor=ALICE)
    assert to_canonical(service.get("d1").document) == '{age:30,name:"Alice S."}'
    service.append("d1", [delete("/name", tier="admin")], contributor=ROOT)
    assert to_canonical(service.get("d1").document) == "{age:30}"


def test_a_rejected_request_changes_nothing_that_get_returns(backend: Storage) -> None:
    service = make_service(backend)
    service.append("d1", [put()], contributor=ALICE)
    before = service.get("d1")
    with pytest.raises(RejectedError):
        service.append("d1", [put("/nope")], contributor=ALICE)
    assert service.get("d1") == before


def test_append_returns_the_latest_view_and_so_does_a_retry(backend: Storage) -> None:
    service = make_service(backend)
    first = service.append("d1", [put("/name", "Alice")], contributor=ALICE, request_key="K")
    assert first.view == service.get("d1") and first.view.document["name"] == "Alice"  # type: ignore[union-attr]
    service.append("d1", [put("/name", "Bob")], contributor=BOB)
    retry = service.append("d1", [put("/name", "Alice")], contributor=ALICE, request_key="K")
    assert retry.duplicate and retry.entries == first.entries
    assert retry.view is not None and retry.view.document["name"] == "Bob"  # the latest view, not a stale copy


def test_a_second_read_does_not_derive_again(backend: Storage) -> None:
    service = make_service(backend)
    service.append("d1", [put()], contributor=ALICE)
    service.get("d1")
    before = service.derivations
    for _ in range(3):
        assert service.get("d1").document["name"] == "Alice"
    assert service.derivations == before


def test_a_new_service_reads_the_same_bytes_from_the_stored_ledger(backend: Storage) -> None:
    service = make_service(backend)
    service.append("d1", [put("/age", 3), put("/tags", ("a", "b")), put("/name", "x")], contributor=ALICE)
    service.append("d1", [put("/tags", ("b", "c"))], contributor=BOB)
    warm = to_canonical(service.get("d1").document)
    fresh = make_service(backend)  # nothing cached in memory
    assert to_canonical(fresh.get("d1").document) == warm == '{age:3,name:"x",tags:["a","b","c"]}'


def test_incremental_and_full_derivation_agree_across_many_appends(backend: Storage) -> None:
    service = make_service(backend)
    for index in range(25):
        who, tier = [(JOB, "backfill"), (ALICE, "user"), (ROOT, "admin")][index % 3]
        service.append(
            "d1", [put("/age", index, tier=tier), put("/tags", (str(index % 4),), tier=tier)], contributor=who
        )
        incremental = to_canonical(service.get("d1").document)
        assert incremental == to_canonical(make_service(backend).get("d1").document)


def test_the_view_is_stored_and_dropped_by_an_append(backend: Storage) -> None:
    service = make_service(backend)
    service.append("d1", [put()], contributor=ALICE)
    service.get("d1")
    with backend.read() as tx:
        view = tx.get_view("d1")
    assert view is not None and view.watermark_seq == 1 and view.document == '{name:"Alice"}'
    service.append("d1", [put("/age", 1)], contributor=ALICE)  # also computes the new view for its result
    with backend.read() as tx:
        assert tx.get_view("d1").watermark_seq == 2  # type: ignore[union-attr]


def test_documents_do_not_leak_into_each_other(backend: Storage) -> None:
    service = make_service(backend)
    service.append("d1", [put("/name", "one")], contributor=ALICE)
    service.append("d2", [put("/name", "two")], contributor=ALICE)
    assert service.get("d1").document["name"] == "one" and service.get("d2").document["name"] == "two"


def test_pinned_and_changed_schemas_do_not_reuse_a_stale_state() -> None:
    service = make_service(MemoryStorage())
    service.append("d1", [put("/age", 1), put("/age", 5)], contributor=ALICE)
    assert service.get("d1").document["age"] == 5  # max
    from aduku.schema.loader import load_schema

    service.schemas.put(load_schema(schema_text(version=2, fields='"/age": {type: int, merge: lww}')))
    service.schemas.activate(2)
    assert service.get("d1").document["age"] == 5  # lww of 1 then 5 is also 5; use order to tell them apart
    service.append("d1", [put("/age", 2)], contributor=ALICE)
    assert service.get("d1").document["age"] == 2  # under version 2 the last write wins (max would say 5)


def test_defaults_come_from_the_schema_version_and_follow_it(backend: Storage) -> None:
    """SPEC 8.6: a default is not stored in the ledger, so changing it changes documents on the next read."""
    from aduku.schema.loader import load_schema

    fields = '"/plan": {type: string, merge: lww, default: "free"}, "/note": {type: string, merge: lww}'
    service = make_service(backend, schema=schema_text(fields=fields))
    service.append("d1", [put("/note", "hello")], contributor=ALICE)
    assert service.get("d1").document["plan"] == "free" and service.get("nobody").document["plan"] == "free"
    entries_before = service.list_entries("d1")
    service.schemas.put(load_schema(schema_text(version=2, fields=fields.replace('"free"', '"pro"'))))
    service.schemas.activate(2)
    assert service.get("d1").document["plan"] == "pro" and service.get("nobody").document["plan"] == "pro"
    assert service.list_entries("d1") == entries_before  # the ledger did not change
    service.append("d1", [put("/plan", "team")], contributor=ALICE)
    assert service.get("d1").document["plan"] == "team"
