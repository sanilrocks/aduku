"""Feature 005 (FR-031, FR-032, FR-036, research R-12): the pure purge planner."""

from datetime import UTC, datetime, timedelta
from itertools import count

from aduku.behaviors.registry import BehaviorRegistry
from aduku.engine.compaction import compacted_ledger
from aduku.engine.compaction import plan as compact_plan
from aduku.engine.derive import derive, prepare
from aduku.engine.layer_codec import decode_layer
from aduku.engine.purge import downstream_fields, plan
from aduku.ion_codec import FrozenStruct
from aduku.paths import parse_path
from aduku.schema.loader import load_schema
from tests.helpers import T0, make_entry
from tests.service_helpers import schema_text

BEHAVIORS = BehaviorRegistry.with_builtins()
FIELDS = (
    '"/name": {type: string, merge: lww}, "/age": {type: int, merge: max}, "/address": {type: struct, merge: lww},'
    ' "/shout": {type: string, computed: {macro: upper, args: ["/name"]}},'
    ' "/loud": {type: string, computed: {macro: upper, args: ["/shout"]}},'
    ' "/other": {type: string, computed: {macro: upper, args: ["/age"]}}'
)
SCHEMA = load_schema(schema_text(fields=FIELDS), BEHAVIORS)
READY = prepare(SCHEMA, BEHAVIORS)
NOW = datetime(2026, 2, 1, tzinfo=UTC)
MARK = "secret-marker"


def e(seq, request, tier="user", path="/name", value="x", **kw):  # type: ignore[no-untyped-def]  # noqa: ANN001, ANN201
    return make_entry(
        "d", seq, request_id=request, tier=tier, path=path, value=value, tx_time=T0 + timedelta(seconds=seq), **kw
    )


def run(live, archive=(), compactions=None, path=None, tier=None, ids=()):  # type: ignore[no-untyped-def]  # noqa: ANN001, ANN201
    return plan(
        live, list(archive), compactions or {}, SCHEMA, BEHAVIORS, READY,
        path=None if path is None else parse_path(path), tier=tier, entry_ids=frozenset(ids),
    )  # fmt: skip


def test_a_path_scope_erases_what_is_at_or_below_it_and_keeps_the_rest_of_the_record() -> None:
    live = [
        e(
            1,
            "r1",
            path="/name",
            value=MARK,
            meta={"ticket": MARK},
            valid_time=T0,
            idem_key="K1",
            request_key="RK",
            request_hash="h1",
        ),
        e(2, "r1", path="/age", value=3, request_key="RK", request_hash="h1"),
        e(3, "r2", tier="admin", path="/name", value="other"),
    ]
    changes = run(live, path="/name")
    by_id = {x.id: x for x in changes.live}
    erased = by_id["e-d-1"]
    assert (erased.value, erased.meta, erased.valid_time, erased.request_hash, erased.purged) == (
        None,
        None,
        None,
        None,
        True,
    )
    assert (erased.idem_key, erased.request_key, erased.tier, erased.path, erased.op, erased.seq) == (
        "K1",
        "RK",
        "user",
        "/name",
        "set",
        1,
    )
    assert (
        by_id["e-d-3"].purged and "e-d-2" in by_id and by_id["e-d-2"].request_hash is None and not by_id["e-d-2"].purged
    )
    assert (changes.erased, changes.requests, changes.paths) == (2, ("r1", "r2"), ("/name",))


def test_a_tier_narrows_a_path_scope_and_entry_ids_name_entries() -> None:
    live = [e(1, "r1", tier="user"), e(2, "r2", tier="admin"), e(3, "r3", tier="user", path="/age", value=1)]
    assert {x.id for x in run(live, path="/name", tier="admin").live if x.purged} == {"e-d-2"}
    assert {x.id for x in run(live, ids=["e-d-3"]).live if x.purged} == {"e-d-3"}
    assert run(live, ids=["e-d-3"]).paths == ("/age",)


def test_a_purged_ledger_derives_as_if_the_contributions_were_never_made() -> None:
    live = [e(1, "r1", value="first"), e(2, "r2", value=MARK), e(3, "r3", tier="admin", value="top")]
    replaced = {x.id: x for x in run(live, ids=["e-d-3"]).live}
    after = [replaced.get(x.id, x) for x in live]
    assert derive(SCHEMA, BEHAVIORS, after, READY).document == derive(SCHEMA, BEHAVIORS, live[:2], READY).document


def test_a_struct_written_above_the_path_loses_only_that_part() -> None:
    value = FrozenStruct({"city": MARK, "zip": "1"})
    live = [
        e(1, "r1", path="/address", value=value, request_hash="h"),
        e(2, "r1", path="/age", value=1, request_hash="h"),
    ]
    changes = run(live, path="/address/city")
    first = next(x for x in changes.live if x.id == "e-d-1")
    assert first.value == FrozenStruct({"zip": "1"}) and not first.purged and first.request_hash is None
    assert (changes.erased, changes.rewritten) == (0, 1) and next(
        x for x in changes.live if x.id == "e-d-2"
    ).request_hash is None


