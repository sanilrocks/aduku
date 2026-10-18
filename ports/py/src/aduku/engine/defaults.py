"""Defaults (SPEC 8): the lowest layer of the merge, and the values a deleted field may fall back to. Pure.

A default is a static value on a field with an exact path. A struct default is spread over its child paths, and a child
may declare its own default (a deeper declaration wins), so defaults apply per child unless the struct declares one.
"""

from __future__ import annotations

from typing import Any

from aduku.behaviors.merge import SchemaMergeContext
from aduku.engine.squash import Layer, spread
from aduku.paths import Path, parse_pattern

__all__ = ["defaults_layer", "delete_fallbacks"]


def _collect(ctx: SchemaMergeContext, only_on_delete: bool) -> Layer:
    found: Layer = {}
    ordered = sorted(ctx.schema.fields, key=lambda spec: len(parse_pattern(spec.pattern)))  # parents before children
    for spec in ordered:
        if not spec.has_default or spec.derived is not None or "*" in spec.pattern:
            continue
        if only_on_delete and spec.on_delete != "default":
            continue
        declared = parse_pattern(spec.pattern)
        for path, value in spread(ctx, declared, spec.default).items():
            if only_on_delete and path != declared and _says_absent(ctx, path):
                continue  # a child that declares on_delete: absent does not come back with its parent
            found[path] = value
    return found


def _says_absent(ctx: SchemaMergeContext, path: Path) -> bool:
    field = ctx.schema.field_for(path)
    return field is not None and field.on_delete == "absent"


def defaults_layer(ctx: SchemaMergeContext) -> Layer:
    """Every default as a layer, to be overlaid first (below the lowest tier)."""
    return _collect(ctx, only_on_delete=False)


def delete_fallbacks(ctx: SchemaMergeContext) -> dict[Path, Any]:
    """The defaults of the fields that say `on_delete: default`: what a deleted field shows instead of being absent."""
    return _collect(ctx, only_on_delete=True)
