"""The compaction planner and the comparison of two derivations (feature 005, research R-7 to R-9). Pure.

A compaction replaces the entries of a range, up to a request boundary, by one synthetic `layer` entry per tier: the tier's
folded layer. Folding is associative, so the document does not change; what must also survive is what a replay of requests
would have computed on the way (model errors with who introduced them, skipped seqs), which the service records beside the
plan, and the idempotency keys and the request index, which the plan lists. Nothing here reads a clock or a store.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime

from aduku.behaviors.registry import BehaviorRegistry
from aduku.engine.derive import Prepared, fold_entries
from aduku.engine.layer_codec import encode_layer
from aduku.engine.squash import spread
from aduku.errors import ErrorCode, RejectedError
from aduku.ion_codec import to_canonical
from aduku.model import CompactionPlan, CompactionRecord, Entry, Explanation, ModelError, SourceRow
from aduku.paths import PathError, format_path, parse_path
from aduku.schema.model import Schema

__all__ = [
    "COMPACTION_CONTRIBUTOR",
    "EarlierCompaction",
    "Observed",
    "compacted_ledger",
    "differences",
    "id_map",
    "shape_problems",
    "plan",
    "watermark_for",
]

COMPACTION_CONTRIBUTOR = "aduku:compaction"
MAX_REPORTED = 5


@dataclass(frozen=True)
class EarlierCompaction:
    """What an earlier compaction recorded that a later one needs when it takes that compaction's synthetic entries."""

    first_tx_time: datetime | None
    last_tx_time: datetime | None
    has_valid_time: bool


def watermark_for(entries: Sequence[Entry], through_seq: int) -> int:
    """The highest seq at or below `through_seq` that ends a request (a request is atomic, so it is never split); 0 if none."""
    best = 0
    ordered = sorted(entries, key=lambda e: e.seq)
    for index, entry in enumerate(ordered):
        if entry.seq > through_seq:
            break
        following = ordered[index + 1] if index + 1 < len(ordered) else None
        if following is None or following.request_id != entry.request_id:
            best = entry.seq
    return best


def plan(
    entries: Sequence[Entry],
    schema: Schema,
    behaviors: BehaviorRegistry,
    prepared: Prepared,
    *,
    doc: str,
    through_seq: int,
    compaction_id: str,
    new_id: Callable[[], str],
    created: datetime,
    policy: str,
    base_errors: str,
    base_skipped: tuple[int, ...],
    earlier: Mapping[str, EarlierCompaction] | None = None,
) -> CompactionPlan | None:
    """What compacting `entries` (the live ledger, in seq order) up to `through_seq` changes; None if there is nothing to do.

    Raises RejectedError for a range written under more than one schema version (MIXED_SCHEMA_RANGE) and for a
    non-idempotent field with an entry that has no idempotency key (IDEM_KEY_MISSING).
    """
    earlier = earlier or {}
    ordered = sorted(entries, key=lambda e: e.seq)
    watermark = watermark_for(ordered, through_seq)
    span = [e for e in ordered if e.seq <= watermark]
    if not span:
        return None
    tiers_seen = [e.tier for e in span]
    if all(e.synthetic for e in span) and len(set(tiers_seen)) == len(tiers_seen):
        return None  # already one synthetic entry per tier: nothing to shrink
    contributed = [e for e in span if not e.synthetic]
    if len({e.schema_version for e in contributed}) > 1:
        raise RejectedError(
            ErrorCode.MIXED_SCHEMA_RANGE,
            message="the range holds entries written under more than one schema version; choose a watermark within one",
        )
    _check_keys(schema, prepared, contributed)

    synthetic: list[Entry] = []
    for tier in dict.fromkeys(tiers_seen):  # first-seen order; a tier the schema lacks gets an empty layer
        mine = [e for e in span if e.tier == tier]
        layer = (
            fold_entries(schema, behaviors, None, mine, prepared).layers.get(tier, {}) if tier in schema.tiers else {}
        )
        synthetic.append(
            Entry(
                id=new_id(),
                doc=doc,
                seq=max(e.seq for e in mine),
                request_id=compaction_id,
                tier=tier,
                path="/",
                op="layer",
                value=encode_layer(layer),
                contributor=COMPACTION_CONTRIBUTOR,
                tx_time=max(e.tx_time for e in mine),
                schema_version=schema.version,
                synthetic=compaction_id,
            )
        )
    synthetic.sort(key=lambda e: e.seq)

    stamps: list[datetime] = []
    has_valid = False
    for e in span:
        info = earlier.get(e.synthetic) if e.synthetic is not None else None
        if info is not None:  # a synthetic entry stands for the interval and the valid times its compaction recorded
            stamps.extend(t for t in (info.first_tx_time, info.last_tx_time) if t is not None)
            has_valid = has_valid or info.has_valid_time
        else:
            stamps.append(e.tx_time)
            has_valid = has_valid or e.valid_time is not None
    record = CompactionRecord(
        id=compaction_id,
        doc=doc,
        watermark_seq=watermark,
        schema_version=schema.version,
        fold_fingerprint=schema.fold_fingerprint,
        created=created,
        policy=policy,
        base_errors=base_errors,
        base_skipped=tuple(sorted(base_skipped)),
        archived=True,
        first_tx_time=min(stamps) if stamps else None,
        last_tx_time=max(stamps) if stamps else None,
        has_valid_time=has_valid,
    )
    sources = tuple(SourceRow(compaction_id, e.id, e.seq, e.tier, e.idem_key) for e in span)
    index = tuple((e.request_id, e.id, e.seq) for e in contributed)
    keys = tuple(e.idem_key for e in contributed if e.idem_key is not None)
    return CompactionPlan(record, tuple(span), tuple(synthetic), sources, index, keys)


