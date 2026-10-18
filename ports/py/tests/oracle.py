"""The reference flat fold: the oracle the layered engine is compared with (SPEC 6.2, Principle V).

It applies the accepted entries one at a time, sorted by (tier rank, seq), to a single growing map. The engine instead
squashes each tier into a layer and overlays the layers. Both must give the same merged state.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from aduku.behaviors.merge import Recreated, SchemaMergeContext, step
from aduku.engine.defaults import defaults_layer
from aduku.ion_codec import ABSENT, TOMBSTONE
from aduku.model import Entry
from aduku.paths import Path, parse_path


def flat_fold(ctx: SchemaMergeContext, tier_order: Sequence[str], entries: Sequence[Entry]) -> dict[Path, Any]:
    rank = {tier: index for index, tier in enumerate(tier_order)}
    merged: dict[Path, Any] = dict(defaults_layer(ctx))  # the lowest layer
    for entry in sorted(entries, key=lambda e: (rank[e.tier], e.seq)):
        for path, state in _writes(ctx, entry).items():
            if state is TOMBSTONE or isinstance(state, Recreated):  # a reset also discards what lies beneath
                for other in [p for p in merged if len(p) > len(path) and p[: len(path)] == path]:
                    del merged[other]
            merged[path] = step(ctx.rule_for(path), path, merged.get(path, ABSENT), state, ctx)
    return merged


def _writes(ctx: SchemaMergeContext, entry: Entry) -> dict[Path, Any]:
    path = parse_path(entry.path)
    if entry.op == "delete":
        return {path: TOMBSTONE}
    out: dict[Path, Any] = {}

    def spread(at: Path, value: Any) -> None:
        if ctx.rule_for(at).field_wise and hasattr(value, "items"):
            out[at] = type(value)()  # the container exists
            for key, child in value.items():
                from aduku.paths import Segment

                spread((*at, Segment(key)), child)
        else:
            out[at] = value

    spread(path, entry.value)
    return out
