"""In-memory storage with the same transactional semantics as SQLite.

A transaction changes the live state in place and keeps an undo log; if the block raises, the log is replayed backwards.
A write therefore costs what it changes, not the size of the ledger, and a read costs nothing extra.
"""

from __future__ import annotations

import threading
from bisect import bisect_right
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta

from aduku.model import (
    CompactionPlan,
    CompactionRecord,
    DerivedRecord,
    Entry,
    HistoryRecord,
    PurgeApplication,
    PurgeRecord,
    SchedulerRun,
    SourceRow,
    StateChange,
)
from aduku.storage.base import (
    AuditRow,
    ConstraintError,
    EffectRecord,
    LayerRow,
    RequestRecord,
    ViewRow,
    effects_to_prune,
    live,
)


@dataclass
class _State:
    entries: dict[str, list[Entry]] = field(default_factory=dict)
    by_request: dict[str, list[Entry]] = field(default_factory=dict)
    requests: dict[str, RequestRecord] = field(default_factory=dict)
    request_keys: dict[tuple[str, str, str], str] = field(default_factory=dict)
    idem_keys: set[tuple[str, str]] = field(default_factory=set)
    views: dict[str, ViewRow] = field(default_factory=dict)
    effects: list[EffectRecord] = field(default_factory=list)
    effect_counter: int = 0
    derived: dict[tuple[str, str], DerivedRecord] = field(default_factory=dict)  # (doc, field) -> latest result
    runs: list[SchedulerRun] = field(default_factory=list)
    leases: dict[str, tuple[str, datetime]] = field(default_factory=dict)  # source -> (owner, expires)
    layers: dict[tuple[str, str], LayerRow] = field(default_factory=dict)  # (doc, tier) -> saved layer (feature 005)
    epochs: dict[str, int] = field(default_factory=dict)
    archive: dict[str, list[tuple[str, Entry]]] = field(
        default_factory=dict
    )  # doc -> (compaction id, original), seq order
    compactions: dict[str, list[CompactionRecord]] = field(default_factory=dict)  # doc -> oldest first
    sources: dict[str, list[SourceRow]] = field(default_factory=dict)  # compaction id -> replaced entries
    request_index: dict[str, list[tuple[str, int]]] = field(default_factory=dict)  # request id -> (entry id, seq)
    expiries: list[tuple[str, int, datetime, int]] = field(default_factory=list)
    request_effects: dict[str, tuple[str, str]] = field(default_factory=dict)  # request id -> (added, resolved) text
    purges: list[PurgeRecord] = field(default_factory=list)
    tiers_used: set[str] = field(
        default_factory=set
    )  # tiers of every entry ever appended, so the whole ledger need not be read
    schema_states: dict[str, tuple[int, int | None]] = field(default_factory=dict)  # id -> (generation, active)
    schema_pins: dict[str, dict[str, int]] = field(default_factory=dict)  # id -> doc -> version
    schema_served: dict[str, dict[int, tuple[str, str]]] = field(default_factory=dict)  # id -> version -> (fp, parts)
    schema_log: list[tuple[str, HistoryRecord]] = field(default_factory=list)  # (id, record), oldest first


