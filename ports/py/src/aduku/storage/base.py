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

from aduku.model import DerivedRecord, EffectRecord, Entry, SchedulerRun

__all__ = [
    "AuditBackend",
    "AuditRow",
    "Clock",
    "ConstraintError",
    "EffectRecord",
    "FakeClock",
    "IdSource",
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


class Tx(Protocol):
    """One storage transaction. Reads see this transaction's own writes."""

    def last_seq(self, doc: str) -> int: ...

    def entries(self, doc: str, since_seq: int = 0, limit: int | None = None) -> list[Entry]:
        """Entries after `since_seq` in seq order; at most `limit` of them when given."""

    def entries_of_request(self, request_id: str) -> list[Entry]: ...

    def tiers_in_use(self, doc: str | None = None) -> set[str]:
        """The tiers any entry uses: of one document, or of the whole ledger when `doc` is None."""

    def doc_ids(self) -> list[str]:
        """Every document that has an entry, in name order."""

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
