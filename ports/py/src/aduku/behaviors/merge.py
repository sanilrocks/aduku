"""Built-in merge rules (SPEC 6.3) and the tombstone-aware fold step (SPEC 6.7). Pure.

A rule is `combine(path, old, new, ctx)` where `old` is a value or ABSENT and `new` is a value. `step` wraps a rule
with the tombstone semantics that apply to every rule, so the rules themselves never see a tombstone.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from aduku.behaviors.registry import Behavior, BehaviorRegistry, Laws
from aduku.errors import SchemaError
from aduku.ion_codec import (
    ABSENT,
    TOMBSTONE,
    FrozenStruct,
    Symbol,
    Timestamp,
    Value,
    timestamp_sort_key,
    to_canonical,
)
from aduku.paths import Path, Segment
from aduku.schema.model import Schema

__all__ = ["MergeContext", "MergeTypeError", "Recreated", "SchemaMergeContext", "register_builtins", "step", "unwrap"]


RULE_CACHE_LIMIT = 10_000  # paths remembered; map-like structs can have unboundedly many


class MergeTypeError(TypeError):
    """A merge rule was applied to values of a type it cannot combine (SPEC 6.5): the values are never merged."""


class MergeContext:
    """Gives a rule access to the fold step, so `struct` can merge each child by the child's own rule."""

    def step(self, path: Path, old: Any, new: Any) -> Any:
        raise NotImplementedError


@dataclass(frozen=True)
class Recreated:
    """A value written after a tombstone. It keeps the reset: whatever came before it stays discarded.

    Without this marker a squashed layer would forget that it contains a delete, and "squash each tier, then merge"
    would differ from the flat fold (for example max(0, [delete, -1]) would give 0 instead of -1).
    """

    value: Any


def unwrap(state: Any) -> Any:
    """The value a fold state stands for: ABSENT, TOMBSTONE or a plain value (the reset marker is dropped)."""
    return state.value if isinstance(state, Recreated) else state


def step(rule: Behavior, path: Path, old: Any, new: Any, ctx: MergeContext) -> Any:
    """One fold step. A state is ABSENT, TOMBSTONE, a plain value, or Recreated(value) (a value after a tombstone).

    A tombstone beats whatever came before it. A plain value written after a tombstone re-creates the field by
    merging into ABSENT and keeps the reset. A Recreated state discards everything before it. This operation is
    associative whatever the rule, which is what makes squashing per tier equal to the flat fold.
    """
    if new is ABSENT:
        return old
    if new is TOMBSTONE:
        return TOMBSTONE
    if isinstance(new, Recreated):
        return new
    if old is ABSENT:
        return rule.impl(path, ABSENT, new, ctx)
    if old is TOMBSTONE:
        return Recreated(rule.impl(path, ABSENT, new, ctx))
    if isinstance(old, Recreated):
        return Recreated(rule.impl(path, old.value, new, ctx))
    return rule.impl(path, old, new, ctx)


# -- the rules -----------------------------------------------------------------------------------------------------


def _lww(path: Path, old: Any, new: Any, ctx: MergeContext) -> Value:
    return new


def _order(value: Any) -> tuple[str, Any]:
    """(family, sort key) for the ordered types; anything else cannot be compared."""
    if isinstance(value, bool) or value is None or isinstance(value, Symbol):
        raise MergeTypeError(f"{type(value).__name__} has no order")
    if isinstance(value, int | float | Decimal):
        if value != value:
            raise MergeTypeError("NaN has no order")
        return ("number", value)
    if isinstance(value, str):
        return ("string", value)
    if isinstance(value, Timestamp):
        return ("timestamp", timestamp_sort_key(value))
    raise MergeTypeError(f"{type(value).__name__} has no order")


