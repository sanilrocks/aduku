"""Feature 005, user story 1: reads and writes use saved per-tier layers and give exactly the ledger's answer."""

from collections.abc import Callable

from aduku.errors import RejectedError
from aduku.model import DocumentResult, Identity
from aduku.service import Aduku
from aduku.storage.base import LayerRow, Storage
from tests.service_helpers import ALICE, FIELDS, JOB, ROOT, make_service, put, schema_text
from tests.snapshot_helpers import CountingStorage, FaultyStorage, restart, service_on


def ledger_only(db: Aduku) -> Aduku:
    """The same storage and schemas, with saved layers off: every answer comes from the ledger alone."""
    return Aduku(db.storage, db.schemas, clock=db.clock, ids=db.ids, behaviors=db.behaviors, layers=False)


def same(a: DocumentResult, b: DocumentResult) -> bool:
    return (a.document, a.model_errors, a.status, a.skipped, a.conflicts) == (
        b.document,
        b.model_errors,
        b.status,
        b.skipped,
        b.conflicts,
    )


def seed(db: Aduku, doc: str = "d") -> None:
    db.append(doc, [put("/name", "ann", "backfill"), put("/age", 3, "backfill")], contributor=JOB)
    db.append(doc, [put("/name", "bob"), put("/tags", ("x",))], contributor=ALICE)
    db.append(doc, [put("/age", 9, "admin"), put("/plan", "pro", "admin")], contributor=ROOT)


def layers_of(storage: Storage, doc: str = "d") -> dict[str, LayerRow]:
    with storage.read() as tx:
        return {r.tier: r for r in tx.get_layers(doc)}


def test_a_write_at_one_tier_leaves_the_other_tiers_layers_untouched(backend: Storage) -> None:
    db = service_on(backend)
    seed(db)
    before = layers_of(backend)
    assert set(before) == {"backfill", "user", "admin"}
    db.append("d", [put("/name", "cy")], contributor=ALICE)
    after = layers_of(backend)
    assert after["backfill"] == before["backfill"] and after["admin"] == before["admin"]
    assert after["user"].tier_seq == db.last_seq("d") and after["user"].state != before["user"].state


def test_a_restarted_service_does_not_read_the_entries_the_layers_cover(backend: Storage) -> None:
    counting = CountingStorage(backend)
    db = service_on(counting)  # type: ignore[arg-type]
    for i in range(30):
        db.append("d", [put("/name", f"n{i}")], contributor=ALICE)
    cold = restart(db)
    counting.reset()
    cold.append("d", [put("/name", "last")], contributor=ALICE)
    assert counting.entries_read == 0
    again = restart(db)
    counting.reset()
    assert again.get("d").document["name"] == "last"
    assert counting.entries_read == 0
    assert same(again.get("d"), ledger_only(db).get("d"))


def test_a_rules_change_makes_layers_unused_and_they_are_saved_again(backend: Storage) -> None:
    db = service_on(backend)
    seed(db)
    from aduku.schema.loader import load_schema

    v2 = load_schema(
        schema_text(
            version=2, fields=FIELDS.replace('"/age": {type: int, merge: max}', '"/age": {type: int, merge: min}')
        ),
        db.behaviors,
    )
    db.schemas.put(v2)
    db.schemas.activate(2)
    cold = restart(db)
    assert same(cold.get("d"), ledger_only(db).get("d"))
    cold.append("d", [put("/age", 1, "user")], contributor=ALICE)
    fresh = layers_of(backend)
    assert all(row.schema_version == 2 and row.rules_fingerprint == v2.rules_fingerprint for row in fresh.values())
    assert same(restart(db).get("d"), ledger_only(db).get("d"))


def test_missing_layers_are_not_needed(backend: Storage) -> None:
    db = service_on(backend)
    seed(db)
    with backend.transaction() as tx:
        tx.drop_layers("d")
    cold = restart(db)
    assert same(cold.get("d"), ledger_only(db).get("d"))
    cold.append("d", [put("/name", "zed")], contributor=ALICE)
    assert set(layers_of(backend)) == {"backfill", "user", "admin"}  # the write saved them again
    assert restart(db).get("d").document["name"] == "zed"