def compacted(entries, through):  # type: ignore[no-untyped-def]  # noqa: ANN001, ANN201
    ids = count(1)
    result = compact_plan(
        entries, SCHEMA, BEHAVIORS, READY, doc="d", through_seq=through, compaction_id=f"c{through}",
        new_id=lambda: f"s{through}-{next(ids)}", created=NOW, policy="p", base_errors="[]", base_skipped=(),
    )  # fmt: skip
    assert result is not None
    return result


def test_a_synthetic_entry_is_folded_again_from_the_originals_that_remain() -> None:
    entries = [e(1, "r1", value="old"), e(2, "r2", value=MARK)]
    result = compacted(entries, 2)
    live = compacted_ledger(result, entries)
    archive = [(result.record.id, x) for x in result.replaced]
    changes = run(live, archive, {result.record.id: result.record}, path="/name")
    synthetic = next(x for x in changes.live if x.synthetic)
    assert decode_layer(synthetic.value) == {}  # type: ignore[arg-type]  # both contributions were at the path
    assert changes.expired_sources is False and {x.id for x in changes.archive if x.purged} == {"e-d-1", "e-d-2"}
    only = run(live, archive, {result.record.id: result.record}, ids=["e-d-2"])
    kept = next(x for x in only.live if x.synthetic)
    assert dict(decode_layer(kept.value)) == {parse_path("/name"): "old"}  # type: ignore[arg-type]


def test_when_the_originals_are_gone_the_whole_compacted_contribution_at_the_path_is_erased() -> None:
    entries = [e(1, "r1", value=MARK), e(2, "r1", path="/age", value=4)]
    result = compacted(entries, 2)
    live = compacted_ledger(result, entries)
    changes = run(live, [], {result.record.id: result.record}, path="/name")  # the archive was expired
    synthetic = next(x for x in changes.live if x.synthetic)
    assert dict(decode_layer(synthetic.value)) == {parse_path("/age"): 4}  # type: ignore[arg-type]
    assert changes.expired_sources is True


def test_nested_compactions_reach_the_originals_through_the_archives() -> None:
    entries = [e(1, "r1", value=MARK), e(2, "r2", value="second"), e(3, "r3", path="/age", value=2)]
    first = compacted(entries, 2)
    after_first = compacted_ledger(first, entries)
    second = compacted(after_first, 3)
    live = compacted_ledger(second, after_first)
    archive = [(first.record.id, x) for x in first.replaced] + [(second.record.id, x) for x in second.replaced]
    records = {first.record.id: first.record, second.record.id: second.record}
    changes = run(live, archive, records, ids=["e-d-1"])
    top = next(x for x in changes.live if x.tier == "user" and x.synthetic)
    assert dict(decode_layer(top.value)) == {parse_path("/name"): "second", parse_path("/age"): 2}  # type: ignore[arg-type]
    inner = [x for x in changes.archive if x.synthetic == first.record.id]
    assert inner and decode_layer(inner[0].value) == {parse_path("/name"): "second"}  # type: ignore[arg-type]
    assert any(x.id == "e-d-1" and x.purged for x in changes.archive)


def test_downstream_fields_follow_the_dependency_graph() -> None:
    assert downstream_fields(SCHEMA, [parse_path("/name")]) == ("/loud", "/shout")
    assert downstream_fields(SCHEMA, [parse_path("/age")]) == ("/other",)
    assert downstream_fields(SCHEMA, [parse_path("/address/city")]) == ()


# -- findings of the code review ----------------------------------------------------------------------------------------


def test_review_1_an_expired_synthetic_layer_loses_the_path_only_for_the_tier_named() -> None:
    entries = [e(1, "r1", tier="backfill", value="low-marker"), e(2, "r2", tier="user", value="high-marker")]
    result = compacted(entries, 2)
    live = compacted_ledger(result, entries)
    changes = run(live, [], {result.record.id: result.record}, path="/name", tier="user")  # the archive is gone
    layers = {x.tier: decode_layer(x.value) for x in changes.live if x.synthetic}  # type: ignore[arg-type]
    assert set(layers) == {"user"} and layers["user"] == {}  # the backfill layer is not touched: it was not named
    untouched = next(x for x in live if x.tier == "backfill")
    assert untouched not in changes.live and decode_layer(untouched.value) == {parse_path("/name"): "low-marker"}  # type: ignore[arg-type]


def test_review_2_a_purge_by_id_reaches_a_layer_that_cannot_be_folded_again() -> None:
    from dataclasses import replace

    entries = [e(1, "r1", value=MARK), e(2, "r2", path="/age", value=4)]
    result = compacted(entries, 2)
    live = compacted_ledger(result, entries)
    archive = [(result.record.id, x) for x in result.replaced]
    other_rules = replace(result.record, fold_fingerprint="rules that fold differently")  # the schema moved on
    changes = run(live, archive, {result.record.id: other_rules}, ids=["e-d-1"])
    layer = decode_layer(next(x for x in changes.live if x.synthetic).value)  # type: ignore[arg-type]
    assert layer == {parse_path("/age"): 4} and changes.expired_sources is True  # the value is gone from the layer too
