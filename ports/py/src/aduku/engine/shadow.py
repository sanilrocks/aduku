"""Why an entry is left out of a document (feature 006, research R-4). Pure.

`fold_entries` already skips the entries a schema version cannot use and lists their seqs. This module says why, by running the same
checks in the same order for those seqs only, so that a reason can never disagree with the ledger and the version: nothing is stored.
The one check the fold did not make before is `entry_nonconformity`: an entry written under another version whose value does not
conform to the type now in force.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import TYPE_CHECKING

from aduku.behaviors.merge import SchemaMergeContext
from aduku.behaviors.registry import BehaviorRegistry
from aduku.engine.squash import spread
from aduku.model import Entry, Shadowed
from aduku.paths import PathError, parse_path
from aduku.schema.model import Schema, conforms

if TYPE_CHECKING:
    from aduku.engine.derive import Prepared

__all__ = ["entry_nonconformity", "shadow_reasons"]


def entry_nonconformity(schema: Schema, ctx: SchemaMergeContext, entry: Entry) -> str | None:
    """The type text a set entry's value fails under `schema`, or None. A struct write is checked child by child."""
    if entry.op != "set" or entry.purged:
        return None
    for path, state in spread(ctx, parse_path(entry.path), entry.value).items():
        field = schema.field_for(path)
        if (
            field is not None
            and field.type is not None
            and schema.derived_field_at_or_above(path) is None
            and not conforms(field.type, state)
        ):
            return field.type
    return None


def shadow_reasons(
    schema: Schema,
    behaviors: BehaviorRegistry,
    prepared: Prepared,
    entries: Sequence[Entry],
    skipped: Iterable[int],
) -> list[Shadowed]:
    """A reason for each entry whose seq is in `skipped`, in seq order: tier, derived, type, then merge."""
    wanted = set(skipped)
    found: list[Shadowed] = []
    for entry in sorted(entries, key=lambda e: e.seq):
        if entry.seq in wanted and not entry.purged:
            reason, detail = _why(schema, prepared.ctx, entry)
            found.append(Shadowed(entry.seq, entry.id, entry.tier, entry.path, reason, detail))
    return found


def _why(schema: Schema, ctx: SchemaMergeContext, entry: Entry) -> tuple[str, str]:
    if entry.tier not in schema.tiers:
        return "tier_not_in_schema", entry.tier
    if entry.op == "layer":
        return "cannot_be_merged", "compacted layer"
    try:
        path = parse_path(entry.path)
    except PathError:
        return "cannot_be_merged", "invalid path"
    derived = schema.derived_field_at_or_above(path)
    if derived is not None:
        return "now_derived", f"{derived.derived} {derived.pattern}"
    try:
        written = spread(ctx, path, entry.value) if entry.op == "set" else {path: None}
    except PathError:
        return "cannot_be_merged", "invalid path"
    for child in written:
        under = schema.derived_field_at_or_above(child)
        if under is not None:
            return "now_derived", f"{under.derived} {under.pattern}"
    failing = entry_nonconformity(schema, ctx, entry) if entry.schema_version != schema.version else None
    if failing is not None:
        return "no_longer_conforms", failing
    return "cannot_be_merged", ctx.rule_for(path).name
