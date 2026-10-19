"""Immutable schema objects (SPEC 5) and the type system used by schemas."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Sequence
from dataclasses import dataclass, field, is_dataclass
from dataclasses import fields as dc_fields
from decimal import Decimal
from functools import cached_property
from typing import Any, Literal

from aduku.ion_codec import (
    MAX_DEPTH,
    TOMBSTONE,
    Annotated,
    FrozenStruct,
    IonError,
    Symbol,
    Timestamp,
    timestamp_representable,
    to_canonical,
)
from aduku.paths import Path, most_specific, parse_pattern

__all__ = [
    "AuditSettings",
    "BehaviorRef",
    "ComputedSpec",
    "FieldSpec",
    "ImpureSpec",
    "MISSING",
    "MacroArg",
    "Schema",
    "conforms",
    "parse_type",
]


class _Missing:
    def __repr__(self) -> str:
        return "MISSING"


MISSING = _Missing()

FIELD_CACHE_LIMIT = 10_000  # paths remembered per schema; keyed paths are unbounded in number

SCALARS = frozenset({"string", "symbol", "int", "decimal", "float", "bool", "timestamp"})
_LIST = re.compile(r"list<([a-z]+)>\Z")


def parse_type(text: str) -> tuple[str, str | None]:
    """Parse a type name into (kind, element kind). `list<string>` gives ("list", "string")."""
    if text in SCALARS or text in ("struct", "any", "list"):
        return (text, None)
    match = _LIST.match(text)
    if match and (match.group(1) in SCALARS or match.group(1) in ("struct", "any")):
        return ("list", match.group(1))
    raise ValueError(f"unknown type {text!r}")


def _scalar_conforms(kind: str, value: Any) -> bool:
    if kind == "any":
        return value is not TOMBSTONE
    if value is None or value is TOMBSTONE or isinstance(value, Annotated):
        return False
    if kind == "string":
        return isinstance(value, str) and not isinstance(value, Symbol)
    if kind == "symbol":
        return isinstance(value, Symbol)
    if kind == "int":
        return isinstance(value, int) and not isinstance(value, bool)
    if kind == "decimal":
        return isinstance(value, Decimal)
    if kind == "float":
        return isinstance(value, float)
    if kind == "bool":
        return isinstance(value, bool)
    if kind == "timestamp":
        return isinstance(value, Timestamp)
    if kind == "struct":
        return isinstance(value, FrozenStruct)
    return False


def _storable(value: Any, depth: int = 0) -> bool:
    """False if the value, or anything nested in it, has no canonical Ion form (blobs, clobs, foreign objects, or
    nesting deeper than MAX_DEPTH)."""
    if depth > MAX_DEPTH:
        return False
    if isinstance(value, Decimal):
        return value.is_finite()  # NaN and infinities have no Ion text
    if isinstance(value, Timestamp):
        return timestamp_representable(value)
    if value is None or isinstance(value, bool | int | float | str):
        return True
    if isinstance(value, tuple):
        return all(_storable(item, depth + 1) for item in value)
    if isinstance(value, FrozenStruct):
        return all(_storable(item, depth + 1) for item in value.values())
    return False


def conforms(type_text: str, value: Any) -> bool:
    """True if `value` conforms to the schema type. A null value conforms only to `any`.

    Values that cannot be stored (blobs, clobs) never conform, so they are rejected at admission.
    """
    return _storable(value) and _conforms(type_text, value)


def _conforms(type_text: str, value: Any) -> bool:
    kind, element = parse_type(type_text)
    if kind == "any":
        return value is not TOMBSTONE
    if kind == "list":
        if not isinstance(value, tuple):
            return False
        return element is None or all(_scalar_conforms(element, item) for item in value)
    return _scalar_conforms(kind, value)


def _describe(value: Any) -> str:
    """A stable text for a schema tree: dataclasses by field, sequences in order, Ion values as canonical text."""
    if value is MISSING:
        return "<missing>"
    if is_dataclass(value) and not isinstance(value, type):
        parts = (f"{item.name}={_describe(getattr(value, item.name))}" for item in dc_fields(value))
        return f"{type(value).__name__}({','.join(parts)})"
    if isinstance(value, tuple):
        return "(" + ",".join(_describe(item) for item in value) + ")"
    try:
        return type(value).__name__ + ":" + to_canonical(value)
    except (TypeError, IonError):
        return type(value).__name__ + ":" + repr(value)


@dataclass(frozen=True)
class BehaviorRef:
    name: str
    version: int = 1
    args: FrozenStruct = field(default_factory=FrozenStruct)
    custom: bool = False


# Platform bounds on an impure compute (feature 002, research R-8): a schema asking for more is refused at load.
MAX_HOOK_TIMEOUT = 30.0
MAX_HOOK_RETRIES = 5
MAX_HOOK_DEADLINE = 60.0


@dataclass(frozen=True)
class MacroArg:
    """One argument of a computed field: the path of a value in the document, or a literal."""

    path: str | None = None
    literal: Any = MISSING

    @property
    def is_literal(self) -> bool:
        return self.literal is not MISSING


@dataclass(frozen=True)
class ComputedSpec:
    """A pure computed field: a macro (preset or registered) over ordered arguments."""

    macro: str
    version: int = 1
    args: tuple[MacroArg, ...] = ()

    @property
    def paths(self) -> tuple[str, ...]:
        return tuple(arg.path for arg in self.args if arg.path is not None)


@dataclass(frozen=True)
class ImpureSpec:
    """An impure computed field: a registered hook called with its declared dependencies (SPEC 9.3.1)."""

    hook: str
    version: int
    depends: tuple[str, ...]
    reason: str
    timeout: float
    retry_max: int
    backoff: float
    deadline: float
    recompute_by: tuple[str, ...]
    failure_mode: str | None = None  # None: the field's own, then the schema's impure_failure_mode


@dataclass(frozen=True)
class FieldSpec:
    pattern: str
    type: str | None = None
    merge: BehaviorRef | None = None
    default: Any = MISSING
    on_delete: Literal["absent", "default"] | None = None  # None: not declared, which behaves as "absent"
    validate: tuple[BehaviorRef, ...] = ()
    authority: tuple[str, ...] | None = None
    ops: tuple[str, ...] | None = None
    redact: Any = None
    derived: Literal["computed", "deferred"] | None = None
    derived_spec: Any = None
    failure_mode: str | None = None
    computed: ComputedSpec | None = None
    impure: ImpureSpec | None = None
    on_missing: Literal["null", "absent", "error"] = "null"  # what a computed field does when an input is missing

    @property
    def effective_on_delete(self) -> str:
        return self.on_delete or "absent"

    @property
    def has_default(self) -> bool:
        return self.default is not MISSING

    @cached_property
    def path_pattern(self) -> Path:
        return parse_pattern(self.pattern)


@dataclass(frozen=True)
class AuditSettings:
    enabled: bool = False
    mode: Literal["full", "metadata_only"] = "full"
    max_entries: int | None = None
    max_age_seconds: int | None = None


@dataclass(frozen=True)
class Schema:
    id: str
    version: int
    tiers: tuple[str, ...]
    fields: tuple[FieldSpec, ...]
    lenient_tiers: tuple[str, ...] = ()
    lenient_contributors: tuple[str, ...] = ()
    roles: tuple[str, ...] = ()
    rejection_audit: AuditSettings = AuditSettings()
    document_validate: tuple[BehaviorRef, ...] = ()
    impure_failure_mode: str = "reject_on_failure"

    @cached_property
    def rules_fingerprint(self) -> str:
        """A digest of everything in this version that decides a document (tiers, fields, rules and their versions).

        A cached view is only used when it was derived under the same fingerprint (FR-026).
        """
        return hashlib.sha256(_describe(self).encode()).hexdigest()[:16]

    def tier_rank(self, tier: str) -> int:
        return self.tiers.index(tier)

    def has_tier(self, tier: str) -> bool:
        return tier in self.tiers

    def field_for(self, path: Sequence[Any]) -> FieldSpec | None:
        """The field declaration that governs `path` (exact, then `*`, then `**`), or None."""
        key = tuple(path)
        cache = self._lookups
        cached = cache.get(key, MISSING)  # one atomic read: another thread may clear the memo at any moment
        if cached is not MISSING:
            return cached if isinstance(cached, FieldSpec) else None
        best = most_specific(list(self._index), key)
        found = None if best is None else self._index[best]
        if len(cache) >= FIELD_CACHE_LIMIT:
            cache.clear()
        cache[key] = found
        return found

    @cached_property
    def _lookups(self) -> dict[tuple[Any, ...], FieldSpec | None]:
        return {}

    def derived_field_at_or_above(self, path: Sequence[Any]) -> FieldSpec | None:
        """The computed or deferred field that `path` is, or lies beneath, if any."""
        path = tuple(path)
        for length in range(1, len(path) + 1):
            spec = self.field_for(path[:length])
            if spec is not None and spec.derived is not None:
                return spec
        return None

    @cached_property
    def _index(self) -> dict[Path, FieldSpec]:
        """Parsed patterns, computed once per schema."""
        return {parse_pattern(spec.pattern): spec for spec in self.fields}
