"""Feature 005, user story 4: compaction changes no answer (scenarios 1 to 7 and 9 of the spec)."""

import threading
from dataclasses import replace
from datetime import timedelta

import pytest

from aduku.errors import ErrorCode, RejectedError
from aduku.storage.base import SequentialIds, Storage
from tests.service_helpers import ALICE, FIELDS, JOB, ROOT, make_service, put, schema_text
from tests.snapshot_helpers import restart

COUNTER = FIELDS + ', "/n": {type: int, merge: "acme.add@1"}'


def build(backend: Storage, **kw):  # type: ignore[no-untyped-def]
    return make_service(backend, schema=schema_text(fields=COUNTER), **kw)


def observe(db, doc: str = "d"):  # type: ignore[no-untyped-def]
    result = db.get(doc)
    paths = ("/name", "/age", "/tags", "/n", "/plan", "/displayName")
    return (
        result.document, result.model_errors, result.status, result.skipped, result.conflicts,
        {p: replace(db.explain(doc, p), originals_kept=None, sources=()) for p in paths},
    )  # fmt: skip


def seed(db, doc: str = "d", rounds: int = 6, tag: str = "K"):  # type: ignore[no-untyped-def]
    for i in range(rounds):
        db.append(doc, [put("/name", f"u{i}"), put("/age", i)], contributor=ALICE)
        db.append(doc, [put("/name", f"b{i}", "backfill"), put("/tags", (f"t{i % 3}",), "backfill")], contributor=JOB)
        db.append(doc, [put("/n", 1, "user", idem_key=f"{tag}{i}")], contributor=ALICE)
    db.append(doc, [put("/plan", "pro", "admin")], contributor=ROOT)


def test_compacting_changes_no_answer_and_leaves_one_entry_per_tier(backend: Storage) -> None:
    db = build(backend)
    seed(db)
    before = observe(db)
    live_before = db.list_entries("d")
    report = db.compact("d", through_seq=db.last_seq("d"))
    assert not report.noop and report.watermark_seq == db.last_seq("d") and report.replaced == len(live_before)
    entries = db.list_entries("d")
    assert len(entries) == report.synthetic == 3 and {e.synthetic for e in entries} == {report.compaction_id}
    assert all(e.op == "layer" for e in entries) and db.last_seq("d") == live_before[-1].seq
    assert observe(db) == before  # the archive keeps the originals, so even explain is exactly the old answer
    assert observe(restart(db)) == before
    assert db.verify_archive("d").checked and db.verify_archive("d").differences == ()


def test_size_after_compacting_ten_thousand_entries_on_twenty_fields(backend: Storage) -> None:
    from tests.snapshot_helpers import bulk_history

    fields = ", ".join(f'"/f{i}": {{type: int, merge: max}}' for i in range(20))
    db = make_service(backend, schema=schema_text(fields=fields))
    from aduku.model import Entry
    from aduku.storage.base import RequestRecord
    from tests.helpers import T0

    with backend.transaction() as tx:
        rows = [
            Entry(f"b{i}", "big", i, f"q{i // 50}", ("backfill", "user", "admin")[i % 3], f"/f{i % 20}", "set", i,
                  "bulk", T0 + timedelta(seconds=i), 1)
            for i in range(1, 10_001)
        ]  # fmt: skip
        for start in range(0, len(rows), 50):
            chunk = rows[start : start + 50]
            tx.append_request(RequestRecord(chunk[0].request_id, "big", "bulk"), chunk)
    before = db.get("big").document
    db.append("big", [put("/f0", 99_999, "user")], contributor=ALICE)
    # the explain comparison is quadratic in the entries per path (about 30 s here): the size claim does not need it, and
    # the same comparison runs on every other test of this file and on the 1,000-entry document of the next one
    report = db.compact("big", through_seq=10_000, verify=False)
    assert len(db.list_entries("big")) <= 60 + 1 and report.synthetic == 3
    assert db.get("big").document["f0"] == 99_999 and before["f1"] == db.get("big").document["f1"]
    _ = bulk_history


def test_a_thousand_entries_on_twenty_fields_are_compared_path_by_path_in_seconds(backend: Storage) -> None:
    import time

    from aduku.model import Entry
    from aduku.storage.base import RequestRecord
    from tests.helpers import T0

    fields = ", ".join(f'"/f{i}": {{type: int, merge: max}}' for i in range(20))
    db = make_service(backend, schema=schema_text(fields=fields))
    with backend.transaction() as tx:
        rows = [
            Entry(f"b{i}", "mid", i, f"q{i // 50}", ("backfill", "user", "admin")[i % 3], f"/f{i % 20}", "set", i % 97,
                  "bulk", T0 + timedelta(seconds=i), 1)
            for i in range(1, 1001)
        ]  # fmt: skip
        for start in range(0, len(rows), 50):
            chunk = rows[start : start + 50]
            tx.append_request(RequestRecord(chunk[0].request_id, "mid", "bulk"), chunk)
    before = {p: db.explain("mid", p).value for p in (f"/f{i}" for i in range(20))}
    started = time.perf_counter()
    report = db.compact("mid", through_seq=1000)
    assert report.checked_paths == 20 and time.perf_counter() - started < 20
    assert {p: db.explain("mid", p).value for p in before} == before