class _MemoryTx:
    def __init__(self, state: _State, undo: list[Callable[[], None]]) -> None:
        self._s = state
        self._undo = undo
        self._open = True

    def finish(self) -> None:
        self._open = False

    @live
    def last_seq(self, doc: str) -> int:
        rows = self._s.entries.get(doc)
        return rows[-1].seq if rows else 0

    @live
    def entries(
        self, doc: str, since_seq: int = 0, limit: int | None = None, *, until_seq: int | None = None
    ) -> list[Entry]:
        rows = self._s.entries.get(doc, [])
        # seqs grow but may have gaps once history was compacted, so find the start by search
        start = bisect_right(rows, max(since_seq, 0), key=lambda e: e.seq)
        stop = len(rows) if until_seq is None else bisect_right(rows, until_seq, key=lambda e: e.seq)
        found = rows[start:stop]
        return found if limit is None else found[:limit]

    @live
    def entries_of_request(self, request_id: str) -> list[Entry]:
        return list(self._s.by_request.get(request_id, []))

    @live
    def tiers_in_use(self, doc: str | None = None) -> set[str]:
        if doc is None:
            return set(self._s.tiers_used)
        return {entry.tier for entry in self._s.entries.get(doc, [])}

    @live
    def compacted_docs(self) -> list[str]:
        return sorted(name for name, epoch in self._s.epochs.items() if epoch > 0)

    @live
    def doc_ids(self) -> list[str]:
        return sorted(self._s.entries)

    @live
    def find_request(self, doc: str, contributor: str, request_key: str) -> RequestRecord | None:
        request_id = self._s.request_keys.get((doc, contributor, request_key))
        return self._s.requests[request_id] if request_id is not None else None

    @live
    def idem_key_seen(self, doc: str, idem_key: str) -> bool:
        return (doc, idem_key) in self._s.idem_keys

    @live
    def append_request(self, request: RequestRecord, entries: Sequence[Entry]) -> None:
        state = self._s
        if request.request_id in state.requests:
            raise ConstraintError(f"request id {request.request_id!r} already used")
        key = (request.doc, request.contributor, request.request_key or "")
        if request.request_key is not None and key in state.request_keys:
            raise ConstraintError(f"request key {request.request_key!r} already used by this contributor")
        expected = self.last_seq(request.doc) + 1
        new_idem: set[tuple[str, str]] = set()
        for entry in entries:  # check everything before changing anything
            if entry.doc != request.doc:
                raise ConstraintError("all entries of a request must belong to its document")
            if entry.seq != expected:
                raise ConstraintError(f"expected seq {expected}, got {entry.seq}")
            expected += 1
            if entry.idem_key is not None:
                pair = (entry.doc, entry.idem_key)
                if pair in state.idem_keys or pair in new_idem:
                    raise ConstraintError(f"idempotency key {entry.idem_key!r} already used")
                new_idem.add(pair)

        rows = state.entries.setdefault(request.doc, [])
        before = len(rows)
        rows.extend(entries)
        state.by_request[request.request_id] = list(entries)
        state.idem_keys |= new_idem
        state.requests[request.request_id] = request
        new_tiers = {entry.tier for entry in entries} - state.tiers_used
        state.tiers_used |= new_tiers
        if request.request_key is not None:
            state.request_keys[key] = request.request_id

        def undo() -> None:
            del rows[before:]
            if not rows:
                state.entries.pop(request.doc, None)
            del state.by_request[request.request_id]
            state.idem_keys -= new_idem
            state.tiers_used -= new_tiers
            del state.requests[request.request_id]
            if request.request_key is not None:
                del state.request_keys[key]

        self._undo.append(undo)

    @live
    def get_view(self, doc: str) -> ViewRow | None:
        return self._s.views.get(doc)

    @live
    def put_view(self, view: ViewRow) -> None:
        previous = self._s.views.get(view.doc)
        self._s.views[view.doc] = view
        self._undo.append(lambda: self._restore_view(view.doc, previous))

    @live
    def drop_view(self, doc: str) -> None:
        previous = self._s.views.pop(doc, None)
        self._undo.append(lambda: self._restore_view(doc, previous))

    # -- saved layers and the epoch (feature 005) --------------------------------------------------------------------

    @live
    def get_layers(self, doc: str) -> list[LayerRow]:
        return sorted((r for (d, _), r in self._s.layers.items() if d == doc), key=lambda r: r.tier)

    @live
    def put_layers(self, rows: Sequence[LayerRow]) -> None:
        layers = self._s.layers
        before: dict[tuple[str, str], LayerRow | None] = {}
        for row in rows:
            key = (row.doc, row.tier)
            kept = layers.get(key)
            if kept is not None and (
                row.epoch < kept.epoch or (row.epoch == kept.epoch and row.tier_seq < kept.tier_seq)
            ):
                continue
            before.setdefault(key, kept)
            layers[key] = row
        self._undo.append(lambda: self._restore_layers(before))

    @live
    def drop_layers(self, doc: str) -> None:
        doomed = {key: row for key, row in self._s.layers.items() if key[0] == doc}
        for key in doomed:
            del self._s.layers[key]
        self._undo.append(lambda: self._restore_layers(dict(doomed)))

    @live
    def tier_last_seq(self, doc: str, tier: str, through: int) -> int:
        return max((e.seq for e in self._s.entries.get(doc, []) if e.tier == tier and e.seq <= through), default=0)

    @live
    def epoch(self, doc: str) -> int:
        return self._s.epochs.get(doc, 0)

    @live
    def bump_epoch(self, doc: str) -> int:
        epochs = self._s.epochs
        previous = epochs.get(doc, 0)
        epochs[doc] = previous + 1
        self._undo.append(lambda: epochs.__setitem__(doc, previous))
        return previous + 1

    @live
    def seq_at_tx_time(self, doc: str, moment: datetime) -> int:
        live_rows = self._s.entries.get(doc, [])
        archived = [entry for _, entry in self._s.archive.get(doc, [])]
        return max((e.seq for e in (*live_rows, *archived) if e.tx_time <= moment), default=0)

    @live
    def apply_compaction(self, plan: CompactionPlan) -> None:
        state = self._s
        doc = plan.record.doc
        live_rows = state.entries.get(doc, [])
        by_id = {entry.id: entry for entry in live_rows}
        for entry in plan.replaced:
            if by_id.get(entry.id) != entry:
                raise ConstraintError(f"entry {entry.id!r} is not in the ledger as the compaction expects")
        replaced = {entry.id for entry in plan.replaced}
        for entry in plan.synthetic:
            if entry.id in by_id and entry.id not in replaced:
                raise ConstraintError(f"entry id {entry.id!r} is already used")
        touched = {entry.request_id for entry in plan.replaced}
        before = (
            list(live_rows),
            list(state.archive.get(doc, [])),
            list(state.compactions.get(doc, [])),
            {rid: list(state.request_index.get(rid, [])) for rid in touched},
            {rid: list(state.by_request.get(rid, [])) for rid in touched},
            state.epochs.get(doc, 0),
        )
        kept = [entry for entry in live_rows if entry.id not in replaced]
        state.entries[doc] = sorted([*kept, *plan.synthetic], key=lambda entry: entry.seq)
        state.archive[doc] = sorted(
            [*state.archive.get(doc, []), *((plan.record.id, entry) for entry in plan.replaced)],
            key=lambda item: item[1].seq,
        )
        state.compactions[doc] = [*state.compactions.get(doc, []), plan.record]
        state.sources[plan.record.id] = list(plan.sources)
        for request_id, entry_id, seq in plan.request_index:
            state.request_index.setdefault(request_id, []).append((entry_id, seq))
        for request_id in touched:
            state.by_request[request_id] = [e for e in state.by_request.get(request_id, []) if e.id not in replaced]
        state.epochs[doc] = before[5] + 1
        for request_id, added, resolved in plan.request_effects:
            state.request_effects[request_id] = (added, resolved)

        def undo() -> None:
            for request_id, _, _ in plan.request_effects:
                state.request_effects.pop(request_id, None)
            state.entries[doc] = before[0]
            state.archive[doc] = before[1]
            state.compactions[doc] = before[2]
            state.sources.pop(plan.record.id, None)
            for rid in touched:
                state.request_index[rid] = before[3][rid]
                if not before[3][rid]:
                    del state.request_index[rid]
                state.by_request[rid] = before[4][rid]
            state.epochs[doc] = before[5]

        self._undo.append(undo)

    @live
    def compactions(self, doc: str) -> list[CompactionRecord]:
        return list(self._s.compactions.get(doc, []))

    @live
    def sources_of(self, compaction_id: str) -> list[SourceRow]:
        return sorted(self._s.sources.get(compaction_id, []), key=lambda row: row.seq)

    @live
    def archive_of(self, compaction_id: str) -> list[Entry]:
        found = [entry for rows in self._s.archive.values() for cid, entry in rows if cid == compaction_id]
        return sorted(found, key=lambda entry: entry.seq)

    @live
    def archived_entries(self, doc: str, through_seq: int | None = None) -> list[Entry]:
        rows = [entry for _, entry in self._s.archive.get(doc, [])]
        return [e for e in rows if through_seq is None or e.seq <= through_seq]

    @live
    def request_entries(self, request_id: str) -> list[tuple[str, int]]:
        live_entries = self._s.by_request.get(request_id, [])
        if live_entries:
            return [(entry.id, entry.seq) for entry in live_entries]
        return sorted(self._s.request_index.get(request_id, []), key=lambda item: item[1])

    @live
    def request_effects(self, request_id: str) -> tuple[str, str] | None:
        return self._s.request_effects.get(request_id)

    @live
    def expire_archive(self, doc: str, through_seq: int, at: datetime) -> int:
        state = self._s
        doomed = {c.id for c in state.compactions.get(doc, []) if c.archived and c.watermark_seq <= through_seq}
        if not doomed:
            return 0
        before = (list(state.archive.get(doc, [])), list(state.compactions.get(doc, [])), len(state.expiries))
        removed = [item for item in state.archive.get(doc, []) if item[0] in doomed]
        state.archive[doc] = [item for item in state.archive.get(doc, []) if item[0] not in doomed]
        state.compactions[doc] = [replace(c, archived=False) if c.id in doomed else c for c in state.compactions[doc]]
        state.expiries.append((doc, through_seq, at, len(removed)))

        def undo() -> None:
            state.archive[doc] = before[0]
            state.compactions[doc] = before[1]
            del state.expiries[before[2] :]

        self._undo.append(undo)
        return len(removed)

    def _restore_layers(self, before: Mapping[tuple[str, str], LayerRow | None]) -> None:
        for key, row in before.items():
            if row is None:
                self._s.layers.pop(key, None)
            else:
                self._s.layers[key] = row

    @live
    def append_effects(self, records: Sequence[EffectRecord]) -> list[int]:
        state = self._s
        before_len, before_counter = len(state.effects), state.effect_counter
        assigned: list[int] = []
        for record in records:
            state.effect_counter += 1
            assigned.append(state.effect_counter)
            state.effects.append(replace(record, effect_seq=state.effect_counter))

        def undo() -> None:
            del state.effects[before_len:]
            state.effect_counter = before_counter

        self._undo.append(undo)
        return assigned

    @live
    def latest_effects(self, doc: str) -> list[EffectRecord]:
        latest: dict[tuple[str, str, str], EffectRecord] = {}
        for record in self._s.effects:
            if record.doc == doc:
                latest[(record.field, record.fingerprint, record.hook)] = record  # appended in sequence order
        return sorted(latest.values(), key=lambda r: r.effect_seq or 0)

    @live
    def effects_seq(self, doc: str) -> int:
        return max((r.effect_seq or 0 for r in self._s.effects if r.doc == doc), default=0)

    @live
    def list_effects(self, doc: str, field: str | None = None, limit: int | None = None) -> list[EffectRecord]:
        found = [r for r in self._s.effects if r.doc == doc and (field is None or r.field == field)]
        return found if limit is None else found[max(len(found) - limit, 0) :]

    @live
    def prune_effects(
        self, doc: str | None, now: datetime, keep: int = 5, max_age: timedelta = timedelta(days=90)
    ) -> int:
        state = self._s
        scope = [r for r in state.effects if doc is None or r.doc == doc]
        doomed = set(effects_to_prune(scope, now, keep, max_age))
        if not doomed:
            return 0
        before = list(state.effects)
        state.effects[:] = [r for r in state.effects if r.effect_seq not in doomed]
        self._undo.append(lambda: state.effects.__setitem__(slice(None), before))
        return len(doomed)

    # -- purge (feature 005) -----------------------------------------------------------------------------------------

    @live
    def apply_purge(self, application: PurgeApplication) -> tuple[int, int]:
        state = self._s
        doc = application.doc
        live_rows = state.entries.get(doc, [])
        live_by_id = {entry.id: index for index, entry in enumerate(live_rows)}
        archive_rows = state.archive.get(doc, [])
        archive_by_id = {entry.id: index for index, (_, entry) in enumerate(archive_rows)}
        for entry in application.live:
            if entry.id not in live_by_id:
                raise ConstraintError(f"entry {entry.id!r} is not in the ledger")
        for entry in application.archive:
            if entry.id not in archive_by_id:
                raise ConstraintError(f"entry {entry.id!r} is not in the archive")
        saved = (
            list(live_rows),
            list(archive_rows),
            {rid: list(state.by_request.get(rid, [])) for rid in {e.request_id for e in application.live}},
            {rid: state.requests.get(rid) for rid in application.requests},
            [row for (d, _), row in state.layers.items() if d == doc],
            state.views.get(doc),
            state.epochs.get(doc, 0),
            list(state.effects),
            {key: value for key, value in state.derived.items() if key[0] == doc},
        )
        for entry in application.live:
            live_rows[live_by_id[entry.id]] = entry
            if entry.request_id in state.by_request:
                state.by_request[entry.request_id] = [
                    entry if e.id == entry.id else e for e in state.by_request[entry.request_id]
                ]
        for entry in application.archive:
            archive_rows[archive_by_id[entry.id]] = (archive_rows[archive_by_id[entry.id]][0], entry)
        for request_id in application.requests:
            if request_id in state.requests:
                state.requests[request_id] = replace(state.requests[request_id], request_hash=None)
        for key in [key for key in state.layers if key[0] == doc]:
            del state.layers[key]
        state.views.pop(doc, None)
        state.epochs[doc] = saved[6] + 1
        fields = set(application.fields)
        gone_effects = [r for r in state.effects if r.doc == doc and r.field in fields]
        state.effects[:] = [r for r in state.effects if not (r.doc == doc and r.field in fields)]
        gone_derived = [key for key in state.derived if key[0] == doc and key[1] in fields]
        for key in gone_derived:
            del state.derived[key]

        def undo() -> None:
            live_rows[:] = saved[0]
            archive_rows[:] = saved[1]
            for rid, rows in saved[2].items():
                state.by_request[rid] = rows
            for rid, record in saved[3].items():
                if record is not None:
                    state.requests[rid] = record
            for row in saved[4]:
                state.layers[(row.doc, row.tier)] = row
            if saved[5] is not None:
                state.views[doc] = saved[5]
            state.epochs[doc] = saved[6]
            state.effects[:] = saved[7]
            state.derived.update(saved[8])

        self._undo.append(undo)
        return len(gone_effects), len(gone_derived)

    @live
    def append_purge_log(self, record: PurgeRecord) -> None:
        purges = self._s.purges
        purges.append(record)

        def undo() -> None:
            purges.pop()

        self._undo.append(undo)

    @live
    def purge_log(self, doc: str) -> list[PurgeRecord]:
        return [r for r in self._s.purges if r.doc == doc]

    # -- the derived store of deferred fields (feature 003) ----------------------------------------------------------

    @live
    def get_derived(self, doc: str) -> list[DerivedRecord]:
        return sorted((r for (d, _), r in self._s.derived.items() if d == doc), key=lambda r: r.field)

    @live
    def put_derived(self, records: Sequence[DerivedRecord]) -> None:
        state = self._s
        before = {(r.doc, r.field): state.derived.get((r.doc, r.field)) for r in records}
        for record in records:
            state.derived[(record.doc, record.field)] = record
        self._undo.append(lambda: self._restore_derived(before))

    @live
    def list_derived(self, fields: Sequence[str], docs: Sequence[str] | None = None) -> list[DerivedRecord]:
        return self._matching(fields, docs)

    @live
    def mark_invalidated(self, fields: Sequence[str], docs: Sequence[str] | None = None) -> int:
        matching = self._matching(fields, docs)
        before = {(r.doc, r.field): r for r in matching}
        for record in matching:
            self._s.derived[(record.doc, record.field)] = replace(record, invalidated=True)
        self._undo.append(lambda: self._restore_derived(before))
        return len(matching)

    @live
    def delete_derived(self, docs: Sequence[str] | None = None, fields: Sequence[str] | None = None) -> int:
        doomed = {
            key: record
            for key, record in self._s.derived.items()
            if (docs is None or key[0] in docs) and (fields is None or key[1] in fields)
        }
        for key in doomed:
            del self._s.derived[key]
        self._undo.append(lambda: self._restore_derived(dict(doomed)))
        return len(doomed)

    @live
    def append_run(self, run: SchedulerRun) -> None:
        runs = self._s.runs
        runs.append(run)

        def undo() -> None:
            runs.pop()

        self._undo.append(undo)

    @live
    def list_runs(self, hook: str, limit: int | None = None) -> list[SchedulerRun]:
        found = [r for r in self._s.runs if r.hook == hook]
        return found if limit is None else found[max(len(found) - limit, 0) :]

    @live
    def take_lease(self, hook: str, owner: str, expires: datetime, now: datetime) -> bool:
        held = self._s.leases.get(hook)
        if held is not None and held[0] != owner and held[1] > now:
            return False
        self._set_lease(hook, (owner, expires))
        return True

    @live
    def renew_lease(self, hook: str, owner: str, expires: datetime) -> bool:
        held = self._s.leases.get(hook)
        if held is None or held[0] != owner:
            return False
        self._set_lease(hook, (owner, expires))
        return True

    @live
    def release_lease(self, hook: str, owner: str) -> None:
        held = self._s.leases.get(hook)
        if held is not None and held[0] == owner:
            self._set_lease(hook, None)

    @live
    def schema_generation(self, schema_id: str) -> int:
        return self._s.schema_states.get(schema_id, (0, None))[0]

    @live
    def schema_state(self, schema_id: str) -> tuple[int, int | None, dict[str, int]]:
        generation, active = self._s.schema_states.get(schema_id, (0, None))
        return generation, active, dict(self._s.schema_pins.get(schema_id, {}))

    @live
    def served_versions(self, schema_id: str) -> dict[int, tuple[str, str]]:
        return dict(self._s.schema_served.get(schema_id, {}))

    @live
    def apply_schema_change(self, change: StateChange, expect_generation: int | None) -> int:
        state = self._s
        sid = change.schema_id
        generation, active = state.schema_states.get(sid, (0, None))
        if expect_generation is not None and expect_generation != generation:
            raise ConstraintError(f"schema state generation is {generation}, expected {expect_generation}")
        served = state.schema_served.setdefault(sid, {})
        for version, fingerprint, _parts in change.served:
            if version in served and served[version][0] != fingerprint:
                raise ConstraintError(f"schema version {version} was already served with another fingerprint")
        pins = state.schema_pins.setdefault(sid, {})
        touched = [*([change.pin_doc] if change.pin_doc is not None else []), *(doc for doc, _ in change.pins)]
        before_pins = {doc: pins.get(doc) for doc in touched}
        added = [version for version, _, _ in change.served if version not in served]
        for version, fingerprint, parts in change.served:
            served.setdefault(version, (fingerprint, parts))
        if change.pin_doc is not None:
            if change.pin_version is None:
                pins.pop(change.pin_doc, None)
            else:
                pins[change.pin_doc] = change.pin_version
        pins.update(change.pins)
        new_active = active if change.active is None else change.active
        state.schema_states[sid] = (generation + 1, new_active)
        record = HistoryRecord(
            id=len(state.schema_log) + 1,
            at=change.at,
            operator=change.operator,
            action=change.action,
            doc=change.doc,
            from_version=change.from_version,
            to_version=change.to_version,
            change_class=change.change_class,
            note=change.note,
        )
        state.schema_log.append((sid, record))

        def undo() -> None:
            state.schema_log.pop()
            state.schema_states[sid] = (generation, active)
            if generation == 0:
                state.schema_states.pop(sid, None)
            for version in added:
                served.pop(version, None)
            for doc, before in before_pins.items():
                if before is None:
                    pins.pop(doc, None)
                else:
                    pins[doc] = before

        self._undo.append(undo)
        return generation + 1

    @live
    def schema_history(self, schema_id: str, limit: int | None = None, doc: str | None = None) -> list[HistoryRecord]:
        found = [r for sid, r in self._s.schema_log if sid == schema_id and (doc is None or r.doc == doc)]
        return found if limit is None else found[max(len(found) - limit, 0) :]

    def _matching(self, fields: Sequence[str], docs: Sequence[str] | None) -> list[DerivedRecord]:
        return sorted(
            (r for (d, f), r in self._s.derived.items() if f in fields and (docs is None or d in docs)),
            key=lambda r: (r.doc, r.field),
        )

    def _restore_derived(self, before: Mapping[tuple[str, str], DerivedRecord | None]) -> None:
        for key, record in before.items():
            if record is None:
                self._s.derived.pop(key, None)
            else:
                self._s.derived[key] = record

    def _set_lease(self, hook: str, value: tuple[str, datetime] | None) -> None:
        leases = self._s.leases
        previous = leases.get(hook)
        if value is None:
            leases.pop(hook, None)
        else:
            leases[hook] = value

        def undo() -> None:
            if previous is None:
                leases.pop(hook, None)
            else:
                leases[hook] = previous

        self._undo.append(undo)

    def _restore_view(self, doc: str, previous: ViewRow | None) -> None:
        if previous is None:
            self._s.views.pop(doc, None)
        else:
            self._s.views[doc] = previous


