"""P0 admission, stage 1 (V0): schema errors. Pure: no storage, no clock, no IO.

Every reason of every entry is collected so a rejected request lists them all (FR-007). Rule checks (V1 to V3) run
only when this stage is clean, so they are not part of this module.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from aduku.behaviors.merge import MergeTypeError, SchemaMergeContext
from aduku.behaviors.registry import BehaviorRegistry
from aduku.engine.squash import spread
from aduku.errors import ErrorCode, Reason
from aduku.ion_codec import ABSENT, FrozenStruct
from aduku.model import EntryInput, Identity
from aduku.paths import Path, PathError, Segment, format_path, is_field_name, matches, parse_path
from aduku.schema.model import Schema, conforms

__all__ = ["check_request"]

CORE_OPS = frozenset({"set", "delete"})


def check_request(
    schema: Schema,
    entries: Sequence[EntryInput],
    contributor: Identity,
    lenient: bool,
    behaviors: BehaviorRegistry,
    ctx: SchemaMergeContext | None = None,
) -> list[Reason]:
    """Schema errors for a request, ordered by entry index; request-level reasons come last.

    Each written value is also checked to be mergeable by its rule, children of a struct included, so that no
    accepted entry can later make the document impossible to derive. `ctx` is the schema's merge context, when the
    caller already has one.
    """
    ctx = ctx or SchemaMergeContext(schema, behaviors)
    reasons: list[Reason] = []
    seen_idem_keys: set[str] = set()
    for index, entry in enumerate(entries):
        reasons.extend(_check_entry(schema, index, entry, contributor, ctx))
        if entry.idem_key is not None:
            if entry.idem_key in seen_idem_keys:
                reasons.append(
                    Reason(
                        ErrorCode.DUPLICATE_IDEM_KEY,
                        "idempotency key is used twice in this request",
                        entry_index=index,
                        path=entry.path,
                    )
                )
            seen_idem_keys.add(entry.idem_key)
    if lenient and not _flag_permitted(schema, entries, contributor):
        reasons.append(
            Reason(
                ErrorCode.LENIENT_NOT_PERMITTED,
                "the contributor and tiers of this request may not use the non-rejection flag",
            )
        )
    return reasons


def _flag_permitted(schema: Schema, entries: Sequence[EntryInput], contributor: Identity) -> bool:
    """Permitted when the contributor is listed, or when every tier used in the request is listed (FR-022)."""
    if contributor.id in schema.lenient_contributors:
        return True
    # an empty request has nothing that could be authorized, so it is never permitted
    return bool(entries) and all(entry.tier in schema.lenient_tiers for entry in entries)


def _check_entry(
    schema: Schema, index: int, entry: EntryInput, contributor: Identity, ctx: SchemaMergeContext
) -> list[Reason]:
    found: list[Reason] = []

    def add(code: ErrorCode, message: str, rule: str | None = None) -> None:
        found.append(Reason(code, message, entry_index=index, rule=rule, path=entry.path))

    if entry.tier not in schema.tiers:
        add(ErrorCode.TIER_NOT_PERMITTED, f"tier {entry.tier!r} is not defined by the schema")
    elif entry.tier not in contributor.tiers:
        add(ErrorCode.TIER_NOT_PERMITTED, f"the contributor may not use tier {entry.tier!r}")

    if entry.meta is not None and not conforms("any", entry.meta):
        add(ErrorCode.TYPE_MISMATCH, "meta contains a value with no Ion form")

    try:
        path = parse_path(entry.path)
    except PathError as error:
        add(ErrorCode.UNKNOWN_PATH, f"invalid path: {error}")
        return found

    derived = schema.derived_field_at_or_above(path)
    if derived is not None:
        add(ErrorCode.FIELD_NOT_WRITABLE, f"{derived.pattern} is a {derived.derived} field and cannot be written")
        return found

    field = schema.field_for(path)
    if field is None:
        add(ErrorCode.UNKNOWN_PATH, "the schema does not declare this path")
        return found

    # Only set and delete exist in this version, whatever else a field lists (SPEC 4.3: extension operations are v1.1).
    allowed_ops = (frozenset(field.ops) & CORE_OPS) if field.ops is not None else CORE_OPS
    if entry.op not in allowed_ops:
        add(ErrorCode.OP_NOT_ALLOWED, f"operation {entry.op!r} is not allowed for {field.pattern}")
    elif entry.op == "set":
        type_text = field.type or "any"
        if not conforms(type_text, entry.value):
            add(ErrorCode.TYPE_MISMATCH, f"the value does not conform to type {type_text}")
        else:
            found.extend(_check_children(schema, ctx, index, entry, path, contributor))

    if not _may_write(schema, path, entry.tier, contributor):
        add(ErrorCode.NOT_AUTHORIZED, f"not authorized to write {field.pattern}", "authority")
    if entry.op == "delete":  # a delete wipes everything beneath the path, so the children's rules apply to it too
        for child in schema.fields:
            below = child.path_pattern
            if len(below) > len(path) and matches(below[: len(path)], path):
                declared = schema.authority_of(child)
                if declared is not None and not _allowed(declared[0], entry.tier, contributor):
                    add(
                        ErrorCode.NOT_AUTHORIZED,
                        f"not authorized to write {child.pattern}, which this delete wipes",
                        "authority",
                    )
                    break
    return found


def _allowed(authority: tuple[str, ...], tier: str, contributor: Identity) -> bool:
    """One of the entry's tier or the contributor's roles is in the list (SPEC 11)."""
    return tier in authority or bool(set(contributor.roles) & set(authority))


def _may_write(schema: Schema, path: Path, tier: str, contributor: Identity) -> bool:
    """Whether the effective authority of `path` (its own list, else the nearest above it) lets this entry write it."""
    declared = schema.authority_for(path)
    return declared is None or _allowed(declared[0], tier, contributor)


def _bad_keys(ctx: SchemaMergeContext, path: Path, value: Any) -> list[Path]:
    """Paths of structs, written to field-wise fields, that have a key which cannot be a path segment."""
    if not (ctx.rule_for(path).field_wise and isinstance(value, FrozenStruct)):
        return []
    found = [path] if not all(is_field_name(key) for key in value) else []
    for key in value:
        if is_field_name(key):
            found.extend(_bad_keys(ctx, (*path, Segment(key)), value[key]))
    return found


def _check_children(
    schema: Schema, ctx: SchemaMergeContext, index: int, entry: EntryInput, path: Path, contributor: Identity
) -> list[Reason]:
    """What a written value really writes: a struct at a field-wise field is spread over its child paths, and each
    child must be writable by this contributor, not derived, typed right and mergeable by its rule."""
    found: list[Reason] = []

    def add(code: ErrorCode, message: str, rule: str | None = None) -> None:
        found.append(Reason(code, message, entry_index=index, rule=rule, path=entry.path))

    for bad in _bad_keys(ctx, path, entry.value):
        add(
            ErrorCode.TYPE_MISMATCH,
            f"the struct written at {format_path(bad)} has a key that is not a valid field name",
        )
    if found:
        return found
    for child, state in spread(ctx, path, entry.value).items():
        where = format_path(child)
        field = schema.field_for(child)
        if child != path:
            if field is None:
                add(ErrorCode.UNKNOWN_PATH, f"the schema does not declare {where}, which this struct write sets")
                continue
            derived = schema.derived_field_at_or_above(child)
            if derived is not None:
                add(ErrorCode.FIELD_NOT_WRITABLE, f"{where} is under {derived.pattern}, a {derived.derived} field")
                continue
            if field.ops is not None and "set" not in field.ops:
                add(
                    ErrorCode.OP_NOT_ALLOWED,
                    f"operation 'set' is not allowed for {where}, which this struct write sets",
                )
                continue
            if not _may_write(schema, child, entry.tier, contributor):
                add(ErrorCode.NOT_AUTHORIZED, f"not authorized to write {where}", "authority")
                continue
            if field.type is not None and not conforms(field.type, state):
                add(ErrorCode.TYPE_MISMATCH, f"the value of {where} does not conform to type {field.type}")
                continue
        rule = ctx.rule_for(child)
        try:
            rule.impl(child, ABSENT, state, ctx)
        except MergeTypeError as error:
            add(ErrorCode.TYPE_MISMATCH, f"the value of {where} cannot be merged by {rule.name}: {error}")
    return found