def test_tombstones_and_recreations_survive(backend: Storage) -> None:
    from tests.service_helpers import delete

    steps = [
        ([put("/name", "a"), put("/age", 5)], ALICE),
        ([delete("/name", "user")], ALICE),
        ([put("/age", 2, "admin")], ROOT),
        ([put("/name", "z", "backfill")], JOB),
        ([put("/name", "again")], ALICE),
        ([delete("/age", "admin")], ROOT),
    ]
    db = build(backend)
    for entries, who in steps[:4]:
        db.append("d", entries, contributor=who)
    before = observe(db)
    db.compact("d", through_seq=db.last_seq("d"))
    assert observe(db) == before
    for entries, who in steps[4:]:  # later writes behave as against the originals
        db.append("d", entries, contributor=who)
    reference = build(backend, ids=SequentialIds("ref"))
    for entries, who in steps:
        reference.append("ref", entries, contributor=who)
    assert db.get("d").document == reference.get("ref").document


def test_keys_are_carried_and_a_repeated_key_is_still_refused(backend: Storage) -> None:
    db = build(backend)
    seed(db, rounds=2)
    db.compact("d", through_seq=db.last_seq("d"))
    with pytest.raises(RejectedError) as caught:
        db.append("d", [put("/n", 1, "user", idem_key="K0")], contributor=ALICE)
    assert caught.value.code is ErrorCode.DUPLICATE_IDEM_KEY
    assert db.get("d").document["n"] == 2


def test_a_non_idempotent_field_without_its_keys_refuses_the_compaction(backend: Storage) -> None:
    db = build(backend)
    db.append("d", [put("/n", 1, "user")], contributor=ALICE)  # no idempotency key
    db.append("d", [put("/name", "x")], contributor=ALICE)
    with pytest.raises(RejectedError) as caught:
        db.compact("d", through_seq=2)
    assert caught.value.code is ErrorCode.IDEM_KEY_MISSING and "/n" in str(caught.value)
    assert len(db.list_entries("d")) == 2 and db.verify_archive("d").checked is False


def test_a_second_compaction_of_a_compacted_document(backend: Storage) -> None:
    db = build(backend)
    seed(db, rounds=3)
    db.compact("d", through_seq=6)
    seed(db, rounds=3, tag="L")
    before = observe(db)
    report = db.compact("d", through_seq=db.last_seq("d"))
    assert not report.noop and len(db.list_entries("d")) == 3
    assert observe(db) == before and db.verify_archive("d").checked
    again = db.compact("d", through_seq=db.last_seq("d"))
    assert again.noop


def test_a_retry_after_compaction_is_still_a_duplicate_with_its_original_answer(backend: Storage) -> None:
    db = build(backend)
    first = db.append("d", [put("/name", "ann"), put("/age", 3)], contributor=ALICE, request_key="K")
    db.append("d", [put("/name", "bob")], contributor=ALICE)
    retried_before = db.append("d", [put("/name", "ann"), put("/age", 3)], contributor=ALICE, request_key="K")
    db.compact("d", through_seq=db.last_seq("d"))
    retried = db.append("d", [put("/name", "ann"), put("/age", 3)], contributor=ALICE, request_key="K")
    assert retried.duplicate and retried.entries == first.entries == retried_before.entries
    assert (
        retried.request_id == first.request_id
        and retried.view
        == db.append("d", [put("/name", "ann"), put("/age", 3)], contributor=ALICE, request_key="K").view
    )
    assert len(db.list_entries("d")) == 1 and db.last_seq("d") == 3


def test_entries_recorded_during_a_compaction_are_kept_after_the_watermark(backend: Storage, monkeypatch) -> None:  # noqa: ANN001
    db = build(backend)
    seed(db, rounds=2)
    limit = db.last_seq("d")
    real = db._observe
    fired = threading.Event()

    def noisy(*args, **kwargs):  # type: ignore[no-untyped-def]
        if not fired.is_set():
            fired.set()
            db.append("d", [put("/name", "late")], contributor=ALICE)  # a write in the middle of the check
        return real(*args, **kwargs)

    monkeypatch.setattr(db, "_observe", noisy)
    db.compact("d", through_seq=limit)
    entries = db.list_entries("d")
    assert entries[-1].seq == limit + 1 and entries[-1].op == "set" and [e.op for e in entries[:-1]] == ["layer"] * 3
    assert db.get("d").document["name"] == "late"


