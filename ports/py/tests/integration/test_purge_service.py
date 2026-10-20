"""Feature 005, user story 5: a purge erases what it names from every store, keeps the record, and logs it without values."""

import logging

import pytest

from aduku.errors import ErrorCode, RejectedError
from aduku.model import Identity, PurgeScope
from aduku.storage.base import Storage
from aduku.storage.memory import MemoryStorage
from aduku.storage.sqlite import SqliteStorage
from tests.deferred_helpers import build, deferred
from tests.service_helpers import ALICE, JOB, ROOT, delete, put

MARK = "secret-marker"
IMPURE = (
    '"/taxId": {type: string, computed: {impure: {hook: "fx.echo@1", depends: ["/country"], reason: "registry",'
    ' timeout: "1s", retry: {max: 1, backoff: "10ms"}, deadline: "3s", recompute_by: [admin]}}}'
)
FIELDS = f'"/creditScore": {deferred()}, {IMPURE}, "/address": {{type: struct, merge: lww}}'
TOP = "purge_by: [dpo], roles: [dpo], rejection_audit: {enabled: true, mode: full, retention: {max_entries: 9}},"
DPO = Identity("officer", ("admin",), ("dpo",))
NOBODY = Identity("intern", ("user",), ())


def stored_text(storage: Storage) -> str:
    """Every stored value of every table, as text (to look for a marker)."""
    if isinstance(storage, SqliteStorage):
        parts = []
        for (table,) in storage.connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall():
            parts.extend(str(tuple(row)) for row in storage.connection.execute(f"SELECT * FROM {table}"))
        return "\n".join(parts).lower()
    assert isinstance(storage, MemoryStorage)
    audit = storage.audit_backend()
    return (repr(storage._state) + repr(audit._rows)).lower()  # noqa: SLF001


def world(backend: Storage):  # type: ignore[no-untyped-def]
    built = build(backend, FIELDS, top=TOP)
    db = built.service
    db.append("d1", [put("/name", "ann", "backfill")], contributor=JOB)
    db.append("d1", [put("/name", MARK), put("/country", MARK)], contributor=ALICE, request_key="RK")
    db.append("d1", [put("/name", MARK + "-admin", "admin")], contributor=ROOT)
    with pytest.raises(RejectedError):
        db.append(
            "d1", [put("/name", MARK, "backfill")], contributor=ALICE
        )  # a rejected attempt, kept by the audit log
    db.append("d1", [put("/name", "later")], contributor=ALICE)
    db.get("d1")  # fetches the deferred value and fills the impure one
    db.append("other", [put("/name", "ann")], contributor=ALICE)
    return built, db


def test_the_marker_is_everywhere_before_and_nowhere_after(backend: Storage) -> None:
    built, db = world(backend)
    assert MARK in stored_text(backend) and db.get("d1").document["shout"].value == (MARK + "-admin").upper()
    db.compact("d1", through_seq=3)
    other_before = db.get("other").document
    db.purge("d1", PurgeScope(path="/name"), reason="ticket 1", caller=DPO)
    db.purge("d1", PurgeScope(path="/country"), reason="ticket 2", caller=DPO)
    assert MARK not in stored_text(backend)
    with backend.read() as tx:
        assert tx.get_derived("d1") == [] or all(MARK not in str(r) for r in tx.get_derived("d1"))
        assert [r.reason for r in tx.purge_log("d1")] == ["ticket 1", "ticket 2"]
    assert db.get("other").document == other_before  # another document is not touched
    assert built.stored("other") is not None


def test_the_document_is_what_the_remaining_contributions_give(backend: Storage) -> None:
    _, db = world(backend)
    report = db.purge("d1", PurgeScope(path="/name", tier="user"), reason="ticket", caller=DPO)
    assert report.entries_erased == 2  # the user entries at /name
    assert db.get("d1").document["name"] == MARK + "-admin"
    db.purge("d1", PurgeScope(path="/name"), reason="ticket", caller=DPO)
    assert (
        "name" not in db.get("d1").document and db.get("d1").document["shout"].value is None
    )  # nothing to derive from
    entries = db.list_entries("d1")
    assert [e.purged for e in entries].count(True) == 4 and [e.seq for e in entries] == list(range(1, len(entries) + 1))


