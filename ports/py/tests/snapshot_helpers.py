"""Helpers for the snapshot, time-travel, compaction and purge tests (feature 005)."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

from aduku.model import Entry
from aduku.service import Aduku
from aduku.storage.base import RequestRecord, Storage
from tests.service_helpers import make_service

__all__ = [
    "CountingStorage",
    "FaultyStorage",
    "bulk_history",
    "restart",
    "service_on",
]

EPOCH = datetime(2026, 1, 1, tzinfo=UTC)


def service_on(storage: Storage, *, layers: bool = True, schema: str | None = None, **kwargs: Any) -> Aduku:
    """A service on `storage` with the standard test schema; `layers=False` runs the same service without saved layers."""
    db = make_service(storage, schema=schema, **kwargs)
    db.layers = layers
    return db


def restart(db: Aduku) -> Aduku:
    """A new service on the same storage and schemas: no state or view kept in memory, as after a process restart."""
    fresh = Aduku(db.storage, db.schemas, clock=db.clock, ids=db.ids, behaviors=db.behaviors, layers=db.layers)
    return fresh


def bulk_history(
    storage: Storage,
    doc: str,
    n: int,
    tiers: Sequence[str] = ("backfill", "user", "admin"),
    *,
    per_request: int = 10,
    start_seq: int = 0,
    schema_version: int = 1,
) -> int:
    """Write `n` entries straight through the storage interface (fast: no admission, no derivation).

    Entries cycle over the tiers and over /name, /age and /tags; requests hold `per_request` entries. Returns the last seq.
    """
    seq = start_seq
    with storage.transaction() as tx:
        seq = max(seq, tx.last_seq(doc))
        made = 0
        request_no = 0
        while made < n:
            count = min(per_request, n - made)
            request_id = f"bulk-{doc}-{seq + 1}"
            entries: list[Entry] = []
            for offset in range(count):
                i = seq + 1 + offset
                tier = tiers[i % len(tiers)]
                kind = i % 3
                if kind == 0:
                    path, value = "/name", f"name-{i}"
                elif kind == 1:
                    path, value = "/age", i
                else:
                    path, value = "/tags", (f"t{i % 17}",)
                entries.append(
                    Entry(
                        id=f"bulk-{doc}-{i}",
                        doc=doc,
                        seq=i,
                        request_id=request_id,
                        tier=tier,
                        path=path,
                        op="set",
                        value=value,
                        contributor="bulk",
                        tx_time=EPOCH + timedelta(seconds=i),
                        schema_version=schema_version,
                    )
                )
            tx.append_request(RequestRecord(request_id, doc, "bulk"), entries)
            seq += count
            made += count
            request_no += 1
    return seq


class CountingStorage:
    """Wraps a storage and counts the ledger entries handed out by `entries()` (reads and writes alike)."""

    def __init__(self, inner: Storage) -> None:
        self.inner = inner
        self.entries_read = 0

    def reset(self) -> None:
        self.entries_read = 0

    def __getattr__(self, name: str) -> Any:
        return getattr(self.inner, name)

    def transaction(self) -> Any:
        return _CountingContext(self, self.inner.transaction())

    def read(self) -> Any:
        return _CountingContext(self, self.inner.read())


class _CountingContext:
    def __init__(self, owner: CountingStorage, context: Any) -> None:
        self._owner = owner
        self._context = context

    def __enter__(self) -> Any:
        return _CountingTx(self._owner, self._context.__enter__())

    def __exit__(self, *args: Any) -> Any:
        return self._context.__exit__(*args)


class _CountingTx:
    def __init__(self, owner: CountingStorage, tx: Any) -> None:
        self._owner = owner
        self._tx = tx

    def __getattr__(self, name: str) -> Any:
        attribute = getattr(self._tx, name)
        if name != "entries":
            return attribute

        def counted(*args: Any, **kwargs: Any) -> Any:
            rows = attribute(*args, **kwargs)
            self._owner.entries_read += len(rows)
            return rows

        return counted


class FaultyStorage:
    """Wraps a storage so that the `nth` call of a transaction method named in `methods` raises, to test atomicity."""

    class Injected(Exception):
        pass

    def __init__(self, inner: Storage, methods: Sequence[str], nth: int = 1) -> None:
        self.inner = inner
        self.methods = tuple(methods)
        self.nth = nth
        self.calls = 0
        self.armed = True

    def __getattr__(self, name: str) -> Any:
        return getattr(self.inner, name)

    def transaction(self) -> Any:
        return _FaultyContext(self, self.inner.transaction())

    def read(self) -> Any:
        return self.inner.read()


class _FaultyContext:
    def __init__(self, owner: FaultyStorage, context: Any) -> None:
        self._owner = owner
        self._context = context

    def __enter__(self) -> Any:
        return _FaultyTx(self._owner, self._context.__enter__())

    def __exit__(self, *args: Any) -> Any:
        return self._context.__exit__(*args)


class _FaultyTx:
    def __init__(self, owner: FaultyStorage, tx: Any) -> None:
        self._owner = owner
        self._tx = tx

    def __getattr__(self, name: str) -> Any:
        attribute = getattr(self._tx, name)
        owner = self._owner
        if name not in owner.methods or not callable(attribute):
            return attribute

        def maybe_fail(*args: Any, **kwargs: Any) -> Any:
            if owner.armed:
                owner.calls += 1
                if owner.calls == owner.nth:
                    raise FaultyStorage.Injected(name)
            return attribute(*args, **kwargs)

        return maybe_fail
