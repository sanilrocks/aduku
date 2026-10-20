"""Derivation: P1 to P3 and document assembly, as a pure function of (entries, schema). No clock, no IO.

`fold_entries` can continue from a previous state, which is how the service avoids re-folding a whole ledger on every
append: entries of one document arrive in increasing seq, and within a tier seq is the order that counts.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from aduku.behaviors.merge import MergeTypeError, SchemaMergeContext
from aduku.behaviors.registry import BehaviorRegistry
from aduku.engine.defaults import defaults_layer, delete_fallbacks
from aduku.engine.document import assemble
from aduku.engine.graph import order
from aduku.engine.layer_codec import decode_layer, encode_layer
from aduku.engine.merge_layers import apply_layer, overlay
from aduku.engine.shadow import entry_nonconformity
from aduku.engine.squash import Layer, entry_layer
from aduku.engine.tombstones import apply_tombstones
from aduku.ion_codec import FrozenStruct
from aduku.model import Entry, ModelError
from aduku.paths import Path, PathError, format_path, parse_path
from aduku.schema.model import FieldSpec, Schema

__all__ = [
    "DerivationState",
    "Derived",
    "Prepared",
    "build_document",
    "derive",
    "fold_entries",
    "layer_texts",
    "layers_fit",
    "merge_tiers",
    "prepare",
    "state_from_layers",
]


@dataclass(frozen=True)
class Prepared:
    """What depends only on the schema version, computed once: the merge context, the defaults and the fallbacks."""

    ctx: SchemaMergeContext
    defaults: Layer
    fallbacks: dict[Path, Any]
    computed: tuple[FieldSpec, ...] = ()  # the computed fields in evaluation order (P4)

    @property
    def has_impure(self) -> bool:
        return any(spec.impure is not None for spec in self.computed)


def prepare(schema: Schema, behaviors: BehaviorRegistry) -> Prepared:
    ctx = SchemaMergeContext(schema, behaviors)
    return Prepared(ctx, defaults_layer(ctx), delete_fallbacks(ctx), order(schema))


@dataclass(frozen=True)
class DerivationState:
    """Per-tier squashed layers up to `watermark` (the highest seq folded in). Never changed after creation.

    Continuing from a state saves folding the entries already seen, but each step still copies the tier layers it touches
    and the document is merged and assembled again, so the cost of an append grows with the size of the document, not
    with the length of the ledger. Avoiding that needs snapshots, which are outside this feature.
    """

    schema_version: int
    watermark: int = 0
    layers: dict[str, Layer] = field(default_factory=dict)
    # Seqs of entries this schema version cannot use (unknown tier, unmergeable value): left out of the document, listed.
    skipped: tuple[int, ...] = ()
    # The Schema object the layers were folded under: a state is only continued under that very schema.
    schema_ref: Any = None
    # The model errors of the document this state stands for (who introduced each), and the paths left out of it.
    errors: tuple[ModelError, ...] = ()
    conflicts: tuple[str, ...] = ()
    # The effect-log sequence number this state's errors were computed with (feature 002); a newer log makes it stale.
    effects_seq: int = 0
    # The document's epoch the state was made in (feature 005): a compaction or purge makes older states unusable.
    epoch: int = 0


@dataclass(frozen=True)
class Derived:
    document: FrozenStruct
    merged: Layer
    state: DerivationState
    # Paths whose value was left out of the document because it could not be merged or had no place in it.
    conflicts: tuple[str, ...] = ()


def fold_entries(
    schema: Schema,
    behaviors: BehaviorRegistry,
    state: DerivationState | None,
    entries: Sequence[Entry],
    prepared: Prepared | None = None,
) -> DerivationState:
    """P1: fold `entries` into the per-tier layers of `state` (a new state is returned; `state` is not changed)."""
    ctx = (prepared or prepare(schema, behaviors)).ctx
    if state is not None and state.schema_version != schema.version:
        raise ValueError("a derivation state belongs to one schema version")
    layers = dict(state.layers) if state else {}
    watermark = state.watermark if state else 0
    skipped: list[int] = []  # seqs this call finds unusable; the earlier ones are in `state.skipped`
    copied: set[str] = set()
    skipped.extend(
        entry.seq for entry in entries if entry.tier not in schema.tiers and not entry.purged
    )  # a tier this version lacks
    # a purged entry (feature 005) is a placeholder with no effect, as if its contribution had never been made
    known = [entry for entry in entries if entry.tier in schema.tiers and not entry.purged]
    for entry in sorted(known, key=lambda e: (schema.tier_rank(e.tier), e.seq)):
        try:
            if entry.op != "layer" and schema.derived_field_at_or_above(parse_path(entry.path)) is not None:
                skipped.append(entry.seq)  # the field became derived in this schema version: shadowed (SPEC 9.2)
                continue
            layer = entry_layer(ctx, entry)
            shadowed = [path for path in layer if schema.derived_field_at_or_above(path) is not None]
        except PathError:
            skipped.append(entry.seq)  # not a valid path in this schema version
            continue
        for path in shadowed:  # a child of a struct write that this schema version derives: the rest of the entry stays
            del layer[path]
        if shadowed:
            skipped.append(entry.seq)
        # feature 006: an entry written under another version is not checked again by admission, so a value that no longer
        # conforms to the type now in force is left out (an entry of the version in force always conforms)
        if entry.schema_version != schema.version and entry_nonconformity(schema, ctx, entry) is not None:
            if not shadowed:
                skipped.append(entry.seq)
            continue
        if entry.tier not in copied:
            layers[entry.tier] = dict(layers.get(entry.tier, {}))
            copied.add(entry.tier)
        try:
            apply_layer(layers[entry.tier], layer, ctx)
        except MergeTypeError:
            skipped.append(entry.seq)  # all or nothing: apply_layer left the layer unchanged
    watermark = max([watermark, *(entry.seq for entry in entries)])
    before = state.skipped if state else ()
    unusable = before + tuple(sorted(set(skipped) - set(before))) if skipped else before
    return DerivationState(schema.version, watermark, layers, unusable, schema)


def merge_tiers(
    schema: Schema,
    behaviors: BehaviorRegistry,
    state: DerivationState,
    prepared: Prepared | None = None,
    conflicts: list[Path] | None = None,
) -> Layer:
    """P2: overlay the defaults and then the tier layers, lowest tier first. Clashing paths go to `conflicts`."""
    ready = prepared or prepare(schema, behaviors)
    layers = [ready.defaults, *(state.layers.get(tier, {}) for tier in schema.tiers)]
    return overlay(layers, ready.ctx, conflicts)


def build_document(
    schema: Schema,
    behaviors: BehaviorRegistry,
    merged: Layer,
    prepared: Prepared | None = None,
    conflicts: list[Path] | None = None,
) -> FrozenStruct:
    """P3 and assembly: drop tombstones and reset markers, then nest the values. Dropped paths go to `conflicts`."""
    ready = prepared or prepare(schema, behaviors)
    return assemble(apply_tombstones(merged, ready.fallbacks), ready.ctx, conflicts)


def derive(
    schema: Schema, behaviors: BehaviorRegistry, entries: Sequence[Entry], prepared: Prepared | None = None
) -> Derived:
    """The whole pipeline from a ledger. The order `entries` are handed over in does not matter."""
    ready = prepared or prepare(schema, behaviors)
    state = fold_entries(schema, behaviors, None, entries, ready)
    clashes: list[Path] = []
    merged = merge_tiers(schema, behaviors, state, ready, clashes)
    document = build_document(schema, behaviors, merged, ready, clashes)
    return Derived(document, merged, state, tuple(sorted({format_path(p) for p in clashes})))


# -- saved layers (feature 005, research R-1 to R-3) ------------------------------------------------------------------


def layer_texts(state: DerivationState, schema: Schema) -> dict[str, str]:
    """The canonical text of each tier's layer of `state` (a tier with no entries gets the empty layer)."""
    return {tier: encode_layer(state.layers.get(tier, {})) for tier in schema.tiers}