def test_a_change_to_the_range_during_the_check_is_refused(backend: Storage, monkeypatch) -> None:  # noqa: ANN001
    from aduku.model import Entry

    db = build(backend)
    seed(db, rounds=2)
    real = db._observe

    def meddling(*args, **kwargs):  # type: ignore[no-untyped-def]
        with backend.transaction() as tx:  # another compactor got there first
            tx.bump_epoch("d")
        return real(*args, **kwargs)

    monkeypatch.setattr(db, "_observe", meddling)
    before = db.list_entries("d")
    with pytest.raises(RejectedError) as caught:
        db.compact("d", through_seq=db.last_seq("d"))
    assert caught.value.code is ErrorCode.LEDGER_CHANGED and db.list_entries("d") == before
    _ = Entry


def test_compacting_with_nothing_in_range_or_by_age(backend: Storage) -> None:
    db = build(backend)
    assert db.compact("d", through_seq=5).noop
    seed(db, rounds=2)
    assert db.compact("d", through_seq=0).noop
    entries = db.list_entries("d")
    cutoff_age = db.clock.now() - entries[3].tx_time + timedelta(milliseconds=1)
    report = db.compact("d", older_than=cutoff_age)
    assert not report.noop and 1 <= report.watermark_seq <= entries[3].seq
    with pytest.raises(ValueError):
        db.compact("d")
    with pytest.raises(ValueError):
        db.compact("d", through_seq=1, older_than=timedelta(days=1))


def test_expiry_keeps_sources_and_explain_says_the_originals_are_gone(backend: Storage) -> None:
    db = build(backend)
    seed(db, rounds=2)
    report = db.compact("d", through_seq=db.last_seq("d"))
    kept = db.explain("d", "/name")
    assert (
        kept.originals_kept is True and kept.sources == () and all(not x.startswith("synthetic") for x in kept.inputs)
    )
    gone = db.expire_archive("d", through_seq=db.last_seq("d"))
    assert gone.expired == report.replaced
    after = db.explain("d", "/name")
    assert after.originals_kept is False and len(after.sources) >= 1 and after.value == kept.value
    assert after.winner in {e.id for e in db.list_entries("d")} and set(after.sources) >= {kept.winner}
    assert db.verify_archive("d").checked is False
    assert db.get("d").document["name"] == "u1"


def test_a_layer_of_an_older_epoch_is_never_used_even_if_it_otherwise_fits(backend: Storage) -> None:
    """A layer restored from a backup older than the compaction fits by version, fingerprint and coverage; only its epoch is wrong."""
    from aduku.storage.base import LayerRow

    db = build(backend)
    seed(db, rounds=2)
    db.compact("d", through_seq=db.last_seq("d"))
    with backend.transaction() as tx:
        rows = tx.get_layers("d")
        assert rows and all(r.epoch == 1 for r in rows)
        tx.drop_layers("d")
        tx.put_layers(
            [
                LayerRow(r.doc, r.tier, r.tier_seq, r.schema_version, r.rules_fingerprint, 0, '[{p:"/name",s:"stale"}]')
                for r in rows
            ]
        )
    cold = restart(db)
    cold.append("d", [put("/age", 1)], contributor=ALICE)
    assert cold.get("d").document["name"] != "stale"
    assert restart(db).get("d").document["name"] != "stale"


def test_review_6_a_new_impure_outcome_during_the_check_refuses_the_compaction(backend: Storage, monkeypatch) -> None:  # noqa: ANN001
    from aduku.model import EffectRecord
    from tests.deferred_helpers import build
    from tests.helpers import T0

    impure = (
        '"/taxId": {type: string, computed: {impure: {hook: "fx.echo@1", depends: ["/country"], reason: "registry",'
        ' timeout: "1s", retry: {max: 1, backoff: "10ms"}, deadline: "3s", recompute_by: [admin]}}}'
    )
    db = build(backend, impure).service
    for i in range(3):
        db.append("d", [put("/country", f"C{i}")], contributor=ALICE)
    real = db._observe
    fired: list[int] = []

    def with_an_outcome(*args, **kwargs):  # type: ignore[no-untyped-def]
        if not fired:
            fired.append(1)
            with backend.transaction() as tx:  # another process recorded an outcome while the check ran
                tx.append_effects([EffectRecord("d", "/taxId", "other", "fx.echo@1", "value", '"x"', None, 1, T0)])
        return real(*args, **kwargs)

    monkeypatch.setattr(db, "_observe", with_an_outcome)
    before = db.list_entries("d")
    with pytest.raises(RejectedError) as caught:
        db.compact("d", through_seq=db.last_seq("d"))
    assert caught.value.code is ErrorCode.LEDGER_CHANGED and db.list_entries("d") == before
    assert db.compact("d", through_seq=db.last_seq("d")).synthetic == 1  # and a retry goes through
