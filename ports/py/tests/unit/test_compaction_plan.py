"""Feature 005 (FR-021, FR-024, research R-7 to R-9): the pure planner and the comparison of two derivations."""

from datetime import UTC, datetime, timedelta
from itertools import count

import pytest

from aduku.behaviors.registry import BehaviorRegistry, Laws
from aduku.engine.compaction import (
    EarlierCompaction,
    Observed,
    compacted_ledger,
    differences,
    id_map,
    plan,
    watermark_for,
)
from aduku.engine.derive import derive, prepare
from aduku.engine.layer_codec import decode_layer
from aduku.errors import ErrorCode, RejectedError
from aduku.model import Explanation
from aduku.schema.loader import load_schema
from tests.helpers import T0, make_entry
from tests.service_helpers import schema_text

BEHAVIORS = BehaviorRegistry.with_builtins()
BEHAVIORS.register_merge(
    "acme.add",
    1,
    lambda path, old, new, ctx: (0 if old is None or old.__class__.__name__ == "_Absent" else old) + new,
    Laws(associative=True),
)
FIELDS = '"/name": {type: string, merge: lww}, "/age": {type: int, merge: max}, "/n": {type: int, merge: "acme.add@1"}'
SCHEMA = load_schema(schema_text(fields=FIELDS), BEHAVIORS)
READY = prepare(SCHEMA, BEHAVIORS)
NOW = datetime(2026, 2, 1, tzinfo=UTC)


def e(seq: int, request: str, tier: str = "user", path: str = "/name", value: object = "x", **kw: object):  # noqa: ANN201
    return make_entry(
        "d", seq, request_id=request, tier=tier, path=path, value=value, tx_time=T0 + timedelta(seconds=seq), **kw
    )  # type: ignore[arg-type]


def make_plan(entries, through, **kw):  # type: ignore[no-untyped-def]
    ids = count(1)
    args = dict(
        doc="d",
        through_seq=through,
        compaction_id="c1",
        new_id=lambda: f"s{next(ids)}",
        created=NOW,
        policy="through_seq=1",
        base_errors="[]",
        base_skipped=(),
    )
    args.update(kw)
    return plan(entries, SCHEMA, BEHAVIORS, READY, **args)


def ledger():  # noqa: ANN201
    return [
        e(1, "r1", "backfill", "/name", "a"),
        e(2, "r1", "backfill", "/age", 3),
        e(3, "r2", "user", "/name", "b", valid_time=T0),
        e(4, "r2", "user", "/age", 7),
        e(5, "r3", "admin", "/name", "c"),
    ]


def test_the_watermark_moves_down_to_the_end_of_a_request() -> None:
    entries = ledger()
    assert [watermark_for(entries, n) for n in (0, 1, 2, 3, 4, 5, 99)] == [0, 0, 2, 2, 4, 5, 5]


def test_one_synthetic_entry_per_tier_with_the_highest_source_seq_and_time() -> None:
    result = make_plan(ledger(), 4)
    assert result is not None and result.record.watermark_seq == 4
    assert [(s.tier, s.seq, s.op, s.path, s.request_id, s.synthetic) for s in result.synthetic] == [
        ("backfill", 2, "layer", "/", "c1", "c1"),
        ("user", 4, "layer", "/", "c1", "c1"),
    ]
    assert result.synthetic[1].tx_time == T0 + timedelta(seconds=4)
    user = decode_layer(result.synthetic[1].value)  # type: ignore[arg-type]
    assert sorted((str(k[0].name), v) for k, v in user.items()) == [("age", 7), ("name", "b")]
    assert [r.seq for r in result.replaced] == [1, 2, 3, 4]


def test_the_compacted_ledger_gives_the_same_document() -> None:
    entries = ledger()
    result = make_plan(entries, 4)
    assert result is not None
    after = compacted_ledger(result, entries)
    assert [x.seq for x in after] == [2, 4, 5]
    assert derive(SCHEMA, BEHAVIORS, after, READY).document == derive(SCHEMA, BEHAVIORS, entries, READY).document


def test_the_record_lists_sources_keys_the_request_index_and_what_the_range_held() -> None:
    entries = ledger()
    entries[0] = e(1, "r1", "backfill", "/name", "a", idem_key="K1")
    result = make_plan(entries, 4)
    assert result is not None
    assert [(r.source_id, r.seq, r.tier, r.idem_key) for r in result.sources][0] == ("e-d-1", 1, "backfill", "K1")
    assert {r.source_id for r in result.sources} == {"e-d-1", "e-d-2", "e-d-3", "e-d-4"}
    assert result.retired_keys == ("K1",)
    assert result.request_index == (("r1", "e-d-1", 1), ("r1", "e-d-2", 2), ("r2", "e-d-3", 3), ("r2", "e-d-4", 4))
    record = result.record
    assert (record.first_tx_time, record.last_tx_time, record.has_valid_time) == (
        T0 + timedelta(seconds=1),
        T0 + timedelta(seconds=4),
        True,
    )
    assert record.fold_fingerprint == SCHEMA.fold_fingerprint and record.archived is True


def test_nothing_in_range_or_one_synthetic_per_tier_already_is_a_no_op() -> None:
    assert make_plan(ledger(), 1) is None  # inside the first request: the watermark moves down to 0
    entries = ledger()
    result = make_plan(entries, 4)
    assert result is not None
    assert make_plan(compacted_ledger(result, entries), 4) is None
    assert make_plan([], 10) is None