def _check_keys(schema: Schema, prepared: Prepared, contributed: Sequence[Entry]) -> None:
    """A field whose merge behavior is not idempotent is compacted only with the keys that make a repeat detectable."""
    for entry in contributed:
        if entry.idem_key is not None or entry.purged or entry.op != "set" or entry.tier not in schema.tiers:
            continue
        try:
            leaves = spread(prepared.ctx, parse_path(entry.path), entry.value)
        except PathError:
            continue
        for path in leaves:
            if not prepared.ctx.rule_for(path).laws.idempotent:
                raise RejectedError(
                    ErrorCode.IDEM_KEY_MISSING,
                    message=f"{format_path(path)} is merged by a rule that is not idempotent and an entry has no idempotency key",
                )


def shape_problems(compaction: CompactionPlan) -> list[str]:
    """What is wrong with a plan on its face: it must list every replaced entry as a source, carry every idempotency key and
    the request index of the contributed ones, and hold one synthetic entry per tier of the range."""
    problems: list[str] = []
    replaced = [e for e in compaction.replaced]
    contributed = [e for e in replaced if not e.synthetic]
    if {row.source_id for row in compaction.sources} != {e.id for e in replaced}:
        problems.append("the sources do not list exactly the replaced entries")
    if set(compaction.retired_keys) != {e.idem_key for e in contributed if e.idem_key is not None}:
        problems.append("the idempotency keys of the replaced entries are not all carried")
    if {(rid, eid, seq) for rid, eid, seq in compaction.request_index} != {
        (e.request_id, e.id, e.seq) for e in contributed
    }:
        problems.append("the request index does not list the replaced requests")
    if sorted(e.tier for e in compaction.synthetic) != sorted({e.tier for e in replaced}):
        problems.append("there is not exactly one synthetic entry per tier")
    return problems


def compacted_ledger(compaction: CompactionPlan, entries: Sequence[Entry]) -> list[Entry]:
    """The ledger after the plan: the synthetic entries, then the entries beyond the watermark."""
    beyond = [e for e in entries if e.seq > compaction.record.watermark_seq]
    return sorted([*compaction.synthetic, *beyond], key=lambda e: e.seq)


def id_map(compaction: CompactionPlan) -> dict[str, str]:
    """For every replaced entry, the id of the synthetic entry of its tier that now stands for it."""
    by_tier = {e.tier: e.id for e in compaction.synthetic}
    return {row.source_id: by_tier[row.tier] for row in compaction.sources}


@dataclass(frozen=True)
class Observed:
    """Everything a reader can see of a document, to compare before and after a compaction."""

    document: str
    errors: tuple[ModelError, ...]
    skipped: tuple[int, ...]
    conflicts: tuple[str, ...]
    explained: Mapping[str, Explanation] = field(default_factory=dict)


def differences(before: Observed, after: Observed, mapping: Mapping[str, str]) -> list[str]:
    """What differs between two views of one document, in a fixed order; empty if they agree (research R-9).

    `mapping` sends a replaced entry's id to the synthetic entry that stands for it. An explain answer agrees when it has the
    same value, rule and model errors and the same winner, inputs and shadowed entries once replaced entries are named by
    their synthetic entry: a synthetic entry is an input if any of its sources was, and shadowed if none was.
    """
    found: list[str] = []
    if before.document != after.document:
        found.append("the document differs")
    if before.errors != after.errors:
        found.append("the model errors (or who introduced them) differ")
    if before.skipped != after.skipped:
        found.append("the skipped entries differ")
    if before.conflicts != after.conflicts:
        found.append("the conflicting paths differ")
    for path in sorted(before.explained):
        old, new = before.explained[path], after.explained.get(path)
        if new is None:
            found.append(f"{path}: no explain answer after")
            continue
        found.extend(_explain_differences(path, old, new, mapping))
    return found[:MAX_REPORTED] if len(found) > MAX_REPORTED else found


def _named(ids: Sequence[str], mapping: Mapping[str, str]) -> set[str]:
    return {mapping.get(item, item) for item in ids}


def _explain_differences(path: str, old: Explanation, new: Explanation, mapping: Mapping[str, str]) -> list[str]:
    found: list[str] = []
    if to_canonical(old.value) != to_canonical(new.value):
        found.append(f"{path}: the value differs")
    if old.rule != new.rule:
        found.append(f"{path}: the rule differs")
    if old.model_errors != new.model_errors:
        found.append(f"{path}: the model errors differ")
    old_winner = mapping.get(old.winner, old.winner) if old.winner is not None else None
    if old_winner != new.winner:
        found.append(f"{path}: the winner differs")
    inputs = _named(old.inputs, mapping)
    if inputs != set(new.inputs):
        found.append(f"{path}: the inputs differ")
    shadowed = _named(old.shadowed, mapping) - inputs
    if shadowed != set(new.shadowed):
        found.append(f"{path}: the shadowed entries differ")
    return found
