"""Cache state and backend choice must never change what a reader sees (review round on defaults)."""

import sqlite3
from datetime import timedelta

import pytest

from aduku.engine.request_hash import request_hash
from aduku.errors import RejectedError, StorageError
from aduku.ion_codec import parse, to_canonical
from aduku.model import EntryInput
from aduku.storage.base import Storage
from aduku.storage.sqlite import SqliteStorage
from tests.service_helpers import ALICE, FIELDS, make_service, put, schema_text

STAMPED = schema_text(fields=FIELDS + ', "/t": {type: timestamp, merge: lww}')
OFFSET = parse("2026-01-01T10:00:00+05:00")


def test_a_fresh_view_and_a_cached_view_are_the_same_bytes(backend: Storage) -> None:
    service = make_service(backend, schema=STAMPED)
    appended = service.append("d1", [put("/t", OFFSET)], contributor=ALICE).view
    cached = service.get("d1")
    with backend.transaction() as tx:
        tx.drop_view("d1")
    derived = make_service(backend, schema=STAMPED).get("d1")  # nothing cached anywhere
    assert appended is not None
    texts = {to_canonical(r.document) for r in (appended, cached, derived)}
    assert texts == {"{displayName:computed::null,t:2026-01-01T05:00:00Z}"}
    assert all(r.document["t"].utcoffset() == timedelta(0) for r in (appended, cached, derived))


def test_both_backends_hold_the_same_value_for_the_same_entry(backend: Storage) -> None:
    service = make_service(backend, schema=STAMPED)
    service.append("d1", [put("/t", OFFSET, meta=OFFSET)], contributor=ALICE)
    (entry,) = service.list_entries("d1")
    assert to_canonical(entry.value) == "2026-01-01T05:00:00Z" and entry.value.utcoffset() == timedelta(0)
    assert to_canonical(entry.meta) == "2026-01-01T05:00:00Z"


def test_a_value_with_no_ion_form_never_hashes_like_a_stored_one() -> None:
    legit = request_hash("d1", "alice", False, [EntryInput("user", "/name", "set", "<bytes with no Ion form>")])
    blob = request_hash("d1", "alice", False, [EntryInput("user", "/name", "set", b"raw")])
    assert legit != blob
    assert blob == request_hash(
        "d1", "alice", False, [EntryInput("user", "/name", "set", b"raw")]
    )  # still deterministic


def test_a_request_with_a_blob_is_not_reported_as_a_duplicate_of_a_lookalike(backend: Storage) -> None:
    service = make_service(backend)
    service.append("d1", [put("/name", "<bytes with no Ion form>")], contributor=ALICE, request_key="K")
    with pytest.raises(RejectedError):
        service.append("d1", [put("/name", b"raw")], contributor=ALICE, request_key="K")
    assert [e.value for e in service.list_entries("d1")] == ["<bytes with no Ion form>"]


# -- per-schema work is done once --------------------------------------------------------------------------------------


def test_defaults_and_fallbacks_are_computed_once_per_schema_version(
    monkeypatch: pytest.MonkeyPatch, backend: Storage
) -> None:
    import aduku.engine.derive as derive_module

    calls = {"defaults": 0, "fallbacks": 0}
    real_defaults, real_fallbacks = derive_module.defaults_layer, derive_module.delete_fallbacks
    monkeypatch.setattr(
        derive_module,
        "defaults_layer",
        lambda ctx: calls.__setitem__("defaults", calls["defaults"] + 1) or real_defaults(ctx),
    )
    monkeypatch.setattr(
        derive_module,
        "delete_fallbacks",
        lambda ctx: calls.__setitem__("fallbacks", calls["fallbacks"] + 1) or real_fallbacks(ctx),
    )
    service = make_service(backend)
    for index in range(10):
        service.append("d1", [put("/age", index)], contributor=ALICE)
    service.append("d2", [put("/age", 1)], contributor=ALICE)
    service.get("d1")
    assert calls == {"defaults": 1, "fallbacks": 1}


