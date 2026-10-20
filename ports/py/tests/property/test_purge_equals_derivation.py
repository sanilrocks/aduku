"""Feature 005 (FR-032, FR-037): after a purge the document is what the remaining contributions give; rebuild is clean."""

from hypothesis import given
from hypothesis import strategies as st

from aduku.errors import RejectedError
from aduku.model import EntryInput, Identity, PurgeScope
from aduku.storage.base import SequentialIds
from aduku.storage.memory import MemoryStorage
from aduku.storage.sqlite import SqliteStorage
from tests.service_helpers import ALICE, FIELDS, JOB, ROOT, make_service, schema_text

SCHEMA = schema_text(fields=FIELDS).replace("tiers:", "purge_by: [dpo], roles: [dpo], tiers:")
WRITERS = {"backfill": JOB, "user": ALICE, "admin": ROOT}
DPO = Identity("officer", ("admin",), ("dpo",))
PATHS = ("/name", "/age", "/tags")
values = {
    "/name": st.text("abc", max_size=3),
    "/age": st.integers(-3, 3),
    "/tags": st.lists(st.sampled_from("xyz"), max_size=3).map(tuple),
}


@st.composite
def contribution(draw: st.DrawFn) -> tuple[str, str, object]:
    tier = draw(st.sampled_from(sorted(WRITERS)))
    path = draw(st.sampled_from(PATHS))
    return (tier, path, None if draw(st.booleans()) else draw(values[path]))


history = st.lists(contribution(), min_size=1, max_size=10)


def append_all(db, rows):  # type: ignore[no-untyped-def]
    for tier, path, value in rows:
        entry = EntryInput(tier, path, "delete") if value is None else EntryInput(tier, path, "set", value)
        try:
            db.append("d", [entry], contributor=WRITERS[tier])
        except RejectedError:
            pass


def check(storage, rows, path, tier, how):  # type: ignore[no-untyped-def]
    db = make_service(storage, schema=SCHEMA)
    append_all(db, rows)
    if db.last_seq("d") == 0:
        return
    if how in ("compact", "expire"):
        try:
            db.compact("d", through_seq=db.last_seq("d") - db.last_seq("d") // 3)
        except RejectedError:
            return
        if how == "expire":
            db.expire_archive("d", through_seq=db.last_seq("d"))
    scoped = tier if tier != "any" else None
    db.purge("d", PurgeScope(path=path, tier=scoped), reason="ticket", caller=DPO)
    kept = [r for r in rows if not (r[1] == path and (scoped is None or r[0] == scoped))]
    reference = make_service(MemoryStorage(), schema=SCHEMA, ids=SequentialIds("ref"))
    append_all(reference, kept)
    got = db.get("d")
    if how != "expire":  # after an expiry the compacted contribution at the path goes whole, which may be more
        assert got.document == reference.get("d").document
    report = db.rebuild("d")
    assert report.identical and not report.failed
    again = db.purge("d", PurgeScope(path=path, tier=scoped), reason="ticket", caller=DPO)  # nothing more to erase
    assert again.entries_erased == 0 and db.get("d").document == got.document
    assert len(db.purge_log("d")) == 2


@given(
    history,
    st.sampled_from(PATHS),
    st.sampled_from(["any", "backfill", "user", "admin"]),
    st.sampled_from(["plain", "compact", "expire"]),
)
def test_memory_backend(rows, path, tier, how) -> None:  # type: ignore[no-untyped-def]
    check(MemoryStorage(), rows, path, tier, how)


@given(
    history,
    st.sampled_from(PATHS),
    st.sampled_from(["any", "backfill", "user", "admin"]),
    st.sampled_from(["plain", "compact", "expire"]),
)
def test_sqlite_backend(rows, path, tier, how) -> None:  # type: ignore[no-untyped-def]
    storage = SqliteStorage(":memory:")
    try:
        check(storage, rows, path, tier, how)
    finally:
        storage.close()
