"""SC-003 and FR-003: the document depends on (tier, seq) and the schema, never on arrival order or recorded time."""

import random
from dataclasses import replace
from datetime import timedelta

from hypothesis import given
from hypothesis import strategies as st

from aduku.behaviors.registry import BehaviorRegistry
from aduku.engine.derive import derive, fold_entries
from aduku.ion_codec import to_canonical
from aduku.schema.loader import load_schema
from tests.engine_helpers import PROPERTY_FIELDS, ledger
from tests.helpers import T0
from tests.service_helpers import schema_text

BEHAVIORS = BehaviorRegistry.with_builtins()
SCHEMA = load_schema(schema_text(fields=PROPERTY_FIELDS))


def document(entries) -> str:  # type: ignore[no-untyped-def]
    return to_canonical(derive(SCHEMA, BEHAVIORS, entries).document)


@given(ledger(), st.randoms())
def test_the_order_entries_are_handed_over_in_does_not_matter(entries, rng: random.Random) -> None:  # type: ignore[no-untyped-def]
    shuffled = list(entries)
    rng.shuffle(shuffled)
    assert document(shuffled) == document(entries)


@given(ledger(), st.lists(st.integers(-(10**6), 10**6), min_size=14, max_size=14))
def test_recorded_times_never_matter(entries, offsets) -> None:  # type: ignore[no-untyped-def]
    skewed = [replace(e, tx_time=T0 + timedelta(seconds=offsets[i % 14])) for i, e in enumerate(entries)]
    assert document(skewed) == document(entries)


@given(ledger(), st.data())
def test_folding_in_chunks_equals_folding_at_once(entries, data) -> None:  # type: ignore[no-untyped-def]
    """The service folds new entries into a kept state; that must equal a fresh derivation."""
    cuts = sorted(data.draw(st.lists(st.integers(0, len(entries)), max_size=4)))
    state = None
    start = 0
    for cut in [*cuts, len(entries)]:
        state = fold_entries(SCHEMA, BEHAVIORS, state, entries[start:cut])
        start = cut
    assert state is not None or not entries
    from aduku.engine.derive import build_document, merge_tiers

    chunked = to_canonical(
        build_document(
            SCHEMA, BEHAVIORS, merge_tiers(SCHEMA, BEHAVIORS, state or fold_entries(SCHEMA, BEHAVIORS, None, []))
        )
    )
    assert chunked == document(entries)
