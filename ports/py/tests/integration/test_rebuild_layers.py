"""Feature 005, user story 2: `rebuild` compares saved layers with the ledger, reports and repairs differences."""

import pytest

from aduku.storage.base import LayerRow, Storage
from tests.service_helpers import ALICE, JOB, ROOT, put
from tests.snapshot_helpers import restart, service_on


def seeded(backend: Storage, doc: str = "d"):  # noqa: ANN201
    db = service_on(backend)
    db.append(doc, [put("/name", "ann", "backfill"), put("/age", 3, "backfill")], contributor=JOB)
    db.append(doc, [put("/name", "bob"), put("/tags", ("x",))], contributor=ALICE)
    db.append(doc, [put("/age", 9, "admin")], contributor=ROOT)
    return db


def rows(backend: Storage, doc: str = "d") -> dict[str, LayerRow]:
    with backend.read() as tx:
        return {r.tier: r for r in tx.get_layers(doc)}


def ledger(backend: Storage, doc: str = "d") -> list:
    with backend.read() as tx:
        return tx.entries(doc)


def test_correct_layers_report_no_difference_and_stay_byte_identical(backend: Storage) -> None:
    db = seeded(backend)
    before = rows(backend)
    report = db.rebuild("d")
    assert report.identical and report.layers_differing == () and report.layers_refreshed == ()
    assert rows(backend) == before


@pytest.mark.parametrize("damage", ["state", "ahead"])
def test_an_altered_layer_or_a_coverage_that_claims_too_much_is_reported_and_repaired(
    backend: Storage, damage: str
) -> None:
    db = seeded(backend)
    good = rows(backend)
    bad = good["user"]
    altered = LayerRow(
        "d",
        "user",
        bad.tier_seq + (5 if damage == "ahead" else 0),
        bad.schema_version,
        bad.rules_fingerprint,
        bad.epoch,
        bad.state if damage == "ahead" else '[{p:"/name",s:"tampered"}]',
    )
    with backend.transaction() as tx:
        tx.drop_layers("d")
        tx.put_layers([good["backfill"], altered, good["admin"]])
    entries = ledger(backend)
    report = db.rebuild("d")
    assert not report.identical and report.layers_differing == (("d", "user"),)
    assert rows(backend) == good  # repaired
    assert ledger(backend) == entries  # the ledger is never written
    assert db.rebuild("d").identical  # a second rebuild finds nothing


def test_a_layer_that_lags_or_is_missing_or_is_from_another_version_is_refreshed_not_a_difference(
    backend: Storage,
) -> None:
    db = seeded(backend)
    good = rows(backend)
    lagging = LayerRow("d", "user", good["user"].tier_seq - 1, 1, good["user"].rules_fingerprint, 0, good["user"].state)
    other = LayerRow("d", "admin", good["admin"].tier_seq, 9, good["admin"].rules_fingerprint, 0, good["admin"].state)
    with backend.transaction() as tx:
        tx.drop_layers("d")
        tx.put_layers([lagging, other])  # backfill missing
    report = db.rebuild("d")
    assert report.identical  # nothing was *wrong*
    assert set(report.layers_refreshed) == {("d", "backfill"), ("d", "user"), ("d", "admin")}
    assert rows(backend) == good


def test_a_damaged_view_is_still_found(backend: Storage) -> None:
    db = seeded(backend)
    with backend.read() as tx:
        view = tx.get_view("d")
    assert view is not None
    with backend.transaction() as tx:
        tx.put_view(type(view)(**{**view.__dict__, "document": '{name:"tampered"}'}))
    report = db.rebuild("d")
    assert report.differing == ("d",) and not report.identical
    with backend.read() as tx:
        assert tx.get_view("d") == view


def test_rebuild_of_all_documents_checks_every_documents_layers(backend: Storage) -> None:
    db = seeded(backend, "a")
    db.append("b", [put("/name", "x", "user")], contributor=ALICE)
    with backend.transaction() as tx:
        tx.drop_layers("a")
    report = db.rebuild()
    assert report.checked == ("a", "b") and report.identical
    assert ("a", "user") in report.layers_refreshed and not [x for x in report.layers_refreshed if x[0] == "b"]
    assert set(rows(backend, "a")) == {"backfill", "user", "admin"}


def test_rebuild_writes_layers_for_a_document_written_without_them(backend: Storage) -> None:
    db = service_on(backend, layers=False)
    db.append("d", [put("/name", "ann", "user")], contributor=ALICE)
    assert rows(backend) == {}
    report = restart(db).rebuild("d")
    assert report.identical and report.layers_refreshed == ()  # layers were off in this service: nothing to compare
    on = service_on(backend, layers=True)
    report = on.rebuild("d")
    assert set(report.layers_refreshed) == {("d", "user")} and set(rows(backend)) == {"user"}


def impure_world(backend: Storage):  # type: ignore[no-untyped-def]
    from tests.deferred_helpers import build, deferred

    impure = (
        '"/taxId": {type: string, computed: {impure: {hook: "fx.echo@1", depends: ["/country"], reason: "registry",'
        ' timeout: "1s", retry: {max: 1, backoff: "10ms"}, deadline: "3s", recompute_by: [admin]}}}'
    )
    built = build(backend, f'"/creditScore": {deferred()}, {impure}')
    built.service.append("d1", [put("/name", "Alice"), put("/country", "US")], contributor=ALICE)
    built.service.get("d1")  # fetches the deferred value
    return built


def test_rebuild_calls_no_hook_and_no_source_while_the_recorded_outcomes_cover_the_inputs(backend: Storage) -> None:
    built = impure_world(backend)
    before = (built.calls("fx.echo@1"), built.calls("fx.score@1"))
    assert before == (1, 1)
    report = built.service.rebuild()
    assert report.identical and (built.calls("fx.echo@1"), built.calls("fx.score@1")) == before


def test_rebuild_fills_a_missing_impure_outcome_once_and_never_calls_a_source(backend: Storage) -> None:

    built = impure_world(backend)
    db = built.service
    db.append("d1", [put("/country", "FR")], contributor=ALICE)
    with backend.transaction() as tx:  # the outcome for the current inputs is gone
        tx.apply_purge(
            __import__("aduku.model", fromlist=["PurgeApplication"]).PurgeApplication("d1", (), (), (), ("/taxId",))
        )
    before = (built.calls("fx.echo@1"), built.calls("fx.score@1"))
    report = db.rebuild()
    assert report.identical
    assert (
        built.calls("fx.echo@1") == before[0] + 1 and built.calls("fx.score@1") == before[1]
    )  # one fill, no source call
    assert db.rebuild().identical and built.calls("fx.echo@1") == before[0] + 1
