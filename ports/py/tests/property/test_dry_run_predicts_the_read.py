"""Feature 006 (SC-002): the dry run reports, for every document, exactly the difference between the document read now and the
one read after the activation; it stores nothing and calls nothing."""

from hypothesis import given
from hypothesis import strategies as st

from aduku.errors import RejectedError
from aduku.model import EntryInput
from aduku.storage.memory import MemoryStorage
from aduku.storage.sqlite import SqliteStorage
from tests.schema_helpers import Env, versions_text
from tests.service_helpers import ALICE, FIELDS, JOB, ROOT

V1 = (
    FIELDS.replace(
        '"/age": {type: int, merge: max}', '"/age": {type: int, merge: max, validate: [range::{min: 0, max: 9}]}'
    )
    + ', "/n": {type: int, merge: "acme.add@1"}, "/nick": {type: string, merge: lww}'
)
# a mix of changes: another merge, a rule that fires, a field that becomes computed, a type that tightens, a removed field
V2 = (
    FIELDS.replace(
        '"/age": {type: int, merge: max}', '"/age": {type: int, merge: min, validate: [range::{min: 0, max: 4}]}'
    )
    .replace('"/pin": {type: string, merge: lww, ops: [set]},', "")
    .replace('"/name": {type: string, merge: lww}', '"/name": {type: string, merge: lww, validate: [max_len::{n: 2}]}')
    + ', "/n": {type: int, merge: "acme.add@1"}, "/nick": {type: string, computed: {macro: upper, args: ["/name"]}}'
)
WRITERS = {"backfill": JOB, "user": ALICE, "admin": ROOT}
PATHS = ("/name", "/age", "/tags", "/n", "/pin", "/nick")
values = {
    "/name": st.text("abc", max_size=3),
    "/age": st.integers(0, 9),
    "/tags": st.lists(st.sampled_from("xyz"), max_size=3).map(tuple),
    "/n": st.integers(1, 3),
    "/pin": st.text("pq", max_size=2),
    "/nick": st.text("abc", max_size=3),
}


@st.composite
def op(draw: st.DrawFn) -> tuple[str, str, bool, object]:
    tier = draw(st.sampled_from(sorted(WRITERS)))
    path = draw(st.sampled_from(PATHS))
    return (tier, path, draw(st.booleans()), draw(values[path]))


requests = st.lists(st.lists(op(), min_size=1, max_size=3), min_size=1, max_size=10)


def stored(storage):  # type: ignore[no-untyped-def]
    with storage.read() as tx:
        docs = tx.doc_ids()
        return [
            [
                (
                    d,
                    tx.get_view(d),
                    tx.get_layers(d),
                    tx.epoch(d),
                    tx.latest_effects(d),
                    tx.get_derived(d),
                    tx.entries(d, 0),
                )
                for d in docs
            ],
            tx.schema_state("t"),
            tx.schema_history("t"),
        ]


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
            db.append(f"d{number % 3}", entries, contributor=WRITERS[tier], lenient=tier == "backfill")
        except RejectedError:
            pass
    docs = [d for d in ("d0", "d1", "d2") if db.last_seq(d)]
    now = {d: db.get(d, consistency="fresh") for d in docs}
    before = stored(storage)
    report = db.plan_schema(2)
    assert stored(storage) == before  # a dry run changes no stored byte
    assert report.examined == len(docs) and report.failed == 0 and report.would_call_hook == 0
    db.activate_schema(2, operator="alice", acknowledge_breaking=True)
    planned = {c.doc: c for c in report.documents}
    for doc in docs:
        after = db.get(doc, consistency="fresh")
        change = planned.get(doc)
        was = {f"{e.rule_id}@{e.path_scope}" for e in now[doc].model_errors}
        will = {f"{e.rule_id}@{e.path_scope}" for e in after.model_errors}
        differs = (
            after.document != now[doc].document
            or was != will
            or after.skipped != now[doc].skipped
            or after.conflicts != now[doc].conflicts
        )
        assert (change is not None) == differs, (doc, change)
        if change is not None:
            assert change.document_changed == (after.document != now[doc].document)
            assert change.errors_added == tuple(sorted(will - was)) and change.errors_resolved == tuple(
                sorted(was - will)
            )
            assert (change.skipped_before, change.skipped_after) == (now[doc].skipped, after.skipped)
            assert sum(n for _, n in change.shadowed_by_reason) == len(after.shadowed)
    assert report.changed == len(planned) and report.changed + report.unchanged == report.examined


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
