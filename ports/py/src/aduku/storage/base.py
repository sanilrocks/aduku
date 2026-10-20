"""Storage, clock and id-source interfaces. All IO and non-determinism enter through these (Principle III)."""

from __future__ import annotations

import functools
from abc import ABC, abstractmethod
from collections.abc import Callable, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol, cast

from ulid import ULID

from aduku.model import (
    CompactionPlan,
    CompactionRecord,
    DerivedRecord,
    EffectRecord,
    Entry,
    HistoryRecord,
    PurgeApplication,
    PurgeRecord,
    SchedulerRun,
    SourceRow,
    StateChange,
)

__all__ = [
    "AuditBackend",
    "AuditRow",
    "Clock",
    "ConstraintError",
    "EffectRecord",
    "FakeClock",
    "IdSource",
    "LayerRow",
    "RequestRecord",
    "SequentialIds",
    "Storage",
    "SystemClock",
    "Tx",
    "UlidIdSource",
    "ViewRow",
    "effects_to_prune",
    "live",
]


def live[F: Callable[..., Any]](method: F) -> F:
    """Guard for transaction methods: a handle used after its transaction ended raises RuntimeError.

    Without it a leftover handle could write outside any transaction.
    """

    @functools.wraps(method)
    def wrapper(self: Any, *args: Any, **kwargs: Any) -> Any:
        if not self._open:
            raise RuntimeError("transaction is finished")
        return method(self, *args, **kwargs)

    return cast(F, wrapper)


class ConstraintError(Exception):
    """A ledger constraint was violated (non-consecutive seq, reused idempotency key or request key)."""


def effects_to_prune(records: Sequence[EffectRecord], now: datetime, keep: int, max_age: timedelta) -> list[int]:
    """The `effect_seq` of each record to remove: superseded ones beyond the newest `keep` of their key, and superseded
    ones older than `max_age`. The latest record of a key (the one in force) is never listed."""
    by_key: dict[tuple[str, str, str, str], list[EffectRecord]] = {}
    for record in sorted(records, key=lambda r: r.effect_seq or 0):
        by_key.setdefault((record.doc, record.field, record.fingerprint, record.hook), []).append(record)
    cutoff = now - max_age
    doomed: list[int] = []
    for group in by_key.values():
        superseded = group[:-1]
        for index, record in enumerate(superseded):
            too_many = index < len(superseded) - keep
            if (too_many or record.recorded_at < cutoff) and record.effect_seq is not None:
                doomed.append(record.effect_seq)
    return doomed


@dataclass(frozen=True)
class RequestRecord:
    request_id: str
    doc: str
    contributor: str
    request_key: str | None = None
    request_hash: str | None = None


@dataclass(frozen=True)
class ViewRow:
    """A cached derived view; the engine decides the contents of the text fields."""

    doc: str
    watermark_seq: int
    schema_version: int
    rules_fingerprint: str
    document: str
    model_errors: str
    provenance: str
    effects_seq: int = 0  # the greatest effect-log sequence number the view was derived with (feature 002)
    epoch: int = 0  # feature 005: the document's epoch when it was derived; another epoch means it is not used


@dataclass(frozen=True)
class LayerRow:
    """One saved per-tier layer (feature 005, research R-1): the canonical text of what the tier's entries fold to, up to
    and including `tier_seq` (the highest seq of that tier it covers), made under one schema version and epoch."""

    doc: str
    tier: str
    tier_seq: int
    schema_version: int
    rules_fingerprint: str
    epoch: int
    state: str


@dataclass(frozen=True)
class AuditRow:
    """One stored rejection record; attempted entries and reasons are canonical Ion text (see audit.py)."""

    doc: str
    contributor: str
    tiers: tuple[str, ...]
    lenient: bool
    attempted: str
    reasons: str
    schema_version: int | None
    recorded_at: datetime
    id: int | None = None


class AuditBackend(Protocol):
    """Storage for the rejection audit log. Separate from the ledger: nothing in derivation ever reads it."""

    def atomic(self) -> AbstractContextManager[None]:
        """Everything done inside the block is kept, or none of it; joins an enclosing atomic block."""

    def insert(self, row: AuditRow) -> None: ...

    def rows(self, doc: str, since: datetime | None = None, limit: int | None = None) -> list[AuditRow]:
        """Oldest first. `since` keeps rows recorded at or after it; `limit` keeps the newest N of those."""

    def count(self, doc: str) -> int: ...

    def docs(self) -> list[str]: ...

    def delete_oldest(self, doc: str, keep: int) -> int:
        """Delete all but the `keep` newest rows of `doc`; returns how many were deleted."""

    def delete_older_than(self, doc: str, cutoff: datetime) -> int: ...

    def pruned(self, doc: str) -> int: ...

    def add_pruned(self, doc: str, count: int) -> None: ...

    def get_override(self, doc: str) -> str | None: ...

    def set_override(self, doc: str, setting: str | None) -> None: ...

    def update_attempted(self, row_id: int, attempted: str) -> None:
        """Replace the attempted entries of one record (a purge scrubs the values out of them)."""


