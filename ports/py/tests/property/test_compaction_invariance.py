"""Feature 005 (SC-005): compaction at any watermark changes no observable answer; after expiry explain follows research R-9."""

from dataclasses import replace

from hypothesis import given
from hypothesis import strategies as st

from aduku.errors import ErrorCode, RejectedError
from aduku.model import EntryInput
from aduku.storage.memory import MemoryStorage
from aduku.storage.sqlite import SqliteStorage
from tests.service_helpers import ALICE, FIELDS, JOB, ROOT, make_service, schema_text
from tests.snapshot_helpers import restart

SCHEMA = (
    FIELDS.replace(
        '"/age": {type: int, merge: max}', '"/age": {type: int, merge: max, validate: [range::{min: 0, max: 5}]}'
    )
    + ', "/n": {type: int, merge: "acme.add@1"}'
)
WRITERS = {"backfill": JOB, "user": ALICE, "admin": ROOT}
PATHS = ("/name", "/age", "/tags", "/n")
values = {
    "/name": st.text("abc", max_size=3),
    "/age": st.integers(0, 9),
    "/tags": st.lists(st.sampled_from("xyz"), max_size=3).map(tuple),
    "/n": st.integers(1, 3),
}


@st.composite
def op(draw: st.DrawFn) -> tuple[str, str, bool, object]:
    tier = draw(st.sampled_from(sorted(WRITERS)))
    path = draw(st.sampled_from(PATHS))
    return (tier, path, draw(st.booleans()), draw(values[path]))


requests = st.lists(st.lists(op(), min_size=1, max_size=3), min_size=1, max_size=9)


def observe(db, with_explain: bool):  # type: ignore[no-untyped-def]
    got = db.get("d")
    out = [got.document, got.model_errors, got.status, got.skipped, got.conflicts]
    if with_explain:
        out.append({p: replace(db.explain("d", p), originals_kept=None, sources=()) for p in (*PATHS, "/displayName")})
    return out


def build(storage, plan):  # type: ignore[no-untyped-def]
    db = make_service(storage, schema=schema_text(fields=SCHEMA))
    count = 0
    for group in plan:
        tier = group[0][0]
        entries = []
        for tier_, path, delete, value in group:
            if tier_ != tier:
                continue
            count += 1
            if delete and path != "/n":
                entries.append(EntryInput(tier, path, "delete"))
            else:
                entries.append(EntryInput(tier, path, "set", value, idem_key=f"K{count}" if path == "/n" else None))
        try:
            db.append("d", entries, contributor=WRITERS[tier], lenient=tier == "backfill")
        except RejectedError:
            pass
    return db


def check(storage, plan, cut, expire):  # type: ignore[no-untyped-def]
    db = build(storage, plan)
    last = db.last_seq("d")
    if last == 0:
        return
    before = observe(db, True)
    entries_before = db.list_entries("d")
    try:
        report = db.compact("d", through_seq=1 + cut % last)
    except RejectedError as refusal:
        # the check may refuse a case it cannot prove equal (research R-9: a delete and a re-creation that leave the value
        # where a lower tier had it), but only over who is named by `explain`, never over the document or its errors
        assert refusal.code is ErrorCode.COMPACTION_CHECK_FAILED
        assert all(r.message.startswith("/") for r in refusal.reasons), [r.message for r in refusal.reasons]
        assert db.list_entries("d") == entries_before and observe(db, True) == before
        return
    if report.noop:
        return
    assert observe(db, not expire) == (before if not expire else before[:5])
    assert observe(restart(db), not expire) == (before if not expire else before[:5])
    assert db.verify_archive("d").differences == ()
    if expire:
        db.expire_archive("d", through_seq=last)
        for path in (*PATHS, "/displayName"):  # explain keeps its value and rule once the originals are gone
            now = db.explain("d", path)
            old = before[5][path]
            assert (now.value, now.rule, now.model_errors) == (old.value, old.rule, old.model_errors)
        assert observe(restart(db), False) == before[:5]
    # more writes after a compaction behave as they would have against the originals
    reference = build(MemoryStorage(), plan)
    for target in (db, reference):
        try:
            target.append("d", [EntryInput("user", "/name", "set", "zz")], contributor=ALICE)
        except RejectedError:
            pass
    assert db.get("d").document == reference.get("d").document


@given(requests, st.integers(0, 40), st.booleans())
def test_memory_backend(plan, cut, expire) -> None:  # type: ignore[no-untyped-def]
    check(MemoryStorage(), plan, cut, expire)


@given(requests, st.integers(0, 40), st.booleans())
def test_sqlite_backend(plan, cut, expire) -> None:  # type: ignore[no-untyped-def]
    storage = SqliteStorage(":memory:")
    try:
        check(storage, plan, cut, expire)
    finally:
        storage.close()