def test_field_lookup_is_memoized_and_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    import aduku.schema.model as model
    from aduku.paths import Segment, parse_path
    from aduku.schema.loader import load_schema

    schema = load_schema(schema_text())
    calls: list[int] = []
    real = model.most_specific
    monkeypatch.setattr(model, "most_specific", lambda patterns, path: calls.append(1) or real(patterns, path))
    for _ in range(25):
        assert schema.field_for(parse_path("/age")) is not None  # loading already looked up /name, so use another
    assert len(calls) == 1
    monkeypatch.setattr(model, "FIELD_CACHE_LIMIT", 50)
    for number in range(300):  # keyed paths are unbounded in number; the memo must not grow with them
        schema.field_for((Segment("name", f"id={number}"),))
    assert len(schema._lookups) <= 50


# -- skipped lists everything left out ---------------------------------------------------------------------------------


def test_shadowed_and_invalid_entries_are_listed_as_skipped() -> None:
    from aduku.behaviors.registry import BehaviorRegistry
    from aduku.engine.derive import derive
    from aduku.schema.loader import load_schema
    from tests.helpers import make_entry

    schema = load_schema(schema_text(fields=FIELDS + ', "/total": {type: int, computed: {macro: sum, args: ["/age"]}}'))
    entries = [
        make_entry(seq=1, request_id="a", path="/total", value=5),  # the field is derived in this schema version
        make_entry(seq=2, request_id="b", path="not a path", value=1),  # no longer a valid path
        make_entry(seq=3, request_id="c", path="/name", value="kept"),
    ]
    derived = derive(schema, BehaviorRegistry.with_builtins(), entries)
    assert derived.state.skipped == (1, 2) and derived.document["name"] == "kept"


# -- a database that cannot be opened is not leaked ----------------------------------------------------------------------


def test_a_failed_open_closes_the_connection(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    opened: list[sqlite3.Connection] = []
    real = sqlite3.connect

    def spy(*args, **kwargs):  # type: ignore[no-untyped-def]
        connection = real(*args, **kwargs)
        opened.append(connection)
        return connection

    monkeypatch.setattr(sqlite3, "connect", spy)
    garbage = tmp_path / "not-a-database.db"
    garbage.write_bytes(b"this is not a sqlite database " * 40)
    with pytest.raises(StorageError):
        SqliteStorage(str(garbage))
    assert len(opened) == 1
    with pytest.raises(sqlite3.ProgrammingError):
        opened[0].execute("select 1")  # closed


# -- the audit log is counted and sliced by the store ------------------------------------------------------------------


def test_the_audit_summary_does_not_load_rows(monkeypatch: pytest.MonkeyPatch, backend: Storage) -> None:
    from tests.integration.test_rejection_audit import audit, reject

    service = make_service(backend, audit=audit())
    for _ in range(3):
        reject(service)
    audit_backend = backend.audit_backend()

    def boom(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise AssertionError("rows() must not be used to count")

    monkeypatch.setattr(type(audit_backend), "rows", boom)
    assert service.rejection_audit_summary("d1") == {"count": 3, "pruned": 0}


def test_the_store_applies_since_and_limit(backend: Storage) -> None:
    from tests.integration.test_rejection_audit import audit, reject

    service = make_service(backend, audit=audit())
    for _ in range(5):
        reject(service)
    store = backend.audit_backend()
    everything = store.rows("d1")
    assert len(everything) == 5 and store.count("d1") == 5 and store.count("other") == 0
    assert [r.id for r in store.rows("d1", limit=2)] == [r.id for r in everything[-2:]]  # the newest two, oldest first
    assert [r.id for r in store.rows("d1", since=everything[3].recorded_at)] == [r.id for r in everything[3:]]
    assert [r.id for r in store.rows("d1", since=everything[1].recorded_at, limit=2)] == [r.id for r in everything[-2:]]


# -- a timestamp that cannot be written in UTC is refused, never a crash -------------------------------------------------


@pytest.mark.parametrize("text", ["0001-01-01T00:00:00+05:00", "9999-12-31T23:59:59-05:00"])
def test_a_timestamp_outside_the_utc_range_is_rejected_at_admission(backend: Storage, text: str) -> None:
    service = make_service(backend, schema=STAMPED)
    with pytest.raises(RejectedError) as caught:
        service.append("d1", [put("/t", parse(text))], contributor=ALICE, request_key="K")
    assert caught.value.code.value == "TYPE_MISMATCH" and service.list_entries("d1") == []
