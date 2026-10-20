"""The purge planner (feature 005, research R-12). Pure.

A purge erases contributions: the entries at or below a path (optionally of one tier), or entries named by id. An erased entry
stays in the ledger as a placeholder without value; an entry that wrote a struct above the path keeps the rest of it; the
synthetic entries a compaction wrote are folded again from the originals that remain, or, when those were expired, lose
what they hold at the path. Nothing here reads a store: the service hands over the entries and applies the changes.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any

from aduku.behaviors.merge import Recreated
from aduku.behaviors.registry import BehaviorRegistry
from aduku.engine.derive import Prepared, fold_entries
from aduku.engine.graph import dependency_paths, edges
from aduku.engine.layer_codec import Layer, decode_layer, encode_layer
from aduku.ion_codec import FrozenStruct, IonError
from aduku.model import CompactionRecord, Entry
from aduku.paths import Path, PathError, format_path, parse_path
from aduku.schema.model import Schema

__all__ = ["PurgeChanges", "downstream_fields", "plan"]


@dataclass(frozen=True)
class PurgeChanges:
    """The rows a purge replaces, by id, and what it counted."""

    live: tuple[Entry, ...]  # replacement entries for rows of the ledger
    archive: tuple[Entry, ...]  # replacement entries for rows of the archive
    erased: int  # contributed entries made into placeholders (each id once, live or archived)
    rewritten: int  # entries whose value changed without being erased (a struct above the path, a synthetic layer)
    requests: tuple[str, ...]  # requests whose stored hash goes: a hash of a value is still a copy of it
    paths: tuple[str, ...]  # the paths erased, to find the derived fields that read them
    expired_sources: bool  # a synthetic entry had no originals left: what it held at the path was dropped whole


def _key(segment: Any) -> str:
    return segment.name if segment.key is None else f"{segment.name}[{segment.key}]"


def _at_or_below(path: Path, scope: Path) -> bool:
    return len(path) >= len(scope) and path[: len(scope)] == scope


def _above(path: Path, scope: Path) -> bool:
    return len(path) < len(scope) and scope[: len(path)] == path


def _without(value: Any, relative: Sequence[Any]) -> Any:
    """`value` (a struct) without what is at `relative`; the same object if it holds nothing there."""
    if not isinstance(value, FrozenStruct) or not relative:
        return value
    key = _key(relative[0])
    if key not in value:
        return value
    if len(relative) == 1:
        return FrozenStruct({k: v for k, v in value.items() if k != key})
    inner = _without(value[key], relative[1:])
    return value if inner is value[key] else FrozenStruct({**value, key: inner})


def _strip_state(state: Any, relative: Sequence[Any]) -> Any:
    if isinstance(state, Recreated):
        inner = _without(state.value, relative)
        return state if inner is state.value else Recreated(inner)
    return _without(state, relative)


class _Planner:
    def __init__(
        self,
        archive: Sequence[tuple[str, Entry]],
        compactions: Mapping[str, CompactionRecord],
        schema: Schema,
        behaviors: BehaviorRegistry,
        prepared: Prepared,
        scope: Path | None,
        tier: str | None,
        entry_ids: frozenset[str],
    ) -> None:
        self.by_compaction: dict[str, list[Entry]] = {}
        for compaction_id, entry in archive:
            self.by_compaction.setdefault(compaction_id, []).append(entry)
        self.compactions = compactions
        self.schema, self.behaviors, self.prepared = schema, behaviors, prepared
        self.scope, self.tier, self.entry_ids = scope, tier, entry_ids
        # what a synthetic layer that cannot be folded again loses: (tier or None for any, path); by path it is the scope itself,
        # by entry id it is the path and tier of each erased entry, which a placeholder keeps
        self.targets: list[tuple[str | None, Path]] = [] if scope is None else [(tier, scope)]
        self.memo: dict[str, Entry] = {}
        self.erased: set[str] = set()
        self.rewritten: set[str] = set()
        self.expired = False

    def revise(self, entry: Entry) -> Entry:
        if entry.id not in self.memo:
            if entry.purged:
                result = entry
            elif entry.op == "layer":
                result = self._layer(entry)
            else:
                result = self._contributed(entry)
            self.memo[entry.id] = result
        return self.memo[entry.id]

    def _matches(self, entry: Entry, path: Path) -> bool:
        if entry.id in self.entry_ids:
            return True
        return (
            self.scope is not None and (self.tier is None or entry.tier == self.tier) and _at_or_below(path, self.scope)
        )

    def _contributed(self, entry: Entry) -> Entry:
        try:
            path = parse_path(entry.path)
        except PathError:
            return entry
        if self._matches(entry, path):
            self.erased.add(entry.id)
            return replace(entry, value=None, meta=None, valid_time=None, request_hash=None, purged=True)
        if (
            self.scope is not None
            and (self.tier is None or entry.tier == self.tier)
            and entry.op == "set"
            and _above(path, self.scope)
        ):
            cut = _without(entry.value, self.scope[len(path) :])
            if cut is not entry.value:
                self.rewritten.add(entry.id)
                return replace(entry, value=cut, request_hash=None)
        return entry

    def _layer(self, entry: Entry) -> Entry:
        record = self.compactions.get(entry.synthetic or "")
        children = [child for child in self.by_compaction.get(entry.synthetic or "", []) if child.tier == entry.tier]
        if children and record is not None and record.fold_fingerprint == self.schema.fold_fingerprint:
            revised = [self.revise(child) for child in children]
            if all(new is old for new, old in zip(revised, children, strict=True)):
                return entry
            self.rewritten.add(entry.id)
            layer = fold_entries(self.schema, self.behaviors, None, revised, self.prepared).layers.get(entry.tier, {})
            return replace(entry, value=encode_layer(layer))
        targets = [path for wanted_tier, path in self.targets if wanted_tier is None or wanted_tier == entry.tier]
        if not targets:  # a tier that was not named, or nothing named at all
            return entry
        try:
            held = decode_layer(entry.value)
        except IonError:
            return entry
        kept: Layer = {}
        for path, state in held.items():
            if any(_at_or_below(path, target) for target in targets):
                continue
            for target in targets:
                if _above(path, target):
                    state = _strip_state(state, target[len(path) :])
            kept[path] = state
        if kept == held:
            return entry
        self.expired = True
        self.rewritten.add(entry.id)
        return replace(entry, value=encode_layer(kept))


def plan(
    live: Sequence[Entry],
    archive: Sequence[tuple[str, Entry]],
    compactions: Mapping[str, CompactionRecord],
    schema: Schema,
    behaviors: BehaviorRegistry,
    prepared: Prepared,
    *,
    path: Path | None,
    tier: str | None,
    entry_ids: frozenset[str],
) -> PurgeChanges:
    """What purging `path` (with an optional `tier`) or the entries `entry_ids` changes in the ledger and in the archive."""
    planner = _Planner(archive, compactions, schema, behaviors, prepared, path, tier, entry_ids)
    for old in [
        *live,
        *(entry for _, entry in archive),
    ]:  # the paths of the entries named by id, for a layer that cannot be re-folded
        if old.id in entry_ids and old.synthetic is None:
            try:
                planner.targets.append((old.tier, parse_path(old.path)))
            except PathError:
                continue
    live_new = {entry.id: planner.revise(entry) for entry in live}
    archive_new = {entry.id: planner.revise(entry) for _, entry in archive}
    changed_requests = {
        old.request_id
        for old in [*live, *(entry for _, entry in archive)]
        if (new := planner.memo[old.id]) is not old and old.synthetic is None and (new.purged or new.value != old.value)
    }
    erased_paths: set[str] = set()
    for old in [*live, *(entry for _, entry in archive)]:
        if old.id in planner.erased and old.synthetic is None:
            try:
                erased_paths.add(format_path(parse_path(old.path)))
            except PathError:
                continue
    if path is not None:
        erased_paths.add(format_path(path))
    for table in (live_new, archive_new):  # the stored hash of every entry of an affected request goes too
        for entry_id, entry in list(table.items()):
            if entry.request_id in changed_requests and entry.request_hash is not None and entry.synthetic is None:
                table[entry_id] = replace(entry, request_hash=None)
    originals = {entry.id: entry for entry in live} | {entry.id: entry for _, entry in archive}
    return PurgeChanges(
        live=tuple(new for new in live_new.values() if new is not originals[new.id]),
        archive=tuple(new for new in archive_new.values() if new is not originals[new.id]),
        erased=len(planner.erased),
        rewritten=len(planner.rewritten - planner.erased),
        requests=tuple(sorted(changed_requests)),
        paths=tuple(sorted(erased_paths)),
        expired_sources=planner.expired,
    )


def downstream_fields(schema: Schema, paths: Sequence[Path]) -> tuple[str, ...]:
    """The derived fields (patterns, sorted) that read one of `paths`, directly or through another derived field."""

    def overlaps(read: str) -> bool:
        try:
            parsed = parse_path(read)
        except PathError:
            return False
        return any(_at_or_below(parsed, p) or _above(parsed, p) for p in paths)

    found = {
        spec.pattern for spec in schema.fields if spec.derived and any(overlaps(r) for r in dependency_paths(spec))
    }
    graph = edges(schema)
    grew = True
    while grew:
        grew = False
        for name, reads in graph.items():
            if name not in found and any(dep in found for dep in reads):
                found.add(name)
                grew = True
    return tuple(sorted(found))