def test_damaged_layers_are_ignored_and_replaced(backend: Storage) -> None:
    db = service_on(backend)
    seed(db)
    rows = layers_of(backend)
    with backend.transaction() as tx:
        tx.put_layers(
            [LayerRow("d", "user", rows["user"].tier_seq, 1, rows["user"].rules_fingerprint, 0, "not ion [[[")]
        )
    cold = restart(db)
    cold.append("d", [put("/name", "ok")], contributor=ALICE)
    assert restart(db).get("d").document["name"] == "ok"
    assert same(restart(db).get("d"), ledger_only(db).get("d"))
    assert layers_of(backend)["user"].state != "not ion [[["


def test_a_layer_that_lags_the_ledger_is_not_trusted(backend: Storage) -> None:
    db = service_on(backend)
    seed(db)
    rows = layers_of(backend)
    stale = LayerRow("d", "user", rows["user"].tier_seq - 1, 1, rows["user"].rules_fingerprint, 0, rows["user"].state)
    with backend.transaction() as tx:
        tx.drop_layers("d")
        tx.put_layers([stale, rows["backfill"], rows["admin"]])
    assert same(restart(db).get("d"), ledger_only(db).get("d"))
    restart(db).append("d", [put("/name", "fresh")], contributor=ALICE)
    assert same(restart(db).get("d"), ledger_only(db).get("d"))


def test_layers_that_cannot_be_saved_never_fail_a_write(backend: Storage) -> None:
    flaky = FaultyStorage(backend, ["put_layers"], nth=1)
    db = service_on(flaky)  # type: ignore[arg-type]
    result = db.append("d", [put("/name", "ann")], contributor=ALICE)  # the save fails: still accepted
    assert result.entries
    assert flaky.calls == 1
    flaky.armed = False
    assert restart(db).get("d").document["name"] == "ann"
    assert same(restart(db).get("d"), ledger_only(db).get("d"))


def test_a_rejected_request_changes_no_layer_and_no_view(backend: Storage) -> None:
    db = service_on(backend)
    seed(db)
    with backend.read() as tx:
        view = tx.get_view("d")
    layers = layers_of(backend)
    for attempt in (
        lambda: db.append("d", [put("/plan", "free", "user")], contributor=ALICE),  # not authorized
        lambda: db.append("d", [put("/age", "old")], contributor=ALICE),  # wrong type
        lambda: db.append("d", [put("/name", "x"), put("/nope", 1)], contributor=ALICE),  # unknown path
    ):
        try:
            attempt()
        except RejectedError:
            pass
        else:
            raise AssertionError("should have been rejected")
    with backend.read() as tx:
        assert tx.get_view("d") == view
    assert layers_of(backend) == layers


def test_layers_off_is_the_same_service_with_no_rows(backend: Storage) -> None:
    db = service_on(backend, layers=False)
    seed(db)
    assert layers_of(backend) == {}
    on = Aduku(backend, db.schemas, clock=db.clock, ids=db.ids, behaviors=db.behaviors, layers=True)
    assert same(on.get("d"), db.get("d"))
    _: Callable[..., object] = make_service  # keep the import used by sibling tests
    _ = Identity


def test_entries_a_new_version_cannot_use_stay_covered_by_the_layers(backend: Storage) -> None:
    """A field that becomes computed shadows its old entries (skipped): the layer still covers them by seq."""
    from aduku.schema.loader import load_schema

    db = service_on(
        backend, schema=schema_text(fields='"/name": {type: string, merge: lww}, "/shout": {type: string, merge: lww}')
    )
    db.append("d", [put("/name", "pat"), put("/shout", "stale")], contributor=ALICE)
    v2 = load_schema(
        schema_text(
            version=2,
            fields='"/name": {type: string, merge: lww}, "/shout": {type: string, computed: {macro: upper, args: ["/name"]}}',
        ),
        db.behaviors,
    )
    db.schemas.put(v2)
    db.schemas.activate(2)
    db.append("d", [put("/name", "kim")], contributor=ALICE)
    assert layers_of(backend)["user"].tier_seq == db.last_seq("d")
    cold = restart(db)
    result = cold.get("d")
    assert result.skipped == (2,) and same(result, ledger_only(db).get("d"))
    cold.append("d", [put("/name", "lee")], contributor=ALICE)
    assert same(restart(db).get("d"), ledger_only(db).get("d")) and restart(db).get("d").skipped == (2,)