def state_from_layers(
    schema: Schema,
    watermark: int,
    texts: Mapping[str, str],
    *,
    skipped: tuple[int, ...] = (),
    errors: tuple[ModelError, ...] = (),
    conflicts: tuple[str, ...] = (),
    effects_seq: int = 0,
    epoch: int = 0,
) -> DerivationState:
    """The derivation state saved layers stand for. Continue it with `fold_entries` over the entries after `watermark`.

    Raises IonError for a layer text that is not a layer.
    """
    layers = {tier: decode_layer(text) for tier, text in texts.items() if tier in schema.tiers}
    return DerivationState(schema.version, watermark, layers, skipped, schema, errors, conflicts, effects_seq, epoch)


class SavedLayerLike(Protocol):
    @property
    def tier_seq(self) -> int: ...

    @property
    def schema_version(self) -> int: ...

    @property
    def rules_fingerprint(self) -> str: ...

    @property
    def epoch(self) -> int: ...


def layers_fit(rows: Mapping[str, SavedLayerLike], tier_seqs: Mapping[str, int], schema: Schema, epoch: int) -> bool:
    """True if saved layers can stand for the ledger up to a watermark (research R-3).

    `tier_seqs[tier]` is the highest seq of that tier at or below the watermark. A tier with entries needs a row that
    covers exactly up to that seq and was made under this very version, rules and epoch; a tier with none needs no row (a row
    for it would be inconsistent). Rows of tiers the schema does not have are ignored.
    """
    for tier in schema.tiers:
        expected = tier_seqs.get(tier, 0)
        row = rows.get(tier)
        if expected == 0:
            if row is not None and row.tier_seq != 0:
                return False
            continue
        if (
            row is None
            or row.tier_seq != expected
            or row.schema_version != schema.version
            or row.rules_fingerprint != schema.rules_fingerprint
            or row.epoch != epoch
        ):
            return False
    return True
