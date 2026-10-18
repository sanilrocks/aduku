"""In-memory storage with the same transactional semantics as SQLite.

A transaction changes the live state in place and keeps an undo log; if the block raises, the log is replayed backwards.
A write therefore costs what it changes, not the size of the ledger, and a read costs nothing extra.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime

from aduku.model import Entry
from aduku.storage.base import AuditRow, ConstraintError, RequestRecord, ViewRow, live


@dataclass
class _State:
    entries: dict[str, list[Entry]] = field(default_factory=dict)
    by_request: dict[str, list[Entry]] = field(default_factory=dict)
    requests: dict[str, RequestRecord] = field(default_factory=dict)
    request_keys: dict[tuple[str, str, str], str] = field(default_factory=dict)
    idem_keys: set[tuple[str, str]] = field(default_factory=set)
    views: dict[str, ViewRow] = field(default_factory=dict)


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
    def entries(self, doc: str, since_seq: int = 0, limit: int | None = None) -> list[Entry]:
        rows = self._s.entries.get(doc, [])
        # seqs are consecutive from 1 (append_request enforces it), so the entries after `since_seq` start at that index
        return rows[max(since_seq, 0) :] if limit is None else rows[max(since_seq, 0) : max(since_seq, 0) + limit]

    @live
    def entries_of_request(self, request_id: str) -> list[Entry]:
        return list(self._s.by_request.get(request_id, []))

    @live
    def tiers_in_use(self, doc: str | None = None) -> set[str]:
        rows = self._s.entries.get(doc, []) if doc is not None else [e for r in self._s.entries.values() for e in r]
        return {entry.tier for entry in rows}

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
        if request.request_key is not None:
            state.request_keys[key] = request.request_id

        def undo() -> None:
            del rows[before:]
            if not rows:
                state.entries.pop(request.doc, None)
            del state.by_request[request.request_id]
            state.idem_keys -= new_idem
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

    def drop_view(self, doc: str) -> None:
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