def _pick(old: Any, new: Any, greater: bool) -> Value:
    """max or min. Ties between equal values with different spellings are broken by canonical text, so the result
    does not depend on the order of the arguments."""
    family_new, key_new = _order(new)
    if old is ABSENT:
        return new
    family_old, key_old = _order(old)
    if family_old != family_new:
        raise MergeTypeError(f"cannot compare a {family_old} with a {family_new}")
    old_rank, new_rank = (key_old, to_canonical(old)), (key_new, to_canonical(new))
    if greater:
        return old if old_rank >= new_rank else new
    return old if old_rank <= new_rank else new


def _max(path: Path, old: Any, new: Any, ctx: MergeContext) -> Value:
    return _pick(old, new, True)


def _min(path: Path, old: Any, new: Any, ctx: MergeContext) -> Value:
    return _pick(old, new, False)


def _items(value: Any) -> tuple[Any, ...]:
    if not isinstance(value, tuple):
        raise MergeTypeError(f"union merges lists, not {type(value).__name__}")
    return value


def _union(path: Path, old: Any, new: Any, ctx: MergeContext) -> Value:
    """Set union, in order of first appearance; items are equal when their canonical text is equal."""
    seen: set[str] = set()
    merged: list[Any] = []
    for item in (*(() if old is ABSENT else _items(old)), *_items(new)):
        key = to_canonical(item)
        if key not in seen:
            seen.add(key)
            merged.append(item)
    return tuple(merged)


def _struct(path: Path, old: Any, new: Any, ctx: MergeContext) -> Value:
    """Field by field; each child merges by its own rule (found through the context)."""
    if not isinstance(new, FrozenStruct) or (old is not ABSENT and not isinstance(old, FrozenStruct)):
        raise MergeTypeError("struct merges structs")
    earlier: dict[str, Any] = dict(old) if isinstance(old, FrozenStruct) else {}
    merged: dict[str, Any] = {}
    for key in sorted(set(earlier) | set(new)):
        child = (*path, Segment(key))
        value = ctx.step(child, earlier.get(key, ABSENT), new.get(key, ABSENT))
        if value is not ABSENT:
            merged[key] = value
    return FrozenStruct(merged)


ORDERED = ("int", "decimal", "float", "string", "timestamp")


def register_builtins(registry: BehaviorRegistry) -> None:
    registry.register_merge("lww", 1, _lww, Laws(associative=True, idempotent=True))
    registry.register_merge("max", 1, _max, Laws(associative=True, commutative=True, idempotent=True), ORDERED)
    registry.register_merge("min", 1, _min, Laws(associative=True, commutative=True, idempotent=True), ORDERED)
    registry.register_merge("union", 1, _union, Laws(associative=True, idempotent=True), ("list",))
    registry.register_merge(
        "struct", 1, _struct, Laws(associative=True), ("struct",), field_wise=True
    )  # other laws are per child


# -- merging by what the schema declares ---------------------------------------------------------------------------


class SchemaMergeContext(MergeContext):
    """Resolves the rule for a path from the schema (default: last write wins) and folds with it."""

    def __init__(self, schema: Schema, behaviors: BehaviorRegistry) -> None:
        self._schema = schema
        self._behaviors = behaviors
        self._rules: dict[Path, Behavior] = {}  # the schema never changes, so a path's rule never does

    @property
    def schema(self) -> Schema:
        return self._schema

    def rule_for(self, path: Path) -> Behavior:
        found = self._rules.get(path)
        if found is None:
            found = self._resolve(path)
            if len(self._rules) >= RULE_CACHE_LIMIT:
                self._rules.clear()
            self._rules[path] = found
        return found

    def _resolve(self, path: Path) -> Behavior:
        field = self._schema.field_for(path)
        ref = field.merge if field is not None else None
        name, version = (ref.name, ref.version) if ref is not None else ("lww", 1)
        rule = self._behaviors.get(name, version)
        if rule is None or rule.kind != "merge":
            raise SchemaError(
                f"merge behavior {name}@{version} is not registered in this service, so schema {self._schema.id!r}"
                f" version {self._schema.version} cannot be used; register it or load the schema with this registry"
            )
        return rule

    def step(self, path: Path, old: Any, new: Any) -> Any:
        return step(self.rule_for(path), path, old, new, self)
