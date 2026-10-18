"""Storage, clock and id-source interfaces. All IO and non-determinism enter through these (Principle III)."""

from __future__ import annotations

import functools
from abc import ABC, abstractmethod
from collections.abc import Callable, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol, cast

from ulid import ULID

from aduku.model import Entry

__all__ = [
    "AuditBackend",
    "AuditRow",
    "Clock",
    "ConstraintError",
    "FakeClock",
    "IdSource",
    "RequestRecord",
    "SequentialIds",
    "Storage",
    "SystemClock",
    "Tx",
    "UlidIdSource",
    "ViewRow",
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

    def drop_view(self, doc: str) -> None: ...


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
