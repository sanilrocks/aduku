"""Explain (SPEC 12.3): which entry gave a path its value, under which rule, and what it superseded.

Worked out from the ledger when asked, not recorded on every append. The winner is the entry after which the path's
value last changed, folding the entries that touch it in merge order (tier rank, then seq). An entry is *essential* if
deriving without it gives a different value there; the winner and the essential entries are the inputs, and the
shadowed entries are those that make no difference. The cost is quadratic in the number of entries touching the path. This holds for any merge rule, built-in or custom, because it
only uses the engine's own derivation.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from aduku.behaviors.registry import BehaviorRegistry
from aduku.engine.derive import Prepared, build_document, derive, fold_entries, merge_tiers
from aduku.ion_codec import ABSENT, FrozenStruct, to_canonical
from aduku.model import Entry, Explanation, ModelError
from aduku.paths import Path, PathError, Segment, format_path, parse_path
from aduku.schema.model import Schema

__all__ = ["explain", "value_at"]


def value_at(document: FrozenStruct, path: Sequence[Segment]) -> Any:
    """The value at a concrete path of an assembled document, or ABSENT."""
    node: Any = document
    for segment in path:
        key = segment.name if segment.key is None else f"{segment.name}[{segment.key}]"
        if not isinstance(node, FrozenStruct) or key not in node:
            return ABSENT
        node = node[key]
    return node


def _touches(entry_path: Path, path: Path) -> bool:
    """True if a write at `entry_path` can change the value at `path`: it is the path, above it, or inside it."""
    shorter = min(len(entry_path), len(path))
    return entry_path[:shorter] == path[:shorter]


def _same(a: Any, b: Any) -> bool:
    if a is ABSENT or b is ABSENT:
        return a is b
    return to_canonical(a) == to_canonical(b)


def explain(
    schema: Schema,
    behaviors: BehaviorRegistry,
    prepared: Prepared,
    entries: Sequence[Entry],
    path_text: str,
    model_errors: Sequence[ModelError],
) -> Explanation:
    """`path_text` has been checked: it parses and the schema declares it."""
    path = parse_path(path_text)
    field = schema.field_for(path)
    derived = schema.derived_field_at_or_above(path)
    if derived is not None:
        spec = derived.derived_spec
        name = spec.get("macro") or spec.get("hook") if isinstance(spec, FrozenStruct) else None
        return Explanation(path_text, None, derived.derived, None, str(name) if name else None, (), (), ())
    rule = prepared.ctx.rule_for(path)
    label = f"{rule.name}@{rule.version}"

    full = derive(schema, behaviors, entries, prepared)
    value = value_at(full.document, path)
    skipped = set(full.state.skipped)
    relevant = []
    for entry in entries:
        try:
            touching = _touches(parse_path(entry.path), path)
        except PathError:
            continue
        if touching and entry.seq not in skipped and entry.tier in schema.tiers:
            relevant.append(entry)
    relevant.sort(key=lambda e: (schema.tier_rank(e.tier), e.seq))
    errors = tuple(e for e in model_errors if _covers(e.path_scope, path))
    shown = None if value is ABSENT else value

    if not relevant:
        default = field is not None and field.has_default and value is not ABSENT
        return Explanation(path_text, shown, "default" if default else None, None, label, (), (), errors)

    # The winner is the entry after which the value last changed, folding the entries in merge order.
    state = None
    seen: Any = value_at(derive(schema, behaviors, [], prepared).document, path)
    changer: Entry | None = None
    for entry in relevant:
        state = fold_entries(schema, behaviors, state, [entry], prepared)
        document = build_document(schema, behaviors, merge_tiers(schema, behaviors, state, prepared), prepared)
        now = value_at(document, path)
        if not _same(now, seen):
            changer = entry
        seen = now
    winner = changer or relevant[-1]
    # An entry is essential if deriving without it gives another value; the rest make no difference.
    essential = {
        entry.id
        for entry in relevant
        if not _same(
            value_at(derive(schema, behaviors, [e for e in relevant if e.id != entry.id], prepared).document, path),
            seen,
        )
    }
    inputs = [entry for entry in relevant if entry.id in essential or entry.id == winner.id]
    used = {entry.id for entry in inputs}
    shadowed = [entry for entry in relevant if entry.id not in used]
    return Explanation(
        path_text,
        shown,
        winner.id,
        winner.seq,
        label,
        tuple(entry.id for entry in inputs),
        tuple(entry.id for entry in shadowed),
        errors,
    )


def _covers(scope: str, path: Path) -> bool:
    """True if a model error with this path scope concerns `path`: the scope is the path or lies above it."""
    try:
        above = parse_path(scope)
    except PathError:
        return scope == format_path(path)
    return path[: len(above)] == above
