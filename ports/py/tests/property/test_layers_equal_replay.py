"""Feature 005 (FR-004, SC-002): an answer through saved layers equals a ledger-only replay on random histories."""

from hypothesis import given
from hypothesis import strategies as st

from aduku.errors import RejectedError
from aduku.model import EntryInput
from aduku.service import Aduku
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

put_op = st.tuples(st.just("put"), st.sampled_from(sorted(WRITERS)), paths).flatmap(
    lambda t: values[t[2]].map(lambda v: ("put", t[1], t[2], v))
)
delete_op = st.tuples(st.just("delete"), st.sampled_from(sorted(WRITERS)), paths)
other_op = st.sampled_from([("restart",), ("drop",), ("damage",), ("rebuild",)])
steps = st.lists(st.one_of(put_op, put_op, delete_op, other_op), min_size=1, max_size=14)


def run(storage: object, ops: list[tuple]) -> tuple[Aduku, Aduku]:
    db = service_on(storage)  # type: ignore[arg-type]
    for op in ops:
        kind = op[0]
        if kind in ("put", "delete"):
            tier, path = op[1], op[2]
            entry = EntryInput(tier, path, "set", op[3]) if kind == "put" else EntryInput(tier, path, "delete")
            try:
                db.append("d", [entry], contributor=WRITERS[tier])
            except RejectedError:
                pass
        elif kind == "restart":
            db = restart(db)
        elif kind == "drop":
            with storage.transaction() as tx:  # type: ignore[attr-defined]
                tx.drop_layers("d")
        elif kind == "damage":
            with storage.transaction() as tx:  # type: ignore[attr-defined]
                rows = tx.get_layers("d")
                if rows:
                    first = rows[0]
                    tx.put_layers(
                        [
                            type(first)(
                                first.doc,
                                first.tier,
                                first.tier_seq,
                                first.schema_version,
                                first.rules_fingerprint,
                                first.epoch,
                                "[",
                            )
                        ]
                    )
        elif kind == "rebuild":
            db.rebuild("d")
    return restart(db), Aduku(db.storage, db.schemas, clock=db.clock, ids=db.ids, behaviors=db.behaviors, layers=False)


def check(storage: object, ops: list[tuple]) -> None:
    on, off = run(storage, ops)
    a, b = on.get("d"), off.get("d")
    assert (a.document, a.model_errors, a.status, a.skipped, a.conflicts) == (
        b.document,
        b.model_errors,
        b.status,
        b.skipped,
        b.conflicts,
    )
    for path in ("/name", "/age", "/tags", "/displayName"):
        assert on.explain("d", path) == off.explain("d", path)


@given(steps)
def test_memory_backend(ops: list[tuple]) -> None:
    check(MemoryStorage(), ops)


@given(steps)
def test_sqlite_backend(ops: list[tuple]) -> None:
    storage = SqliteStorage(":memory:")
    try:
        check(storage, ops)
    finally:
        storage.close()