def test_computed_and_impure_values_are_recomputed_without_the_erased_input(backend: Storage) -> None:
    built, db = world(backend)
    assert db.get("d1").document["taxId"].value == f"tax-{MARK}"
    calls = built.calls("fx.echo@1")
    db.purge("d1", PurgeScope(path="/country"), reason="ticket", caller=DPO)
    assert "taxId" not in db.get("d1").document or MARK not in str(db.get("d1").document["taxId"])
    assert built.calls("fx.echo@1") == calls  # no input, no call; the old outcome is gone from the log
    with backend.read() as tx:
        assert [r for r in tx.list_effects("d1") if r.field == "/taxId"] == []


def test_a_struct_written_above_the_path_keeps_the_rest(backend: Storage) -> None:
    from aduku.ion_codec import FrozenStruct

    _, db = world(backend)
    db.append("d1", [put("/address", FrozenStruct({"city": "addr-marker", "zip": "1"}))], contributor=ALICE)
    assert "addr-marker" in stored_text(backend)
    with pytest.raises(RejectedError):  # /address/city is not declared by this schema: say so and change nothing
        db.purge("d1", PurgeScope(path="/address/city"), reason="ticket", caller=DPO)
    report = db.purge("d1", PurgeScope(path="/address"), reason="ticket", caller=DPO)
    assert report.entries_erased == 1 and "addr-marker" not in stored_text(backend)


def test_unauthorized_or_reasonless_purges_change_nothing(backend: Storage) -> None:
    _, db = world(backend)
    before = stored_text(backend)
    for caller, reason, code in (
        (NOBODY, "ticket", ErrorCode.NOT_AUTHORIZED),
        (DPO, "  ", ErrorCode.PURGE_REASON_REQUIRED),
    ):
        with pytest.raises(RejectedError) as caught:
            db.purge("d1", PurgeScope(path="/name"), reason=reason, caller=caller)
        assert caught.value.code is code
    for scope, code in (
        (PurgeScope(path="/nope"), ErrorCode.UNKNOWN_PATH),
        (PurgeScope(path="/shout"), ErrorCode.FIELD_NOT_WRITABLE),
        (PurgeScope(entries=("no-such-entry",)), ErrorCode.UNKNOWN_ENTRY),
        (PurgeScope(path="/name", tier="ghost"), ErrorCode.TIER_NOT_PERMITTED),
    ):
        with pytest.raises(RejectedError) as caught:
            db.purge("d1", scope, reason="ticket", caller=DPO)
        assert caught.value.code is code
    for bad in (PurgeScope(), PurgeScope(path="/name", entries=("x",))):
        with pytest.raises(ValueError):
            db.purge("d1", bad, reason="ticket", caller=DPO)
    assert stored_text(backend) == before and db.purge_log("d1") == []


def test_explain_names_the_purge_and_never_a_purged_entry_as_winner(backend: Storage) -> None:
    _, db = world(backend)
    first = db.list_entries("d1")[1]
    db.purge("d1", PurgeScope(entries=(first.id,)), reason="ticket 9", caller=DPO)
    why = db.explain("d1", "/name")
    assert [(m.entry_id, m.seq, m.reason) for m in why.purged] == [(first.id, first.seq, "ticket 9")]
    assert (
        first.id not in (why.winner, *why.inputs, *why.shadowed) and why.purged[0].purged_at == db.purge_log("d1")[0].at
    )
    assert db.explain("d1", "/currency").purged == ()


