"""Feature 005 (research R-1, R-3): a derivation state to saved layers and back, and when saved layers fit."""

from dataclasses import dataclass

from aduku.behaviors.registry import BehaviorRegistry
from aduku.engine.derive import (
    DerivationState,
    fold_entries,
    layer_texts,
    layers_fit,
    merge_tiers,
    prepare,
    state_from_layers,
)
from aduku.engine.layer_codec import encode_layer
from aduku.model import ModelError
from aduku.schema.loader import load_schema
from tests.helpers import make_entry


def e(seq: int, path: str, value: object, tier: str):  # noqa: ANN201
    return make_entry("d1", seq, request_id=f"r{seq}", path=path, value=value, tier=tier)


SCHEMA = load_schema(
    'schema::{id: "t", version: 1, tiers: [backfill, user, admin], fields: {"/name": {type: string, merge: lww},'
    ' "/age": {type: int, merge: max}}}',
    BehaviorRegistry.with_builtins(),
)


def folded() -> DerivationState:
    behaviors = BehaviorRegistry.with_builtins()
    entries = [
        e(1, "/name", "a", "user"),
        e(2, "/age", 3, "backfill"),
        e(3, "/name", "b", "user"),
    ]
    return fold_entries(SCHEMA, behaviors, None, entries, prepare(SCHEMA, behaviors))


def test_a_state_goes_to_texts_and_back_equal() -> None:
    state = folded()
    texts = layer_texts(state, SCHEMA)
    assert set(texts) == {"backfill", "user", "admin"} and texts["admin"] == encode_layer({})
    errors = (ModelError("r", "/name", "m", "req-1"),)
    back = state_from_layers(SCHEMA, 3, texts, skipped=(2,), errors=errors, conflicts=("/x",), effects_seq=4)
    assert back.layers.keys() == state.layers.keys() or all(back.layers[t] == state.layers.get(t, {}) for t in texts)
    assert (back.watermark, back.skipped, back.errors, back.conflicts, back.effects_seq) == (
        3,
        (2,),
        errors,
        ("/x",),
        4,
    )
    assert back.schema_ref is SCHEMA and back.schema_version == 1


def test_continuing_from_saved_layers_gives_the_same_merge_as_the_whole_fold() -> None:
    behaviors = BehaviorRegistry.with_builtins()
    ready = prepare(SCHEMA, behaviors)
    first = folded()
    more = [e(4, "/age", 9, "user"), e(5, "/name", "c", "admin")]
    saved = state_from_layers(SCHEMA, 3, layer_texts(first, SCHEMA))
    continued = fold_entries(SCHEMA, behaviors, saved, more, ready)
    whole = fold_entries(
        SCHEMA,
        behaviors,
        None,
        [e(1, "/name", "a", "user"), e(2, "/age", 3, "backfill"), e(3, "/name", "b", "user"), *more],
        ready,
    )
    assert merge_tiers(SCHEMA, behaviors, continued, ready) == merge_tiers(SCHEMA, behaviors, whole, ready)
    assert continued.watermark == 5


@dataclass(frozen=True)
class Row:
    tier: str
    tier_seq: int
    schema_version: int = 1
    rules_fingerprint: str = SCHEMA.rules_fingerprint
    epoch: int = 0


def rows(*items: Row) -> dict[str, Row]:
    return {r.tier: r for r in items}


def test_layers_fit_when_every_tier_with_entries_has_a_matching_row() -> None:
    seqs = {"backfill": 2, "user": 3, "admin": 0}
    assert layers_fit(rows(Row("backfill", 2), Row("user", 3)), seqs, SCHEMA, 0)


def test_layers_do_not_fit_for_each_kind_of_mismatch() -> None:
    seqs = {"backfill": 2, "user": 3, "admin": 0}
    good = [Row("backfill", 2), Row("user", 3)]
    assert not layers_fit(rows(*good[:1]), seqs, SCHEMA, 0)  # a tier with entries has no row
    assert not layers_fit(rows(good[0], Row("user", 2)), seqs, SCHEMA, 0)  # lags: tier_seq below the tier's last
    assert not layers_fit(rows(good[0], Row("user", 4)), seqs, SCHEMA, 0)  # ahead of the view
    assert not layers_fit(rows(good[0], Row("user", 3, schema_version=2)), seqs, SCHEMA, 0)
    assert not layers_fit(rows(good[0], Row("user", 3, rules_fingerprint="other")), seqs, SCHEMA, 0)
    assert not layers_fit(rows(good[0], Row("user", 3, epoch=1)), seqs, SCHEMA, 0)  # another epoch
    assert not layers_fit(rows(*good, Row("admin", 5)), seqs, SCHEMA, 0)  # a row for a tier with no entries


def test_rows_of_tiers_the_schema_does_not_have_are_ignored() -> None:
    seqs = {"backfill": 0, "user": 0, "admin": 0}
    assert layers_fit(rows(Row("retired", 9)), seqs, SCHEMA, 0)
