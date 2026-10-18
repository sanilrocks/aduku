"""SPEC 6.2, SC-002: squashing each tier and then merging the tiers equals the flat (tier, seq) fold."""

from hypothesis import given

from aduku.behaviors.merge import SchemaMergeContext
from aduku.behaviors.registry import BehaviorRegistry
from aduku.engine.derive import fold_entries, merge_tiers
from aduku.schema.loader import load_schema
from tests.engine_helpers import PROPERTY_FIELDS, TIERS, ledger
from tests.oracle import flat_fold
from tests.service_helpers import schema_text

BEHAVIORS = BehaviorRegistry.with_builtins()
SCHEMA = load_schema(schema_text(fields=PROPERTY_FIELDS))
CTX = SchemaMergeContext(SCHEMA, BEHAVIORS)


@given(ledger())
def test_squash_per_tier_then_merge_equals_the_flat_fold(entries) -> None:  # type: ignore[no-untyped-def]
    layered = merge_tiers(SCHEMA, BEHAVIORS, fold_entries(SCHEMA, BEHAVIORS, None, entries))
    assert layered == flat_fold(CTX, TIERS, entries)


@given(ledger())
def test_the_document_equals_the_one_built_from_the_flat_fold(entries) -> None:  # type: ignore[no-untyped-def]
    from aduku.engine.derive import build_document, derive

    assert derive(SCHEMA, BEHAVIORS, entries).document == build_document(
        SCHEMA, BEHAVIORS, flat_fold(CTX, TIERS, entries)
    )


@given(ledger(max_size=30))
def test_longer_ledgers(entries) -> None:  # type: ignore[no-untyped-def]
    layered = merge_tiers(SCHEMA, BEHAVIORS, fold_entries(SCHEMA, BEHAVIORS, None, entries))
    assert layered == flat_fold(CTX, TIERS, entries)