class Tx(Protocol):
    """One storage transaction. Reads see this transaction's own writes."""

    def last_seq(self, doc: str) -> int: ...

    def entries(
        self, doc: str, since_seq: int = 0, limit: int | None = None, *, until_seq: int | None = None
    ) -> list[Entry]:
        """Entries after `since_seq` (and up to `until_seq` when given) in seq order; at most `limit` of them when given."""

    def entries_of_request(self, request_id: str) -> list[Entry]: ...

    def tiers_in_use(self, doc: str | None = None) -> set[str]:
        """The tiers any entry uses: of one document, or of the whole ledger when `doc` is None."""

    def doc_ids(self) -> list[str]:
        """Every document that has an entry, in name order."""

    def compacted_docs(self) -> list[str]:
        """The documents that were compacted at least once, in name order (the other documents have nothing to check)."""

    def find_request(self, doc: str, contributor: str, request_key: str) -> RequestRecord | None: ...

    def idem_key_seen(self, doc: str, idem_key: str) -> bool: ...

    def append_request(self, request: RequestRecord, entries: Sequence[Entry]) -> None:
        """Append a whole request. Entries must have consecutive seq starting at last_seq + 1 or ConstraintError is raised."""

    def get_view(self, doc: str) -> ViewRow | None: ...

    def put_view(self, view: ViewRow) -> None: ...

    def append_effects(self, records: Sequence[EffectRecord]) -> list[int]:
        """Append outcomes of impure computes to the effect log; returns the sequence numbers assigned, in order."""

    def latest_effects(self, doc: str) -> list[EffectRecord]:
        """The outcome in force (the greatest `effect_seq`) for each (field, fingerprint, hook) of a document."""

    def effects_seq(self, doc: str) -> int:
        """The greatest `effect_seq` recorded for a document, 0 if none."""

    def list_effects(self, doc: str, field: str | None = None, limit: int | None = None) -> list[EffectRecord]:
        """Oldest first; `limit` keeps the newest N."""

    def prune_effects(
        self, doc: str | None, now: datetime, keep: int = 5, max_age: timedelta = timedelta(days=90)
    ) -> int:
        """Remove superseded outcomes beyond the newest `keep` per key and those older than `max_age`; returns how many.

        The outcome in force is never removed, so no derivation changes."""

    def drop_view(self, doc: str) -> None: ...

    # -- saved layers and the document epoch (feature 005) -----------------------------------------------------------

    def get_layers(self, doc: str) -> list[LayerRow]:
        """The saved layers of a document, in tier-name order."""

    def put_layers(self, rows: Sequence[LayerRow]) -> None:
        """Save layers. A row replaces the saved one of its (document, tier) only if it is of a newer epoch, or of the same
        epoch and covers at least as much (`tier_seq`): a slow writer cannot regress a newer layer."""

    def drop_layers(self, doc: str) -> None: ...

    def tier_last_seq(self, doc: str, tier: str, through: int) -> int:
        """The highest seq of an entry of `tier` at or below `through`, 0 if there is none."""

    def epoch(self, doc: str) -> int:
        """The document's epoch: 0 until a compaction, an expiry or a purge changes the stored ledger (then it grows)."""

    def bump_epoch(self, doc: str) -> int: ...

    # -- compaction, archive and expiry (feature 005) ------------------------------------------------------------------

    def apply_compaction(self, plan: CompactionPlan) -> None:
        """One atomic step: copy the replaced entries to the archive, remove them, insert the synthetic entries, record the
        compaction, its sources, the request index and the retired idempotency keys, and bump the document's epoch.

        Raises ConstraintError (changing nothing of it) if a replaced entry is not in the ledger as the plan says. The
        effect log, the derived store and the rejection audit log are never touched."""

    def compactions(self, doc: str) -> list[CompactionRecord]:
        """The compactions of a document, oldest first."""

    def sources_of(self, compaction_id: str) -> list[SourceRow]:
        """The entries a compaction replaced, in seq order; kept after the archive is expired."""

    def archive_of(self, compaction_id: str) -> list[Entry]:
        """The archived originals of one compaction, in seq order; empty after expiry."""

    def archived_entries(self, doc: str, through_seq: int | None = None) -> list[Entry]:
        """Every archived entry of a document (all compactions), in seq order, up to `through_seq` when given."""

    def request_entries(self, request_id: str) -> list[tuple[str, int]]:
        """(entry id, seq) of the entries of a request: the live ones, else those a compaction replaced."""

    def request_effects(self, request_id: str) -> tuple[str, str] | None:
        """For a request a compaction replaced: the model errors it added and resolved (canonical text), else None."""

    def expire_archive(self, doc: str, through_seq: int, at: datetime) -> int:
        """Delete the archive of every compaction of the document whose watermark is at or below `through_seq`, record the
        expiry, and return how many archived entries went. Sources, the request index and retired keys stay."""

    # -- purge (feature 005) -----------------------------------------------------------------------------------------

    def apply_purge(self, application: PurgeApplication) -> tuple[int, int]:
        """One atomic step: replace the entries of the ledger and of the archive by their erased versions, clear the stored
        hash of the requests, drop the document's saved layers and view and bump its epoch, and delete the effect-log
        records and derived-store rows of the fields. Returns (effect records deleted, derived rows deleted).

        Raises ConstraintError if an entry to replace is not there."""

    def append_purge_log(self, record: PurgeRecord) -> None: ...

    def purge_log(self, doc: str) -> list[PurgeRecord]:
        """The purge log of a document, oldest first."""

    def seq_at_tx_time(self, doc: str, moment: datetime) -> int:
        """The highest seq whose entry (live or archived) was recorded at or before `moment`, 0 if none. `tx_time` need
        not grow with seq, so this is a maximum over all entries, not a search."""

    # -- the derived store of deferred fields (feature 003): the latest result per document and field -----------------

    def get_derived(self, doc: str) -> list[DerivedRecord]:
        """The records of a document, in field order."""

    def put_derived(self, records: Sequence[DerivedRecord]) -> None:
        """Insert or replace the record of each (document, field)."""

    def list_derived(self, fields: Sequence[str], docs: Sequence[str] | None = None) -> list[DerivedRecord]:
        """The records of the given fields, of every document or only of `docs`, in (document, field) order."""

    def mark_invalidated(self, fields: Sequence[str], docs: Sequence[str] | None = None) -> int:
        """Set `invalidated` on the matching records (repeatable); returns how many records match."""

    def delete_derived(self, docs: Sequence[str] | None = None, fields: Sequence[str] | None = None) -> int:
        """Remove the matching records (all when neither is given); returns how many. The core never reads them."""

    def append_run(self, run: SchedulerRun) -> None: ...

    def list_runs(self, hook: str, limit: int | None = None) -> list[SchedulerRun]:
        """The runs of a source, oldest first; `limit` keeps the newest N."""

    def take_lease(self, hook: str, owner: str, expires: datetime, now: datetime) -> bool:
        """Take the run lease of a source if it is free, expired (at `now`) or already `owner`'s."""

    def renew_lease(self, hook: str, owner: str, expires: datetime) -> bool:
        """Extend the lease if `owner` holds it."""

    def release_lease(self, hook: str, owner: str) -> None:
        """Free the lease if `owner` holds it."""

    def schema_generation(self, schema_id: str) -> int:
        """The generation of the shared schema state (0 when nothing was ever written); one single-row read."""

    def schema_state(self, schema_id: str) -> tuple[int, int | None, dict[str, int]]:
        """(generation, active version or None, document -> pinned version)."""

    def served_versions(self, schema_id: str) -> dict[int, tuple[str, str]]:
        """version -> (fingerprint, parts) of every version served so far; written once and never changed."""

    def apply_schema_change(self, change: StateChange, expect_generation: int | None) -> int:
        """Write state, fingerprints and the history record together and bump the generation by one.

        Raises ConstraintError if `expect_generation` is given and is not the stored generation, or if a served
        version already has another fingerprint. Returns the new generation.
        """

    def schema_history(self, schema_id: str, limit: int | None = None, doc: str | None = None) -> list[HistoryRecord]:
        """The history of the state, oldest first; `limit` keeps the newest N, `doc` only that document's records."""


