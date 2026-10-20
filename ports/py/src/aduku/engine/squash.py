"""P1: turn entries into layers and squash one tier's entries into a single layer. Pure.

A layer maps a path to a fold state: a plain value, TOMBSTONE, or Recreated(value) (see behaviors.merge). An entry that
writes a struct to a field merged field by field is spread over the child paths, so that writes at `/address` and at
`/address/city` interleave correctly by (tier, seq).
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from aduku.behaviors.merge import SchemaMergeContext
from aduku.engine.layer_codec import Layer, decode_layer
from aduku.engine.merge_layers import apply_layer
from aduku.ion_codec import TOMBSTONE, FrozenStruct
from aduku.model import Entry
from aduku.paths import Path, Segment, parse_path

__all__ = ["Layer", "entry_layer", "spread", "squash"]


def entry_layer(ctx: SchemaMergeContext, entry: Entry) -> Layer:
    """The writes one entry makes: a delete is a tombstone at its path; a set is the value, spread if field-wise.

    A `layer` entry (feature 005) is what compaction wrote for a tier: its value is the tier's whole folded layer.
    """
    if entry.op == "layer":
        return decode_layer(entry.value)
    path = parse_path(entry.path)
    if entry.op == "delete":
        return {path: TOMBSTONE}
    return spread(ctx, path, entry.value)


def spread(ctx: SchemaMergeContext, path: Path, value: Any) -> Layer:
    """The layer a set of `value` at `path` makes: a struct at a field-wise field is spread over its child paths."""
    out: Layer = {}
    _spread(ctx, path, value, out)
    return out


def _spread(ctx: SchemaMergeContext, path: Path, value: Any, out: Layer) -> None:
    if ctx.rule_for(path).field_wise and isinstance(value, FrozenStruct):
        out[path] = FrozenStruct()  # the container exists, even if none of its children does
        for key in sorted(value):
            _spread(ctx, (*path, Segment(key)), value[key], out)
    else:
        out[path] = value


def squash(ctx: SchemaMergeContext, entries: Iterable[Entry]) -> Layer:
    """Fold entries (already in seq order) into one layer."""
    layer: Layer = {}
    for entry in entries:
        apply_layer(layer, entry_layer(ctx, entry), ctx)
    return layer
