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
from aduku.engine.compute import compute_fields
from aduku.engine.derive import Prepared, build_document, derive, fold_entries, merge_tiers
from aduku.engine.document import value_at
from aduku.engine.effects import EffectView
from aduku.engine.layer_codec import decode_layer
from aduku.ion_codec import ABSENT, Annotated, FrozenStruct, IonError, to_canonical
from aduku.model import Entry, Explanation, ModelError
from aduku.paths import Path, PathError, format_path, parse_path
from aduku.schema.model import FieldSpec, Schema

__all__ = ["entry_touches", "explain", "paths_touch"]


def _touches(entry_path: Path, path: Path) -> bool:
    """True if a write at `entry_path` can change the value at `path`: it is the path, above it, or inside it."""
    shorter = min(len(entry_path), len(path))
    return entry_path[:shorter] == path[:shorter]


def paths_touch(entry_path: Path, path: Path) -> bool:
    """True if a write at `entry_path` can change the value at `path` (the same path, above it or inside it)."""
    return _touches(entry_path, path)


def entry_touches(entry: Entry, path: Path) -> bool:
    """True if `entry` can change (or, if purged, could have changed) the value at `path`."""
    return _touching(entry, path)


def _touching(entry: Entry, path: Path) -> bool:
    """True if `entry` can change the value at `path`. A `layer` entry (feature 005) touches every path its layer holds."""
    if entry.op == "layer":
        return any(_touches(written, path) for written in decode_layer(entry.value))
    return _touches(parse_path(entry.path), path)


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
    effects: EffectView | None = None,
) -> Explanation:
    """`path_text` has been checked: it parses and the schema declares it."""
    path = parse_path(path_text)
    field = schema.field_for(path)
    derived = schema.derived_field_at_or_above(path)
    if derived is not None and derived.derived == "computed":
        return _explain_computed(schema, behaviors, prepared, entries, path_text, path, derived, model_errors, effects)
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
        if entry.purged:  # a placeholder: it has no effect, so it is neither winner, input nor shadowed (feature 005)
            continue
        try:
            touching = _touching(entry, path)
        except (PathError, IonError):
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


def _explain_computed(
    schema: Schema,
    behaviors: BehaviorRegistry,
    prepared: Prepared,
    entries: Sequence[Entry],
    path_text: str,
    path: Path,
    derived: FieldSpec,
    model_errors: Sequence[ModelError],
    effects: EffectView | None = None,
) -> Explanation:
    """A computed path: the macro or hook, the values it was computed from, and the entries the field now shadows."""
    full = derive(schema, behaviors, entries, prepared)
    computed = compute_fields(schema, behaviors, full.document, prepared.computed, effects)
    result = computed.results.get(derived.pattern)
    skipped = set(full.state.skipped)
    owner = parse_path(derived.pattern)
    shadowed = tuple(entry.id for entry in entries if entry.seq in skipped and _touches(parse_path(entry.path), owner))
    value = value_at(computed.document, path)
    plain = value.value if isinstance(value, Annotated) else value
    errors = tuple(e for e in model_errors if _covers(e.path_scope, path))
    return Explanation(
        path_text,
        None if plain is ABSENT else plain,
        "computed",
        None,
        result.source if result is not None else None,
        (),
        shadowed,
        errors,
        dependencies=result.dependencies if result is not None else (),
        effect_seq=result.effect_seq if result is not None else None,
    )


def _covers(scope: str, path: Path) -> bool:
    """True if a model error with this path scope concerns `path`: the scope is the path or lies above it."""
    try:
        above = parse_path(scope)
    except PathError:
        return scope == format_path(path)
    return path[: len(above)] == above