def test_a_retried_request_is_still_a_duplicate_with_its_original_answer(backend: Storage) -> None:
    _, db = world(backend)
    original = db.append("d1", [put("/name", MARK), put("/country", MARK)], contributor=ALICE, request_key="RK")
    assert original.duplicate
    db.purge("d1", PurgeScope(path="/name"), reason="ticket", caller=DPO)
    again = db.append("d1", [put("/name", MARK), put("/country", MARK)], contributor=ALICE, request_key="RK")
    assert again.duplicate and again.entries == original.entries and again.request_id == original.request_id
    with backend.read() as tx:
        assert [e.purged for e in tx.entries("d1") if e.request_id == original.request_id] == [True, False]
        assert tx.find_request("d1", "alice", "RK").request_hash is None  # type: ignore[union-attr]


def test_a_tombstone_is_an_entry_and_a_purge_leaves_no_value(backend: Storage) -> None:
    _, db = world(backend)
    db.append("d1", [delete("/name", "admin")], contributor=ROOT)
    db.purge("d1", PurgeScope(path="/name"), reason="ticket", caller=DPO)
    db.purge("d1", PurgeScope(path="/country"), reason="ticket", caller=DPO)
    assert MARK not in stored_text(backend) and "name" not in db.get("d1").document
    assert len(db.purge_log("d1")) == 2 and {r.actor for r in db.purge_log("d1")} == {"officer"}


def test_an_expired_archive_never_blocks_erasure_and_the_log_says_what_was_lost(backend: Storage) -> None:
    _, db = world(backend)
    db.compact("d1", through_seq=3)
    db.expire_archive("d1", through_seq=3)
    report = db.purge("d1", PurgeScope(path="/name"), reason="ticket", caller=DPO)
    assert report.expired_sources is True and "expired_sources:true" in db.purge_log("d1")[0].counts.replace(" ", "")
    db.purge("d1", PurgeScope(path="/country"), reason="ticket", caller=DPO)
    assert MARK not in stored_text(backend)
    with pytest.raises(RejectedError) as caught:
        db.purge("d1", PurgeScope(entries=(db.list_entries("d1")[0].id,)), reason="ticket", caller=DPO)
    assert caught.value.code is ErrorCode.UNKNOWN_ENTRY  # a synthetic entry is purged by path, not by id


def test_nothing_logs_a_value(backend: Storage, caplog: pytest.LogCaptureFixture) -> None:
    _, db = world(backend)
    with caplog.at_level(logging.INFO, logger="aduku"):
        db.purge("d1", PurgeScope(path="/name"), reason="ticket", caller=DPO)
    assert any('"event": "purge"' in r.message for r in caplog.records)
    assert all(MARK not in r.message for r in caplog.records)
    assert MARK not in " ".join(r.counts for r in db.purge_log("d1"))


def test_rebuild_after_a_purge_finds_no_difference(backend: Storage) -> None:
    _, db = world(backend)
    db.purge("d1", PurgeScope(path="/name"), reason="ticket", caller=DPO)
    report = db.rebuild()
    assert report.identical and report.failed == ()


def test_review_3_a_state_derived_before_a_purge_is_never_reused_after_it(backend: Storage) -> None:
    _, db = world(backend)
    db.get("d1")
    stale = db._states["d1"]  # noqa: SLF001 - what a reader that started before the purge would remember
    db.purge("d1", PurgeScope(path="/name"), reason="ticket", caller=DPO)
    db.get("d1")  # another reader loads after the purge
    db._remember("d1", stale)  # noqa: SLF001 - and the slow reader finishes last
    db.append("d1", [put("/currency", "EUR")], contributor=ALICE)
    document = db.get("d1").document
    assert (
        "name" not in document and document["shout"].value is None
    )  # nothing of the erased name came back from the stale state


def test_review_7_explain_as_of_says_whether_the_originals_were_used_like_a_live_explain(backend: Storage) -> None:
    from aduku.model import AsOf

    _, db = world(backend)
    db.compact("d1", through_seq=3)
    live = db.explain("d1", "/name")
    past = db.explain("d1", "/name", as_of=AsOf(seq=db.last_seq("d1")))
    assert live.originals_kept is True and past.originals_kept is True
