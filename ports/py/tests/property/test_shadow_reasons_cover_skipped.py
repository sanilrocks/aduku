"""Feature 006 (SC-006): on random ledgers every skipped entry has exactly one of the reasons, and none is skipped under the
version that wrote the entries."""

from dataclasses import replace

from hypothesis import given
from hypothesis import strategies as st

from aduku.engine.shadow import shadow_reasons
from aduku.errors import RejectedError
from aduku.model import EntryInput
from aduku.storage.memory import MemoryStorage
from aduku.storage.sqlite import SqliteStorage
from tests.schema_helpers import Env, versions_text
from tests.service_helpers import ALICE, FIELDS, JOB, ROOT

V1 = FIELDS + ', "/n": {type: int, merge: "acme.add@1"}, "/nick": {type: string, merge: lww}'
# /nick becomes computed, /age a string, /n folds differently
V2 = (
    FIELDS.replace('"/age": {type: int, merge: max}', '"/age": {type: string, merge: lww}')
    + ', "/n": {type: int, merge: min}, "/nick": {type: string, computed: {macro: upper, args: ["/name"]}}'
)
REASONS = {"tier_not_in_schema", "now_derived", "no_longer_conforms", "cannot_be_merged"}
WRITERS = {"backfill": JOB, "user": ALICE, "admin": ROOT}
PATHS = ("/name", "/age", "/tags", "/n", "/nick", "/pin")
values = {
    "/name": st.text("abc", max_size=3),
    "/age": st.integers(0, 9),
    "/tags": st.lists(st.sampled_from("xyz"), max_size=3).map(tuple),
    "/n": st.integers(1, 3),
    "/nick": st.text("abc", max_size=3),
    "/pin": st.text("pq", max_size=2),
}


@st.composite
def op(draw: st.DrawFn) -> tuple[str, str, bool, object]:
    tier = draw(st.sampled_from(sorted(WRITERS)))
    path = draw(st.sampled_from(PATHS))
    return (tier, path, draw(st.booleans()), draw(values[path]))


requests = st.lists(st.lists(op(), min_size=1, max_size=3), min_size=1, max_size=10)


def check(storage, plan):  # type: ignore[no-untyped-def]
    env = Env(storage, *versions_text(V1, V2))
    db = env.service(active=1)
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
    assert db.get("d").skipped == () and db.get("d").shadowed == ()  # the writing version skips nothing
    db.activate_schema(2, operator="alice", acknowledge_breaking=True)
    result = db.get("d", consistency="fresh")
    assert [s.seq for s in result.shadowed] == sorted(result.skipped)  # one reason each, no entry without one
    assert all(s.reason in REASONS for s in result.shadowed)
    # a version that drops the admin tier (an activation would refuse it while the ledger uses it): read through the engine
    dropped = replace(env.schemas()[0], version=3, tiers=("backfill", "user"))
    entries = db.list_entries("d")
    skipped = db._replay(dropped, entries).final.state.skipped
    found = shadow_reasons(dropped, db.behaviors, db._engine(dropped), entries, skipped)
    assert [s.seq for s in found] == sorted(skipped) and all(s.reason in REASONS for s in found)
    assert {s.seq for s in found if s.reason == "tier_not_in_schema"} == {
        e.seq for e in entries if e.tier == "admin"
    } & set(skipped)


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
