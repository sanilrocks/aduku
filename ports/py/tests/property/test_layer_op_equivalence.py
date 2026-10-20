"""Feature 005 (FR-021, Principle V): folding a tier's entries equals folding the one `layer` entry made from their layer."""

from hypothesis import given
from hypothesis import strategies as st

from aduku.behaviors.registry import BehaviorRegistry
from aduku.engine.derive import derive, fold_entries, prepare
from aduku.engine.layer_codec import encode_layer
from aduku.schema.loader import load_schema
from tests.engine_helpers import PROPERTY_FIELDS, TIERS, ledger
from tests.fixtures import register_fixtures
from tests.helpers import make_entry
from tests.service_helpers import schema_text

BEHAVIORS = BehaviorRegistry.with_builtins()
register_fixtures(BEHAVIORS)
SCHEMA = load_schema(
    schema_text(fields=PROPERTY_FIELDS + ', "/best": {type: string, merge: "acme.longest@1"}'), BEHAVIORS
)
READY = prepare(SCHEMA, BEHAVIORS)


def synthetic_of(head, cut_seq):  # type: ignore[no-untyped-def]
    """One `layer` entry per tier for the entries in `head`, as compaction makes them."""
    state = fold_entries(SCHEMA, BEHAVIORS, None, head, READY)
    made = []
    for tier in TIERS:
        sources = [e for e in head if e.tier == tier]
        if not sources:
            continue
        made.append(
            make_entry(
                seq=max(e.seq for e in sources),
                request_id="c1",
                tier=tier,
                path="/",
                op="layer",
                value=encode_layer(state.layers.get(tier, {})),
                synthetic="c1",
            )
        )
    return made


@given(ledger(max_size=20), st.integers(0, 20))
def test_a_prefix_replaced_by_layer_entries_gives_the_same_document(entries, cut) -> None:  # type: ignore[no-untyped-def]
    head, tail = entries[:cut], entries[cut:]
    compacted = [*synthetic_of(head, cut), *tail]
    assert derive(SCHEMA, BEHAVIORS, compacted, READY).document == derive(SCHEMA, BEHAVIORS, entries, READY).document


@given(ledger(max_size=20), st.integers(0, 20))
def test_the_per_tier_layers_are_the_same_after_the_tail_is_folded_in(entries, cut) -> None:  # type: ignore[no-untyped-def]
    head, tail = entries[:cut], entries[cut:]
    whole = fold_entries(SCHEMA, BEHAVIORS, None, entries, READY)
    compacted = fold_entries(SCHEMA, BEHAVIORS, None, [*synthetic_of(head, cut), *tail], READY)
    assert {t: whole.layers.get(t, {}) for t in TIERS} == {t: compacted.layers.get(t, {}) for t in TIERS}


@given(st.lists(st.tuples(st.sampled_from(TIERS), st.text("ab", max_size=4)), max_size=10), st.integers(0, 10))
def test_a_custom_associative_merge_behaves_the_same(rows, cut) -> None:  # type: ignore[no-untyped-def]
    entries = [make_entry(seq=i, request_id=f"r{i}", tier=t, path="/best", value=v) for i, (t, v) in enumerate(rows, 1)]
    head, tail = entries[:cut], entries[cut:]
    assert (
        derive(SCHEMA, BEHAVIORS, [*synthetic_of(head, cut), *tail], READY).document
        == derive(SCHEMA, BEHAVIORS, entries, READY).document
    )
