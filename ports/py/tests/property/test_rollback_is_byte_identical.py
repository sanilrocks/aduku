"""Feature 006 (SC-003): activating another version and then the first again changes nothing that was observable before."""

from hypothesis import given
from hypothesis import strategies as st

from aduku.errors import RejectedError
from aduku.model import EntryInput
from aduku.storage.memory import MemoryStorage
from aduku.storage.sqlite import SqliteStorage
from tests.schema_helpers import Env, versions_text
from tests.service_helpers import ALICE, FIELDS, JOB, ROOT

V1 = FIELDS + ', "/n": {type: int, merge: "acme.add@1"}'
# the second version folds differently (min instead of max), derives a field and drops a declaration
V2 = (
    FIELDS.replace('"/age": {type: int, merge: max}', '"/age": {type: int, merge: min}').replace(
        '"/pin": {type: string, merge: lww, ops: [set]},', ""
    )
    + ', "/n": {type: int, merge: "acme.add@1"}'
)
WRITERS = {"backfill": JOB, "user": ALICE, "admin": ROOT}
PATHS = ("/name", "/age", "/tags", "/n", "/pin")
values = {
    "/name": st.text("abc", max_size=3),
    "/age": st.integers(0, 9),
    "/tags": st.lists(st.sampled_from("xyz"), max_size=3).map(tuple),
    "/n": st.integers(1, 3),
    "/pin": st.text("pq", max_size=2),
}


@st.composite
def op(draw: st.DrawFn) -> tuple[str, str, bool, object]:
    tier = draw(st.sampled_from(sorted(WRITERS)))
    path = draw(st.sampled_from(PATHS))
    return (tier, path, draw(st.booleans()), draw(values[path]))


requests = st.lists(st.lists(op(), min_size=1, max_size=3), min_size=1, max_size=9)


def observe(db, storage):  # type: ignore[no-untyped-def]
    out = []
    for doc in ("d", "e"):
        got = db.get(doc, consistency="fresh")
        out.append((got.document, got.model_errors, got.status, got.skipped, got.conflicts))
    with storage.read() as tx:
        out.append([(doc, tx.get_layers(doc), tx.epoch(doc)) for doc in ("d", "e")])
    out.append([db.list_entries(doc) for doc in ("d", "e")])
    return out


def check(storage, plan):  # type: ignore[no-untyped-def]
    env = Env(storage, *versions_text(V1, V2))
    db = env.service(active=1)
    count = 0
    for number, group in enumerate(plan):
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
            db.append("d" if number % 3 else "e", entries, contributor=WRITERS[tier], lenient=tier == "backfill")
        except RejectedError:
            pass
    before = observe(db, storage)
    db.activate_schema(2, operator="alice", acknowledge_breaking=True)
    db.get("d")  # served under 2 in between: it may cache views and layers, which a rollback must not depend on
    db.activate_schema(1, operator="alice")
    assert observe(db, storage) == before
    assert observe(env.service(), storage) == before  # a fresh process agrees


@given(requests)
def test_memory_backend(plan) -> None:  # type: ignore[no-untyped-def]
    check(MemoryStorage(), plan)


@given(requests)
def test_sqlite_backend(plan) -> None:  # type: ignore[no-untyped-def]
    storage = SqliteStorage(":memory:")
    try:
        check(storage, plan)
    finally:
        storage.close()
