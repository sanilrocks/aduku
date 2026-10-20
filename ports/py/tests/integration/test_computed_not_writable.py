"""US2: no contribution can set or delete a computed field, or anything beneath it (SPEC 9.2, FR-007, FR-008, FR-009)."""

import pytest

from aduku.errors import ErrorCode, RejectedError
from aduku.ion_codec import to_canonical
from aduku.model import EntryInput, Identity
from aduku.schema.loader import load_schema
from aduku.storage.base import Storage
from tests.service_helpers import ALICE, FIELDS, JOB, ROOT, delete, make_service, put, schema_text

IMPURE = (
    '"/taxId": {type: string, computed: {impure: {hook: "fx.echo@1", depends: ["/country"], reason: "registry",'
    ' timeout: "1s", retry: {max: 1, backoff: "10ms"}, deadline: "3s", recompute_by: [admin]}}}'
)
FIELDS2 = FIELDS + ', "/country": {type: string, merge: lww}, ' + IMPURE
AUDIT = "rejection_audit: {enabled: true, mode: full, retention: {max_entries: 10}},"


def svc(backend: Storage, audit: str = "", **options):  # type: ignore[no-untyped-def]
    return make_service(backend, schema=schema_text(audit=audit, fields=FIELDS2), **options)


def untouched(service) -> bool:  # type: ignore[no-untyped-def]
    return service.list_entries("d1") == [] and service.last_seq("d1") == 0


@pytest.mark.parametrize("path", ["/displayName", "/taxId"])
def test_1_a_set_on_a_computed_path_is_rejected_and_nothing_changes(backend: Storage, path: str) -> None:
    service = svc(backend)
    service.append("d1", [put("/name", "Alice")], contributor=ALICE)
    before = service.get("d1")
    with pytest.raises(RejectedError) as caught:
        service.append("d1", [put(path, "HACKED", tier="admin")], contributor=ROOT)
    assert caught.value.code is ErrorCode.FIELD_NOT_WRITABLE
    assert [r.path for r in caught.value.reasons] == [path]
    assert service.get("d1") == before and service.last_seq("d1") == 1
    service.append("d1", [put("/name", "Bob")], contributor=ALICE)  # the next seq is 2: the rejection consumed none
    assert [e.seq for e in service.list_entries("d1")] == [1, 2]


def test_2_a_delete_and_a_child_path_are_rejected_in_the_same_way(backend: Storage) -> None:
    service = svc(backend)
    for entry in (delete("/displayName", tier="admin"), put("/displayName/inner", 1, tier="admin"), delete("/taxId")):
        with pytest.raises(RejectedError) as caught:
            service.append("d1", [entry], contributor=ROOT)
        assert caught.value.code is ErrorCode.FIELD_NOT_WRITABLE
    assert untouched(service)


def test_3_the_non_rejection_flag_does_not_bypass_it(backend: Storage) -> None:
    service = svc(backend)
    with pytest.raises(RejectedError) as caught:
        service.append("d1", [put("/displayName", "HACKED", tier="backfill")], contributor=JOB, lenient=True)
    assert caught.value.code is ErrorCode.FIELD_NOT_WRITABLE and untouched(service)


def test_4_a_request_with_a_valid_entry_and_a_computed_one_is_rejected_whole(backend: Storage) -> None:
    service = svc(backend)
    with pytest.raises(RejectedError) as caught:
        service.append("d1", [put("/name", "Alice"), put("/displayName", "HACKED", tier="admin")], contributor=ROOT)
    assert caught.value.code is ErrorCode.FIELD_NOT_WRITABLE and untouched(service)


def test_5_no_tier_or_identity_gets_through(backend: Storage) -> None:
    service = svc(backend)
    everything = Identity("god", ("backfill", "user", "admin"), ("admin",))
    for tier in ("backfill", "user", "admin"):
        with pytest.raises(RejectedError) as caught:
            service.append("d1", [put("/displayName", "x", tier=tier)], contributor=everything, lenient=True)
        assert caught.value.code is ErrorCode.FIELD_NOT_WRITABLE
    assert untouched(service)


def test_6_auditing_on_keeps_exactly_one_record_off_keeps_none(backend: Storage) -> None:
    # two services on one storage whose schemas differ under the same version: each keeps its own choice (feature 006)
    on, off = svc(backend, AUDIT, schema_state=False), svc(backend, schema_state=False)
    for service in (on, off):
        with pytest.raises(RejectedError):
            service.append("d1", [put("/displayName", "HACKED", tier="admin")], contributor=ROOT)
    assert len(on.list_rejections("d1")) == 1 and untouched(on)
    # `off` shares the storage with `on` but its schema has auditing off: it never adds a record
    assert len(off.list_rejections("d1")) == 1


def test_7_entries_for_a_field_that_later_becomes_computed_are_kept_but_ignored(backend: Storage) -> None:
    old = load_schema(schema_text(fields='"/name": {type: string, merge: lww}, "/shout": {type: string, merge: lww}'))
    new = load_schema(
        schema_text(
            version=2,
            fields='"/name": {type: string, merge: lww}, "/shout": {type: string, computed: {macro: upper, args: ["/name"]}}',
        )
    )
    service = make_service(
        backend, schema=schema_text(fields='"/name": {type: string, merge: lww}, "/shout": {type: string, merge: lww}')
    )
    assert old.version == 1
    service.append("d1", [put("/name", "pat"), put("/shout", "stale")], contributor=ALICE)
    service.schemas.put(new)
    service.schemas.activate(2)
    assert [e.path for e in service.list_entries("d1")] == ["/name", "/shout"]  # kept in the ledger
    document = service.get("d1")
    assert "stale" not in to_canonical(document.document)
    assert document.skipped == (2,)
    with pytest.raises(RejectedError) as caught:
        service.append("d1", [EntryInput("user", "/shout", "set", "again")], contributor=ALICE)
    assert caught.value.code is ErrorCode.FIELD_NOT_WRITABLE
