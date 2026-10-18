"""FR-027, FR-028, SC-010, SC-011, SC-013: the optional, capped, audit-only rejection log."""

import sqlite3
from datetime import timedelta

import pytest

from aduku.errors import RejectedError, SchemaError
from aduku.schema.loader import load_schema
from aduku.storage.base import FakeClock, Storage
from aduku.storage.sqlite import SqliteStorage
from tests.service_helpers import ALICE, JOB, make_service, put, schema_text


def audit(enabled: bool = True, mode: str = "full", retention: str = "max_entries: 100") -> str:
    return f"rejection_audit: {{enabled: {str(enabled).lower()}, mode: {mode}, retention: {{{retention}}}}},"


def reject(service, doc: str = "d1", path: str = "/nope", value: object = "x", who=ALICE) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(RejectedError):
        service.append(doc, [put(path, value)], contributor=who)


def test_off_by_default_leaves_no_record(backend: Storage) -> None:
    service = make_service(backend)
    reject(service)
    assert service.list_rejections("d1") == []


def test_off_leaves_no_record_even_with_retention_configured(backend: Storage) -> None:
    service = make_service(backend, audit=audit(enabled=False))
    reject(service)
    assert service.list_rejections("d1") == []


def test_on_writes_exactly_one_record_per_rejected_request(backend: Storage) -> None:
    service = make_service(backend, audit=audit())
    with pytest.raises(RejectedError):
        service.append("d1", [put("/nope", "a"), put("/age", "x")], contributor=ALICE, lenient=False)
    records = service.list_rejections("d1")
    assert len(records) == 1
    record = records[0]
    assert (record.doc, record.contributor, record.tiers, record.lenient, record.schema_version) == (
        "d1",
        "alice",
        ("user",),
        False,
        1,
    )
    assert [(e["path"], e["op"], e["value"]) for e in record.attempted] == [("/nope", "set", "a"), ("/age", "set", "x")]
    assert [r["code"] for r in record.reasons] == ["UNKNOWN_PATH", "TYPE_MISMATCH"]
    assert record.recorded_at is not None


def test_metadata_only_keeps_paths_operations_and_reasons_but_no_values(backend: Storage) -> None:
    service = make_service(backend, audit=audit(mode="metadata_only"))
    reject(service, path="/age", value="SECRETVALUE")
    reject(service, path="/nope", value="SECRETVALUE")
    for record in service.list_rejections("d1"):
        assert "SECRETVALUE" not in repr(record)
        assert all(set(e) == {"tier", "path", "op"} for e in record.attempted)
        assert record.reasons


def test_accepted_requests_and_duplicates_are_not_audited(backend: Storage) -> None:
    service = make_service(backend, audit=audit())
    service.append("d1", [put()], contributor=ALICE, request_key="K")
    service.append("d1", [put()], contributor=ALICE, request_key="K")
    assert service.list_rejections("d1") == []


def test_rejected_requests_consume_no_sequence_numbers(backend: Storage) -> None:
    service = make_service(backend, audit=audit())
    reject(service)
    reject(service)
    assert [seq for _, seq in service.append("d1", [put()], contributor=ALICE).entries] == [1]


def test_list_filters(backend: Storage) -> None:
    clock = FakeClock(step_seconds=60)
    service = make_service(backend, audit=audit(), clock=clock)
    for _ in range(4):
        reject(service)
    everything = service.list_rejections("d1")
    assert len(everything) == 4
    assert [r.recorded_at for r in everything] == sorted(r.recorded_at for r in everything)
    assert service.list_rejections("d1", limit=2) == everything[-2:]
    assert service.list_rejections("d1", since=everything[2].recorded_at) == everything[2:]
    assert service.list_rejections("other") == []


def test_max_entries_prunes_oldest_first_and_counts_them(backend: Storage) -> None:
    service = make_service(backend, audit=audit(retention="max_entries: 3"))
    for index in range(5):
        reject(service, value=f"v{index}", path="/age")
    kept = service.list_rejections("d1")
    assert [e["value"] for r in kept for e in r.attempted] == ["v2", "v3", "v4"]
    assert service.rejection_audit_summary("d1") == {"count": 3, "pruned": 2}