class _MemoryReadTx(_MemoryTx):
    def append_request(self, request: RequestRecord, entries: Sequence[Entry]) -> None:
        raise RuntimeError("read-only transaction")

    def put_view(self, view: ViewRow) -> None:
        raise RuntimeError("read-only transaction")

    def append_effects(self, records: Sequence[EffectRecord]) -> list[int]:
        raise RuntimeError("read-only transaction")

    def prune_effects(
        self, doc: str | None, now: datetime, keep: int = 5, max_age: timedelta = timedelta(days=90)
    ) -> int:
        raise RuntimeError("read-only transaction")

    def drop_view(self, doc: str) -> None:
        raise RuntimeError("read-only transaction")

    def put_layers(self, rows: Sequence[LayerRow]) -> None:
        raise RuntimeError("read-only transaction")

    def drop_layers(self, doc: str) -> None:
        raise RuntimeError("read-only transaction")

    def apply_compaction(self, plan: CompactionPlan) -> None:
        raise RuntimeError("read-only transaction")

    def apply_purge(self, application: PurgeApplication) -> tuple[int, int]:
        raise RuntimeError("read-only transaction")

    def append_purge_log(self, record: PurgeRecord) -> None:
        raise RuntimeError("read-only transaction")

    def expire_archive(self, doc: str, through_seq: int, at: datetime) -> int:
        raise RuntimeError("read-only transaction")

    def bump_epoch(self, doc: str) -> int:
        raise RuntimeError("read-only transaction")

    def put_derived(self, records: Sequence[DerivedRecord]) -> None:
        raise RuntimeError("read-only transaction")

    def mark_invalidated(self, fields: Sequence[str], docs: Sequence[str] | None = None) -> int:
        raise RuntimeError("read-only transaction")

    def delete_derived(self, docs: Sequence[str] | None = None, fields: Sequence[str] | None = None) -> int:
        raise RuntimeError("read-only transaction")

    def append_run(self, run: SchedulerRun) -> None:
        raise RuntimeError("read-only transaction")

    def take_lease(self, hook: str, owner: str, expires: datetime, now: datetime) -> bool:
        raise RuntimeError("read-only transaction")

    def renew_lease(self, hook: str, owner: str, expires: datetime) -> bool:
        raise RuntimeError("read-only transaction")

    def release_lease(self, hook: str, owner: str) -> None:
        raise RuntimeError("read-only transaction")

    def apply_schema_change(self, change: StateChange, expect_generation: int | None) -> int:
        raise RuntimeError("read-only transaction")


