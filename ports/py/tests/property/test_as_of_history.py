"""Feature 005 (FR-014, SC-004): reading `as_of` a seq gives the document that was live when that entry was committed."""

from hypothesis import given
from hypothesis import strategies as st

from aduku.errors import RejectedError
from aduku.model import AsOf, EntryInput
from aduku.storage.memory import MemoryStorage
from aduku.storage.sqlite import SqliteStorage
from tests.service_helpers import ALICE, JOB, ROOT
from tests.snapshot_helpers import restart, service_on

WRITERS = {"backfill": JOB, "user": ALICE, "admin": ROOT}
paths = st.sampled_from(["/name", "/age", "/tags"])
values = {
    "/name": st.text("abc", max_size=3),
    "/age": st.integers(-3, 3),
    "/tags": st.lists(st.sampled_from("xyz"), max_size=3).map(tuple),
}
entry = st.tuples(st.sampled_from(sorted(WRITERS)), paths, st.booleans()).flatmap(
    lambda t: values[t[1]].map(lambda v: (t[0], t[1], None if t[2] else ("set", v)))
)
requests = st.lists(st.lists(entry, min_size=1, max_size=3), min_size=1, max_size=10)


def check(storage: object, plan: list) -> None:
    db = service_on(storage)  # type: ignore[arg-type]
    live = []
    for group in plan:
        inputs = [
            EntryInput(tier, path, "delete") if action is None else EntryInput(tier, path, "set", action[1])
            for tier, path, action in group
        ]
        writers = {WRITERS[e.tier] for e in inputs}
        if len(writers) != 1:  # one request has one contributor: keep each request to one tier
            inputs = [e for e in inputs if e.tier == inputs[0].tier]
        try:
            result = db.append("d", inputs, contributor=WRITERS[inputs[0].tier])
        except RejectedError:
            continue
        assert result.view is not None
        live.append((result.entries[-1][1], result.view))
    for cold in (db, restart(db)):
        for seq, view in live:
            past = cold.get("d", as_of=AsOf(seq=seq))
            assert (past.document, past.model_errors) == (view.document, view.model_errors), seq
            assert past.as_of is not None and past.as_of.resolved_seq == seq and not past.as_of.inside_request
    entries_before = db.list_entries("d")
    db.get("d", as_of=AsOf(seq=1))
    assert db.list_entries("d") == entries_before


@given(requests)
def test_memory_backend(plan: list) -> None:
    check(MemoryStorage(), plan)


@given(requests)
def test_sqlite_backend(plan: list) -> None:
    storage = SqliteStorage(":memory:")
    try:
        check(storage, plan)
    finally:
        storage.close()