def test_a_range_with_two_schema_versions_is_refused() -> None:
    entries = ledger()
    entries[2] = e(3, "r2", "user", "/name", "b", schema_version=2)
    with pytest.raises(RejectedError) as caught:
        make_plan(entries, 4)
    assert caught.value.code is ErrorCode.MIXED_SCHEMA_RANGE


def test_a_non_idempotent_field_needs_its_keys_but_earlier_synthetic_entries_count_as_keyed() -> None:
    keyless = [e(1, "r1", "user", "/n", 5), e(2, "r2", "user", "/n", 6)]
    with pytest.raises(RejectedError) as caught:
        make_plan(keyless, 2)
    assert caught.value.code is ErrorCode.IDEM_KEY_MISSING and "/n" in str(caught.value)
    keyed = [
        e(1, "r1", "user", "/n", 5, idem_key="K1"),
        e(2, "r2", "user", "/n", 6, idem_key="K2"),
        e(3, "r3", "user", "/n", 7, idem_key="K3"),
    ]
    first = make_plan(keyed, 2)
    assert first is not None
    again = compacted_ledger(first, keyed)  # one synthetic (no idem_key) and r3
    second = make_plan(again, 3, compaction_id="c2", earlier={"c1": EarlierCompaction(T0, T0, False)})
    assert (
        second is not None
        and second.retired_keys == ("K3",)
        and any(r.source_id == first.synthetic[0].id for r in second.sources)
    )
    assert (
        derive(SCHEMA, BEHAVIORS, compacted_ledger(second, again), READY).document
        == derive(SCHEMA, BEHAVIORS, keyed, READY).document
    )


def test_a_second_compaction_takes_the_interval_and_valid_times_of_the_first() -> None:
    entries = ledger()
    first = make_plan(entries, 4)
    assert first is not None
    now = compacted_ledger(first, entries)
    info = EarlierCompaction(first.record.first_tx_time, first.record.last_tx_time, first.record.has_valid_time)
    second = make_plan(now, 5, compaction_id="c2", earlier={"c1": info})
    assert second is not None
    assert (second.record.first_tx_time, second.record.has_valid_time) == (first.record.first_tx_time, True)
    assert second.record.last_tx_time == T0 + timedelta(seconds=5)


def test_a_tier_the_schema_lacks_gets_an_empty_synthetic_entry() -> None:
    entries = [e(1, "r1", "retired", "/name", "x"), e(2, "r2", "user", "/name", "y")]
    result = make_plan(entries, 2)
    assert result is not None and {s.tier for s in result.synthetic} == {"retired", "user"}
    assert decode_layer(next(s for s in result.synthetic if s.tier == "retired").value) == {}  # type: ignore[arg-type]


def test_id_map_sends_each_replaced_entry_to_its_tiers_synthetic_entry() -> None:
    result = make_plan(ledger(), 4)
    assert result is not None
    mapping = id_map(result)
    assert mapping["e-d-1"] == mapping["e-d-2"] == result.synthetic[0].id and mapping["e-d-3"] == result.synthetic[1].id


def explained(**kw: object) -> Explanation:
    base = dict(
        path="/name", value="b", winner="e-d-3", winner_seq=3, rule="lww@1", inputs=("e-d-3",), shadowed=("e-d-1",)
    )
    base.update(kw)
    return Explanation(**base)  # type: ignore[arg-type]


def observed(**kw: object) -> Observed:
    base = dict(document="{}", errors=(), skipped=(), conflicts=(), explained={"/name": explained()})
    base.update(kw)
    return Observed(**base)  # type: ignore[arg-type]


MAPPING = {"e-d-1": "s1", "e-d-3": "s2", "e-d-4": "s2"}


def test_equal_views_with_replaced_ids_renamed_agree() -> None:
    after = observed(explained={"/name": explained(winner="s2", winner_seq=4, inputs=("s2",), shadowed=("s1",))})
    assert differences(observed(), after, MAPPING) == []


def test_a_synthetic_entry_is_an_input_if_any_source_was_and_shadowed_if_none_was() -> None:
    before = observed(explained={"/name": explained(inputs=("e-d-3", "e-d-4"), shadowed=("e-d-1",))})
    after = observed(explained={"/name": explained(winner="s2", inputs=("s2",), shadowed=("s1",))})
    assert differences(before, after, MAPPING) == []
    wrong = observed(explained={"/name": explained(winner="s2", inputs=("s2", "s1"), shadowed=())})
    assert any("inputs differ" in d for d in differences(before, wrong, MAPPING))


@pytest.mark.parametrize(
    "change, text",
    [
        ({"document": "{a:1}"}, "document"),
        ({"skipped": (2,)}, "skipped"),
        ({"conflicts": ("/x",)}, "conflicting"),
    ],
)
def test_each_kind_of_difference_is_named(change: dict, text: str) -> None:
    assert any(text in d for d in differences(observed(), observed(**change), {}))


def test_a_changed_value_rule_or_winner_is_named_and_at_most_five_are_listed() -> None:
    assert any(
        "value differs" in d for d in differences(observed(), observed(explained={"/name": explained(value="z")}), {})
    )
    assert any(
        "rule differs" in d for d in differences(observed(), observed(explained={"/name": explained(rule="max@1")}), {})
    )
    assert any(
        "winner differs" in d for d in differences(observed(), observed(explained={"/name": explained(winner="q")}), {})
    )
    many = observed(
        document="1",
        errors=(),
        skipped=(1,),
        conflicts=("/c",),
        explained={f"/p{i}": explained(value=i) for i in range(9)},
    )
    assert len(differences(many, observed(), {})) == 5