class MemoryAuditBackend:
    def __init__(self, lock: threading.RLock) -> None:
        self._lock = lock
        self._rows: list[AuditRow] = []
        self._next_id = 1
        self._pruned: dict[str, int] = {}
        self._overrides: dict[str, str] = {}
        self._depth = 0

    @contextmanager
    def atomic(self) -> Iterator[None]:
        """Everything done inside is kept, or none of it (joins an enclosing atomic block)."""
        with self._lock:
            if self._depth:
                yield
                return
            saved = (list(self._rows), self._next_id, dict(self._pruned), dict(self._overrides))
            self._depth += 1
            try:
                yield
            except BaseException:
                self._rows, self._next_id, self._pruned, self._overrides = saved
                raise
            finally:
                self._depth -= 1

    def insert(self, row: AuditRow) -> None:
        with self._lock:
            self._rows.append(AuditRow(**{**row.__dict__, "id": self._next_id}))
            self._next_id += 1

    def count(self, doc: str) -> int:
        with self._lock:
            return sum(1 for r in self._rows if r.doc == doc)

    def rows(self, doc: str, since: datetime | None = None, limit: int | None = None) -> list[AuditRow]:
        with self._lock:
            found = sorted(
                (r for r in self._rows if r.doc == doc and (since is None or r.recorded_at >= since)),
                key=lambda r: (r.recorded_at, r.id or 0),
            )
        return found if limit is None or limit >= len(found) else found[len(found) - limit :]

    def docs(self) -> list[str]:
        with self._lock:
            return sorted({r.doc for r in self._rows})

    def delete_oldest(self, doc: str, keep: int) -> int:
        with self._lock:
            rows = self.rows(doc)
            doomed = {r.id for r in rows[: max(len(rows) - keep, 0)]}
            self._rows = [r for r in self._rows if r.id not in doomed]
            return len(doomed)

    def delete_older_than(self, doc: str, cutoff: datetime) -> int:
        with self._lock:
            doomed = {r.id for r in self._rows if r.doc == doc and r.recorded_at < cutoff}
            self._rows = [r for r in self._rows if r.id not in doomed]
            return len(doomed)

    def pruned(self, doc: str) -> int:
        with self._lock:
            return self._pruned.get(doc, 0)

    def add_pruned(self, doc: str, count: int) -> None:
        with self._lock:
            self._pruned[doc] = self._pruned.get(doc, 0) + count

    def update_attempted(self, row_id: int, attempted: str) -> None:
        with self._lock:
            self._rows = [replace(r, attempted=attempted) if r.id == row_id else r for r in self._rows]

    def get_override(self, doc: str) -> str | None:
        with self._lock:
            return self._overrides.get(doc)

    def set_override(self, doc: str, setting: str | None) -> None:
        with self._lock:
            if setting is None:
                self._overrides.pop(doc, None)
            else:
                self._overrides[doc] = setting


class MemoryStorage:
    def __init__(self) -> None:
        self._state = _State()
        self._lock = threading.RLock()
        self._audit = MemoryAuditBackend(self._lock)

    def audit_backend(self) -> MemoryAuditBackend:
        return self._audit

    @contextmanager
    def read(self) -> Iterator[_MemoryReadTx]:
        with self._lock:
            tx = _MemoryReadTx(self._state, [])  # no copy: a reader never changes the state
            try:
                yield tx
            finally:
                tx.finish()

    @contextmanager
    def transaction(self) -> Iterator[_MemoryTx]:
        with self._lock:
            undo: list[Callable[[], None]] = []
            tx = _MemoryTx(self._state, undo)
            try:
                yield tx
            except BaseException:
                for revert in reversed(undo):
                    revert()
                raise
            finally:
                tx.finish()

    def close(self) -> None:
        return None