class Storage(Protocol):
    def transaction(self) -> AbstractContextManager[Tx]:
        """A write transaction: commits on a clean exit, rolls back on any exception. Single writer at a time."""

    def read(self) -> AbstractContextManager[Tx]:
        """A read-only transaction: a consistent snapshot that never takes the write lock and refuses writes."""

    def audit_backend(self) -> AuditBackend: ...

    def close(self) -> None: ...


class Clock(ABC):
    @abstractmethod
    def now(self) -> datetime: ...

    @staticmethod
    def system() -> Clock:
        return SystemClock()


class SystemClock(Clock):
    def now(self) -> datetime:
        return datetime.now(UTC)


class FakeClock(Clock):
    """A settable clock for tests; `step` advances it on every read."""

    def __init__(self, start: datetime | None = None, step_seconds: float = 0.0) -> None:
        from datetime import timedelta

        self._now = start or datetime(2026, 10, 18, 12, 0, 0, tzinfo=UTC)
        self._step = timedelta(seconds=step_seconds)

    def now(self) -> datetime:
        current = self._now
        self._now = current + self._step
        return current

    def set(self, moment: datetime) -> None:
        self._now = moment


class IdSource(ABC):
    @abstractmethod
    def new_id(self) -> str: ...

    @staticmethod
    def system() -> IdSource:
        return UlidIdSource()


class UlidIdSource(IdSource):
    def new_id(self) -> str:
        return str(ULID())


class SequentialIds(IdSource):
    """Deterministic ids for tests: id-000001, id-000002, ..."""

    def __init__(self, prefix: str = "id") -> None:
        self._prefix = prefix
        self._count = 0

    def new_id(self) -> str:
        self._count += 1
        return f"{self._prefix}-{self._count:06d}"