def test_max_age_prunes_old_records(backend: Storage) -> None:
    clock = FakeClock(step_seconds=0)
    service = make_service(backend, audit=audit(retention='max_age: "1h"'), clock=clock)
    reject(service, value="old", path="/age")
    clock.set(clock.now() + timedelta(minutes=30))
    reject(service, value="mid", path="/age")
    clock.set(clock.now() + timedelta(minutes=45))  # "old" is now 75 minutes old
    reject(service, value="new", path="/age")
    assert [e["value"] for r in service.list_rejections("d1") for e in r.attempted] == ["mid", "new"]
    clock.set(clock.now() + timedelta(hours=3))
    assert service.prune_rejections("d1") == 2
    assert service.list_rejections("d1") == []
    assert service.rejection_audit_summary("d1")["pruned"] == 3


def test_whichever_limit_is_exceeded_first_prunes(backend: Storage) -> None:
    clock = FakeClock(step_seconds=0)
    service = make_service(backend, audit=audit(retention='max_entries: 2, max_age: "1h"'), clock=clock)
    for index in range(3):
        reject(service, value=f"v{index}", path="/age")
    assert len(service.list_rejections("d1")) == 2
    clock.set(clock.now() + timedelta(hours=2))
    assert service.prune_rejections() == 2  # all documents


def test_per_document_override(backend: Storage) -> None:
    service = make_service(backend, audit=audit(enabled=False))
    service.set_rejection_audit("d1", "enabled")
    reject(service, "d1")
    reject(service, "d2")
    assert len(service.list_rejections("d1")) == 1 and service.list_rejections("d2") == []
    service.set_rejection_audit("d1", "disabled")
    reject(service, "d1")
    assert len(service.list_rejections("d1")) == 1
    on = make_service(backend, audit=audit(enabled=True))
    on.set_rejection_audit("d3", "disabled")
    reject(on, "d3")
    assert on.list_rejections("d3") == []
    on.set_rejection_audit("d3", "inherit")
    reject(on, "d3")
    assert len(on.list_rejections("d3")) == 1


def test_an_override_cannot_enable_auditing_without_a_retention_limit(backend: Storage) -> None:
    service = make_service(backend)  # no rejection_audit section at all
    with pytest.raises(SchemaError):
        service.set_rejection_audit("d1", "enabled")


def test_a_schema_enabling_auditing_without_retention_is_refused() -> None:
    with pytest.raises(SchemaError) as caught:
        load_schema(schema_text(audit="rejection_audit: {enabled: true},"))
    assert "retention" in " ".join(caught.value.problems)
    load_schema(schema_text(audit="rejection_audit: {enabled: false},"))  # off needs no limit


def test_an_audit_write_failure_does_not_change_the_rejection(tmp_path) -> None:  # type: ignore[no-untyped-def]
    path = str(tmp_path / "a.db")
    storage = SqliteStorage(path)
    service = make_service(storage, audit=audit())
    storage.connection.execute("DROP TABLE rejection_audit")
    with pytest.raises(RejectedError) as caught:
        service.append("d1", [put("/nope")], contributor=ALICE)
    assert caught.value.code.value == "UNKNOWN_PATH"
    storage.close()


def test_dropping_the_audit_log_changes_no_document(tmp_path) -> None:  # type: ignore[no-untyped-def]
    path = str(tmp_path / "b.db")
    storage = SqliteStorage(path)
    service = make_service(storage, audit=audit())
    service.append("d1", [put(), put("/age", 3)], contributor=ALICE)
    reject(service)
    before = (service.list_entries("d1"), service.last_seq("d1"))
    raw = sqlite3.connect(path, isolation_level=None)
    raw.execute("DROP TABLE rejection_audit")
    raw.close()
    assert (service.list_entries("d1"), service.last_seq("d1")) == before
    assert [seq for _, seq in service.append("d1", [put("/age", 4)], contributor=ALICE).entries] == [3]
    storage.close()


def test_audit_code_is_never_imported_by_the_engine() -> None:
    import ast
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[2] / "src" / "aduku" / "engine"
    for path in root.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.ImportFrom) and node.module:
                assert not node.module.endswith("audit"), path
            if isinstance(node, ast.Import):
                assert not any(alias.name.endswith("audit") for alias in node.names), path


