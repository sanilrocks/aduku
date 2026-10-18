"""P2: overlay layers, lowest first. Pure.

Overlaying is associative, so squashing each tier and then overlaying the tiers equals one flat fold of every entry in
(tier, seq) order (SPEC 6.2). A tombstone, or a value written after one, also discards everything beneath its path in
the layers below it (SPEC 6.7 item 5); the marker stays in the result so that this holds however layers are grouped.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from aduku.behaviors.merge import MergeContext, MergeTypeError, Recreated, SchemaMergeContext, step
from aduku.ion_codec import ABSENT, TOMBSTONE
from aduku.paths import Path

__all__ = ["apply_layer", "overlay"]


def apply_layer(acc: dict[Path, Any], layer: dict[Path, Any], ctx: SchemaMergeContext) -> None:
    """Overlay `layer` onto `acc` in place. All or nothing: if a rule cannot merge a value, `acc` is left unchanged."""
    resets = [path for path, state in layer.items() if state is TOMBSTONE or isinstance(state, Recreated)]
    reset_paths = set(resets)  # a path is wiped if a strict prefix of it is reset: check its prefixes, not every reset
    doomed = (
        {p for p in acc if any(p[:size] in reset_paths for size in range(1, len(p)))} if reset_paths and acc else set()
    )
    updates = {
        path: step(ctx.rule_for(path), path, ABSENT if path in doomed else acc.get(path, ABSENT), state, ctx)
        for path, state in layer.items()
    }  # may raise MergeTypeError, before anything is changed
    for path in doomed:
        del acc[path]
    acc.update(updates)


def overlay(
    layers: Iterable[dict[Path, Any]], ctx: SchemaMergeContext, conflicts: list[Path] | None = None
) -> dict[Path, Any]:
    """Overlay layers given lowest first into a new layer.

    Values that merge inside each layer can still clash across layers (entries written under another schema version).
    Then the layer is applied path by path, parents first, and the incoming value of a path that cannot be merged is
    left out and reported in `conflicts`; the lower layer's value for it stands.
    """
    merged: dict[Path, Any] = {}
    for layer in layers:
        try:
            apply_layer(merged, layer, ctx)
        except MergeTypeError:
            for path in sorted(layer, key=lambda p: (len(p), [(s.name, s.key or "") for s in p])):
                try:
                    apply_layer(merged, {path: layer[path]}, ctx)
                except MergeTypeError:
                    if conflicts is not None:
                        conflicts.append(path)
    return merged


def _as_context(ctx: SchemaMergeContext) -> MergeContext:
    return ctx