def test_job_tier_rejection_records_its_tiers(backend: Storage) -> None:
    service = make_service(backend, audit=audit())
    with pytest.raises(RejectedError):
        service.append("d1", [put(tier="user")], contributor=JOB)
    assert service.list_rejections("d1")[0].tiers == ("backfill",)


def test_a_rejection_for_an_unstorable_value_is_still_audited(backend: Storage) -> None:
    service = make_service(backend, audit=audit())
    reject(service, path="/name", value=b"raw-secret")
    (record,) = service.list_rejections("d1")
    assert record.reasons[0]["code"] == "TYPE_MISMATCH"
    assert "raw-secret" not in repr(record)
    assert "bytes" in repr(record.attempted[0]["value"])


def test_a_global_prune_uses_each_documents_own_schema_version(backend: Storage) -> None:
    from aduku.schema.loader import load_schema

    service = make_service(backend, audit=audit(retention="max_entries: 1000"))  # version 1
    service.schemas.put(load_schema(schema_text(audit=audit(retention="max_entries: 2"), version=2)))
    service.schemas.pin("pinned", 1)
    service.schemas.activate(2)
    for _ in range(4):
        reject(service, "pinned", path="/age", value="x")
    assert len(service.list_rejections("pinned")) == 4  # kept: its schema allows 1000
    assert service.prune_rejections() == 0
    assert len(service.list_rejections("pinned")) == 4
    for _ in range(4):
        reject(service, "plain", path="/age", value="x")
    assert len(service.list_rejections("plain")) == 2  # pruned at write under version 2


def test_the_audit_log_is_safe_to_use_while_other_threads_write(tmp_path) -> None:  # type: ignore[no-untyped-def]
    import threading

    storage = SqliteStorage(str(tmp_path / "busy.db"))
    service = make_service(storage, audit=audit(retention="max_entries: 20"))
    failures: list[BaseException] = []

    def guarded(work) -> None:  # type: ignore[no-untyped-def]
        try:
            for index in range(40):
                work(index)
        except BaseException as error:  # noqa: BLE001
            failures.append(error)

    def rejecting(index: int) -> None:
        with pytest.raises(RejectedError):
            service.append("d1", [put("/nope", index)], contributor=ALICE)

    def appending(index: int) -> None:
        service.append("d2", [put("/age", index)], contributor=ALICE)

    def reading(index: int) -> None:
        assert len(service.list_rejections("d1")) <= 20
        service.rejection_audit_summary("d1")
        service.prune_rejections("d1")

    threads = [threading.Thread(target=guarded, args=(work,)) for work in (rejecting, appending, reading, reading)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert failures == []
    storage.close()


def test_an_override_does_not_keep_auditing_once_the_schema_has_no_retention_limit(backend: Storage) -> None:
    from aduku.schema.loader import load_schema

    service = make_service(backend, audit=audit(enabled=False))
    service.set_rejection_audit("d1", "enabled")
    reject(service, "d1")
    assert len(service.list_rejections("d1")) == 1
    service.schemas.put(load_schema(schema_text(version=2)))  # version 2 has no rejection_audit section at all
    service.schemas.activate(2)
    for _ in range(3):
        reject(service, "d1")
    assert len(service.list_rejections("d1")) == 1  # no unbounded growth: nothing more is recorded


def test_recording_and_pruning_are_one_unit(tmp_path) -> None:  # type: ignore[no-untyped-def]
    storage = SqliteStorage(str(tmp_path / "atomic.db"))
    service = make_service(storage, audit=audit(retention="max_entries: 2"))
    for index in range(3):
        reject(service, path="/age", value=f"v{index}")
    assert service.rejection_audit_summary("d1") == {"count": 2, "pruned": 1}
    backend = storage.audit_backend()
    original = type(backend).add_pruned

    def failing(self, doc, count):  # type: ignore[no-untyped-def]
        raise RuntimeError("crash between the delete and the counter")

    type(backend).add_pruned = failing  # type: ignore[method-assign,assignment]
    try:
        reject(service, path="/age", value="v3")  # would insert, prune one, then count it: the count fails
    finally:
        type(backend).add_pruned = original  # type: ignore[method-assign]
    # the audit write failed as a whole: nothing was half done
    assert service.rejection_audit_summary("d1") == {"count": 2, "pruned": 1}
    assert [e["value"] for r in service.list_rejections("d1") for e in r.attempted] == ["v1", "v2"]
    storage.close()
