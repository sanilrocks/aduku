"""SQLite storage (WAL). The ledger table is append-only, enforced by triggers (Principle I)."""

from __future__ import annotations

import sqlite3
import threading
import weakref
from collections.abc import Iterator, Sequence
from contextlib import AbstractContextManager, contextmanager
from datetime import UTC, datetime, timedelta

from aduku.errors import StorageError
from aduku.ion_codec import parse, to_canonical
from aduku.model import Entry
from aduku.storage.base import (
    AuditRow,
    ConstraintError,
    EffectRecord,
    RequestRecord,
    ViewRow,
    effects_to_prune,
    live,
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS entries (
    id TEXT PRIMARY KEY,
    doc TEXT NOT NULL,
    seq INTEGER NOT NULL,
    request_id TEXT NOT NULL,
    tier TEXT NOT NULL,
    path TEXT NOT NULL,
    op TEXT NOT NULL,
    value TEXT,
    contributor TEXT NOT NULL,
    tx_time TEXT NOT NULL,
    idem_key TEXT,
    request_key TEXT,
    request_hash TEXT,
    schema_version INTEGER NOT NULL,
    lenient INTEGER NOT NULL DEFAULT 0,
    meta TEXT,
    UNIQUE (doc, seq)
);
CREATE UNIQUE INDEX IF NOT EXISTS entries_idem_key ON entries (doc, idem_key) WHERE idem_key IS NOT NULL;
CREATE INDEX IF NOT EXISTS entries_request ON entries (request_id);
CREATE INDEX IF NOT EXISTS entries_tier ON entries (tier);
CREATE TRIGGER IF NOT EXISTS entries_no_update BEFORE UPDATE ON entries
BEGIN SELECT RAISE(ABORT, 'entries are append-only'); END;
CREATE TRIGGER IF NOT EXISTS entries_no_delete BEFORE DELETE ON entries
BEGIN SELECT RAISE(ABORT, 'entries are append-only'); END;

CREATE TABLE IF NOT EXISTS doc_seq (doc TEXT PRIMARY KEY, last_seq INTEGER NOT NULL);

CREATE TABLE IF NOT EXISTS requests (
    request_id TEXT PRIMARY KEY,
    doc TEXT NOT NULL,
    contributor TEXT NOT NULL,
    request_key TEXT,
    request_hash TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS requests_key ON requests (doc, contributor, request_key) WHERE request_key IS NOT NULL;

CREATE TABLE IF NOT EXISTS views (
    doc TEXT PRIMARY KEY,
    watermark_seq INTEGER NOT NULL,
    schema_version INTEGER NOT NULL,
    rules_fingerprint TEXT NOT NULL,
    document TEXT NOT NULL,
    model_errors TEXT NOT NULL,
    provenance TEXT NOT NULL,
    effects_seq INTEGER NOT NULL DEFAULT 0
);

-- Outcomes of impure computes (feature 002). Separate from the ledger; append-only except through prune_effects.
CREATE TABLE IF NOT EXISTS effects (
    effect_seq INTEGER PRIMARY KEY AUTOINCREMENT,
    doc TEXT NOT NULL,
    field TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    hook TEXT NOT NULL,
    outcome TEXT NOT NULL,
    value TEXT,
    error TEXT,
    attempts INTEGER NOT NULL,
    recorded_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS effects_key ON effects (doc, field, fingerprint, hook);
CREATE TABLE IF NOT EXISTS effects_pruning (id INTEGER PRIMARY KEY CHECK (id = 1), active INTEGER NOT NULL);
INSERT OR IGNORE INTO effects_pruning (id, active) VALUES (1, 0);
CREATE TRIGGER IF NOT EXISTS effects_no_update BEFORE UPDATE ON effects
BEGIN SELECT RAISE(ABORT, 'effects are append-only'); END;
CREATE TRIGGER IF NOT EXISTS effects_no_delete BEFORE DELETE ON effects
WHEN (SELECT active FROM effects_pruning WHERE id = 1) = 0
BEGIN SELECT RAISE(ABORT, 'effects are append-only'); END;

-- Audit-only data: never read by derivation (SPEC 4.5).
CREATE TABLE IF NOT EXISTS rejection_audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    doc TEXT NOT NULL,
    contributor TEXT NOT NULL,
    tiers TEXT NOT NULL,
    lenient INTEGER NOT NULL,
    attempted TEXT NOT NULL,
    reasons TEXT NOT NULL,
    schema_version INTEGER,
    recorded_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS audit_meta (doc TEXT PRIMARY KEY, pruned_count INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS audit_override (doc TEXT PRIMARY KEY, setting TEXT NOT NULL);
"""


TIME_FORMAT = "%Y-%m-%dT%H:%M:%S.%fZ"


def _stamp(moment: datetime) -> str:
    """Fixed-width UTC text, so that text order is time order."""
    return moment.astimezone(UTC).strftime(TIME_FORMAT)


def _unstamp(text: str) -> datetime:
    return datetime.strptime(text, TIME_FORMAT).replace(tzinfo=UTC)


def _effect(row: sqlite3.Row) -> EffectRecord:
    return EffectRecord(
        doc=row["doc"],
        field=row["field"],
        fingerprint=row["fingerprint"],
        hook=row["hook"],
        outcome=row["outcome"],
        value=row["value"],
        error=row["error"],
        attempts=row["attempts"],
        recorded_at=_unstamp(row["recorded_at"]),
        effect_seq=row["effect_seq"],
    )


def _entry(row: sqlite3.Row) -> Entry:
    return Entry(
        id=row["id"],
        doc=row["doc"],
        seq=row["seq"],
        request_id=row["request_id"],
        tier=row["tier"],
        path=row["path"],
        op=row["op"],
        value=parse(row["value"]) if row["value"] is not None else None,
        contributor=row["contributor"],
        tx_time=_unstamp(row["tx_time"]),
        schema_version=row["schema_version"],
        idem_key=row["idem_key"],
        request_key=row["request_key"],
        request_hash=row["request_hash"],
        lenient=bool(row["lenient"]),
        meta=parse(row["meta"]) if row["meta"] is not None else None,
    )


class _SqliteTx:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self._c = connection
        self._open = True

    def finish(self) -> None:
        self._open = False

    @live
    def last_seq(self, doc: str) -> int:
        row = self._c.execute("SELECT last_seq FROM doc_seq WHERE doc = ?", (doc,)).fetchone()
        return int(row[0]) if row else 0

    @live
    def tiers_in_use(self, doc: str | None = None) -> set[str]:
        if doc is None:
            rows = self._c.execute("SELECT DISTINCT tier FROM entries")
        else:
            rows = self._c.execute("SELECT DISTINCT tier FROM entries WHERE doc = ?", (doc,))
        return {str(row[0]) for row in rows}

    @live
    def doc_ids(self) -> list[str]:
        return [str(row[0]) for row in self._c.execute("SELECT doc FROM doc_seq ORDER BY doc")]

    @live
    def entries(self, doc: str, since_seq: int = 0, limit: int | None = None) -> list[Entry]:
        rows = self._c.execute(
            "SELECT * FROM entries WHERE doc = ? AND seq > ? ORDER BY seq LIMIT ?",
            (doc, since_seq, -1 if limit is None else limit),
        )
        return [_entry(row) for row in rows]

    @live
    def entries_of_request(self, request_id: str) -> list[Entry]:
        rows = self._c.execute("SELECT * FROM entries WHERE request_id = ? ORDER BY doc, seq", (request_id,))
        return [_entry(row) for row in rows]

    @live
    def find_request(self, doc: str, contributor: str, request_key: str) -> RequestRecord | None:
        row = self._c.execute(
            "SELECT * FROM requests WHERE doc = ? AND contributor = ? AND request_key = ?",
            (doc, contributor, request_key),
        ).fetchone()
        if row is None:
            return None
        return RequestRecord(row["request_id"], row["doc"], row["contributor"], row["request_key"], row["request_hash"])

    @live
    def idem_key_seen(self, doc: str, idem_key: str) -> bool:
        return (
            self._c.execute("SELECT 1 FROM entries WHERE doc = ? AND idem_key = ?", (doc, idem_key)).fetchone()
            is not None
        )

    @live
    def append_request(self, request: RequestRecord, entries: Sequence[Entry]) -> None:
        expected = self.last_seq(request.doc) + 1
        for entry in entries:
            if entry.doc != request.doc:
                raise ConstraintError("all entries of a request must belong to its document")
            if entry.seq != expected:
                raise ConstraintError(f"expected seq {expected}, got {entry.seq}")
            expected += 1
        self._c.execute("SAVEPOINT append_request")
        try:
            self._c.execute(
                "INSERT INTO requests (request_id, doc, contributor, request_key, request_hash) VALUES (?, ?, ?, ?, ?)",
                (request.request_id, request.doc, request.contributor, request.request_key, request.request_hash),
            )
            for entry in entries:
                self._c.execute(
                    "INSERT INTO entries (id, doc, seq, request_id, tier, path, op, value, contributor, tx_time, idem_key,"
                    " request_key, request_hash, schema_version, lenient, meta) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        entry.id,
                        entry.doc,
                        entry.seq,
                        entry.request_id,
                        entry.tier,
                        entry.path,
                        entry.op,
                        to_canonical(entry.value),
                        entry.contributor,
                        _stamp(entry.tx_time),
                        entry.idem_key,
                        entry.request_key,
                        entry.request_hash,
                        entry.schema_version,
                        int(entry.lenient),
                        to_canonical(entry.meta) if entry.meta is not None else None,
                    ),
                )
            if entries:
                self._c.execute(
                    "INSERT INTO doc_seq (doc, last_seq) VALUES (?, ?) ON CONFLICT (doc) DO UPDATE SET last_seq = excluded.last_seq",
                    (request.doc, expected - 1),
                )
        except sqlite3.IntegrityError as error:
            self._c.execute("ROLLBACK TO append_request")
            self._c.execute("RELEASE append_request")
            raise ConstraintError(str(error)) from error
        except sqlite3.Error:
            self._c.execute("ROLLBACK TO append_request")
            self._c.execute("RELEASE append_request")
            raise
        self._c.execute("RELEASE append_request")

    @live
    def get_view(self, doc: str) -> ViewRow | None:
        row = self._c.execute("SELECT * FROM views WHERE doc = ?", (doc,)).fetchone()
        if row is None:
            return None
        return ViewRow(
            row["doc"],
            row["watermark_seq"],
            row["schema_version"],
            row["rules_fingerprint"],
            row["document"],
            row["model_errors"],
            row["provenance"],
            row["effects_seq"],
        )

    @live
    def put_view(self, view: ViewRow) -> None:
        self._c.execute(
            "INSERT INTO views (doc, watermark_seq, schema_version, rules_fingerprint, document, model_errors, provenance,"
            " effects_seq) VALUES (?,?,?,?,?,?,?,?) ON CONFLICT (doc) DO UPDATE SET watermark_seq = excluded.watermark_seq,"
            " schema_version = excluded.schema_version, rules_fingerprint = excluded.rules_fingerprint,"
            " document = excluded.document, model_errors = excluded.model_errors, provenance = excluded.provenance,"
            " effects_seq = excluded.effects_seq",
            (
                view.doc,
                view.watermark_seq,
                view.schema_version,
                view.rules_fingerprint,
                view.document,
                view.model_errors,
                view.provenance,
                view.effects_seq,
            ),
        )

    @live
    def drop_view(self, doc: str) -> None:
        self._c.execute("DELETE FROM views WHERE doc = ?", (doc,))

    @live
    def append_effects(self, records: Sequence[EffectRecord]) -> list[int]:
        assigned: list[int] = []
        for record in records:
            cursor = self._c.execute(
                "INSERT INTO effects (doc, field, fingerprint, hook, outcome, value, error, attempts, recorded_at)"
                " VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    record.doc,
                    record.field,
                    record.fingerprint,
                    record.hook,
                    record.outcome,
                    record.value,
                    record.error,
                    record.attempts,
                    _stamp(record.recorded_at),
                ),
            )
            assigned.append(int(cursor.lastrowid or 0))
        return assigned

    @live
    def latest_effects(self, doc: str) -> list[EffectRecord]:
        rows = self._c.execute(
            "SELECT e.* FROM effects e WHERE e.doc = ? AND e.effect_seq = (SELECT MAX(effect_seq) FROM effects x"
            " WHERE x.doc = e.doc AND x.field = e.field AND x.fingerprint = e.fingerprint AND x.hook = e.hook)"
            " ORDER BY e.effect_seq",
            (doc,),
        )
        return [_effect(row) for row in rows]

    @live
    def effects_seq(self, doc: str) -> int:
        row = self._c.execute("SELECT MAX(effect_seq) FROM effects WHERE doc = ?", (doc,)).fetchone()
        return int(row[0] or 0)

    @live
    def list_effects(self, doc: str, field: str | None = None, limit: int | None = None) -> list[EffectRecord]:
        rows = self._c.execute(
            "SELECT * FROM effects WHERE doc = ? AND (? IS NULL OR field = ?) ORDER BY effect_seq", (doc, field, field)
        ).fetchall()
        found = [_effect(row) for row in rows]
        return found if limit is None else found[max(len(found) - limit, 0) :]

    @live
    def prune_effects(
        self, doc: str | None, now: datetime, keep: int = 5, max_age: timedelta = timedelta(days=90)
    ) -> int:
        rows = self._c.execute(
            "SELECT * FROM effects WHERE (? IS NULL OR doc = ?) ORDER BY effect_seq", (doc, doc)
        ).fetchall()
        doomed = effects_to_prune([_effect(row) for row in rows], now, keep, max_age)
        if not doomed:
            return 0
        self._c.execute("UPDATE effects_pruning SET active = 1 WHERE id = 1")
        try:
            for start in range(0, len(doomed), 500):
                chunk = doomed[start : start + 500]
                self._c.execute(f"DELETE FROM effects WHERE effect_seq IN ({','.join('?' * len(chunk))})", chunk)
        finally:
            self._c.execute("UPDATE effects_pruning SET active = 0 WHERE id = 1")
        return len(doomed)


class _SqliteReadTx(_SqliteTx):
    """A read-only handle: reads like any transaction, refuses every write."""

    def append_request(self, request: RequestRecord, entries: Sequence[Entry]) -> None:
        raise RuntimeError("read-only transaction")

    def put_view(self, view: ViewRow) -> None:
        raise RuntimeError("read-only transaction")

    def drop_view(self, doc: str) -> None:
        raise RuntimeError("read-only transaction")


class SqliteAuditBackend:
    """Rejection audit rows in the same database file, separate tables, never joined to the ledger."""

    def __init__(self, storage: SqliteStorage) -> None:
        self._storage = storage

    def atomic(self) -> AbstractContextManager[None]:
        """One transaction for a group of audit statements (joins one that is already open)."""
        return self._storage.atomic()

    def _all(self, sql: str, params: tuple[object, ...] = ()) -> list[sqlite3.Row]:
        """Run a statement and read its result while still holding the lock, so other threads cannot interleave."""
        with self._storage.lock:
            try:
                return self._storage.connection.execute(sql, params).fetchall()
            except sqlite3.Error as error:
                raise StorageError(f"audit store: {error}") from error

    def _run(self, sql: str, params: tuple[object, ...] = ()) -> int:
        """Run a statement; returns the number of rows it changed."""
        with self._storage.lock:
            try:
                return self._storage.connection.execute(sql, params).rowcount
            except sqlite3.Error as error:
                raise StorageError(f"audit store: {error}") from error

    def insert(self, row: AuditRow) -> None:
        self._run(
            "INSERT INTO rejection_audit (doc, contributor, tiers, lenient, attempted, reasons, schema_version,"
            " recorded_at) VALUES (?,?,?,?,?,?,?,?)",
            (
                row.doc,
                row.contributor,
                to_canonical(tuple(row.tiers)),
                int(row.lenient),
                row.attempted,
                row.reasons,
                row.schema_version,
                _stamp(row.recorded_at),
            ),
        )

    def count(self, doc: str) -> int:
        return int(self._all("SELECT COUNT(*) FROM rejection_audit WHERE doc = ?", (doc,))[0][0])

    def rows(self, doc: str, since: datetime | None = None, limit: int | None = None) -> list[AuditRow]:
        where, params = "doc = ?", [doc]
        if since is not None:
            where += " AND recorded_at >= ?"
            params.append(_stamp(since))
        rows = self._all(
            f"SELECT * FROM (SELECT * FROM rejection_audit WHERE {where} ORDER BY recorded_at DESC, id DESC LIMIT ?)"
            " ORDER BY recorded_at, id",
            (*params, -1 if limit is None else limit),
        )
        return [
            AuditRow(
                r["doc"],
                r["contributor"],
                tuple(parse(r["tiers"])),
                bool(r["lenient"]),
                r["attempted"],
                r["reasons"],
                r["schema_version"],
                _unstamp(r["recorded_at"]),
                r["id"],
            )
            for r in rows
        ]

    def docs(self) -> list[str]:
        return [r[0] for r in self._all("SELECT DISTINCT doc FROM rejection_audit ORDER BY doc")]

    def delete_oldest(self, doc: str, keep: int) -> int:
        return self._run(
            "DELETE FROM rejection_audit WHERE doc = ? AND id NOT IN"
            " (SELECT id FROM rejection_audit WHERE doc = ? ORDER BY recorded_at DESC, id DESC LIMIT ?)",
            (doc, doc, max(keep, 0)),  # a negative LIMIT means "no limit" in SQLite
        )

    def delete_older_than(self, doc: str, cutoff: datetime) -> int:
        return self._run("DELETE FROM rejection_audit WHERE doc = ? AND recorded_at < ?", (doc, _stamp(cutoff)))

    def pruned(self, doc: str) -> int:
        rows = self._all("SELECT pruned_count FROM audit_meta WHERE doc = ?", (doc,))
        return int(rows[0][0]) if rows else 0

    def add_pruned(self, doc: str, count: int) -> None:
        self._run(
            "INSERT INTO audit_meta (doc, pruned_count) VALUES (?, ?) ON CONFLICT (doc)"
            " DO UPDATE SET pruned_count = pruned_count + excluded.pruned_count",
            (doc, count),
        )

    def get_override(self, doc: str) -> str | None:
        rows = self._all("SELECT setting FROM audit_override WHERE doc = ?", (doc,))
        return str(rows[0][0]) if rows else None

    def set_override(self, doc: str, setting: str | None) -> None:
        if setting is None:
            self._run("DELETE FROM audit_override WHERE doc = ?", (doc,))
        else:
            self._run(
                "INSERT INTO audit_override (doc, setting) VALUES (?, ?) ON CONFLICT (doc)"
                " DO UPDATE SET setting = excluded.setting",
                (doc, setting),
            )


def _release_reader(connection: sqlite3.Connection, connections: set[sqlite3.Connection], lock: threading.Lock) -> None:
    with lock:
        connections.discard(connection)
    try:
        connection.close()
    except sqlite3.Error:
        pass


class _Reader:
    """What a thread holds for its reads; collected when the thread ends."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection


class SqliteStorage:
    def __init__(self, path: str) -> None:
        self._lock = threading.RLock()  # serializes the one writing connection
        self._path = path
        self._readers = threading.local()  # every thread reads on its own connection, in WAL mode: no lock, no queue
        self._reader_connections: set[sqlite3.Connection] = set()
        self._reader_lock = threading.Lock()
        self._shared = path in ("", ":memory:")  # a private in-memory database cannot be opened a second time
        connection: sqlite3.Connection | None = None
        try:
            connection = sqlite3.connect(path, isolation_level=None, check_same_thread=False, timeout=30)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("PRAGMA foreign_keys = ON")
            connection.executescript(SCHEMA)
            columns = {row["name"] for row in connection.execute("PRAGMA table_info(views)")}
            if "effects_seq" not in columns:  # a database made before feature 002
                connection.execute("ALTER TABLE views ADD COLUMN effects_seq INTEGER NOT NULL DEFAULT 0")
        except sqlite3.Error as error:
            if connection is not None:
                connection.close()  # do not leak the handle (and its locks) of a database that cannot be used
            raise StorageError(f"cannot open {path}: {error}") from error
        self._c = connection

    def audit_backend(self) -> SqliteAuditBackend:
        return SqliteAuditBackend(self)

    @property
    def connection(self) -> sqlite3.Connection:
        """For the audit module, which shares the database file but not the ledger interface."""
        return self._c

    @contextmanager
    def atomic(self) -> Iterator[None]:
        """A unit of work that is kept whole or not at all; blocks nested inside an open one join it."""
        with self._lock:
            if self._c.in_transaction:
                yield
                return
            try:
                self._c.execute("BEGIN IMMEDIATE")
            except sqlite3.Error as error:
                raise StorageError(f"cannot begin: {error}") from error
            try:
                yield
            except BaseException:
                self._rollback()
                raise
            try:
                self._c.execute("COMMIT")
            except sqlite3.Error as error:
                self._rollback()
                raise StorageError(f"commit failed: {error}") from error

    @property
    def lock(self) -> threading.RLock:
        return self._lock

    def _rollback(self) -> None:
        """SQLite may already have rolled back (disk full, interrupt); a second ROLLBACK must not mask the real error."""
        if self._c.in_transaction:
            try:
                self._c.execute("ROLLBACK")
            except sqlite3.Error:
                pass

    @contextmanager
    def read(self) -> Iterator[_SqliteReadTx]:
        """A snapshot of the last committed state. Readers never wait for a writer, nor for each other."""
        if self._shared:  # the only connection there is: take turns
            with self._lock:
                with self._snapshot(self._c) as tx:
                    yield tx
            return
        with self._snapshot(self._reader()) as tx:
            yield tx

    def _reader(self) -> sqlite3.Connection:
        held: _Reader | None = getattr(self._readers, "held", None)
        connection = held.connection if held is not None else None
        if connection is None:
            try:
                connection = sqlite3.connect(self._path, isolation_level=None, check_same_thread=False, timeout=30)
                connection.row_factory = sqlite3.Row
                connection.execute("PRAGMA query_only = ON")  # this connection can only read
            except sqlite3.Error as error:
                raise StorageError(f"cannot open a reader on {self._path}: {error}") from error
            held = self._readers.held = _Reader(connection)
            with self._reader_lock:
                self._reader_connections.add(connection)
            # the thread-local holder goes when its thread does, and the connection with it
            finalizer = weakref.finalize(held, _release_reader, connection, self._reader_connections, self._reader_lock)
            finalizer.atexit = (
                False  # a function and the set, not `self`: the finalizer must not keep the storage alive
            )
        return connection

    @contextmanager
    def _snapshot(self, connection: sqlite3.Connection) -> Iterator[_SqliteReadTx]:
        joined = connection.in_transaction  # a read inside a read on one thread shares the outer snapshot
        if not joined:
            try:
                connection.execute("BEGIN")  # deferred: nothing is locked until the first read
            except sqlite3.Error as error:
                raise StorageError(f"cannot begin read transaction: {error}") from error
        tx = _SqliteReadTx(connection)
        try:
            yield tx
        except sqlite3.Error as error:
            raise StorageError(f"storage failed: {error}") from error
        finally:
            tx.finish()
            try:
                if not joined and connection.in_transaction:
                    connection.execute("ROLLBACK")  # nothing to commit
            except sqlite3.Error:
                pass  # the connection went away (closed, interrupted): there is nothing left to roll back

    @contextmanager
    def transaction(self) -> Iterator[_SqliteTx]:
        with self._lock:
            try:
                self._c.execute("BEGIN IMMEDIATE")
            except sqlite3.Error as error:
                raise StorageError(f"cannot begin transaction: {error}") from error
            tx = _SqliteTx(self._c)
            try:
                yield tx
            except sqlite3.Error as error:
                tx.finish()
                self._rollback()
                raise StorageError(f"storage failed: {error}") from error
            except BaseException:
                tx.finish()
                self._rollback()
                raise
            tx.finish()
            try:
                self._c.execute("COMMIT")
            except sqlite3.Error as error:
                self._rollback()
                raise StorageError(f"commit failed: {error}") from error

    def close(self) -> None:
        with self._reader_lock:
            for connection in self._reader_connections:
                connection.close()
            self._reader_connections.clear()
        self._readers = threading.local()
        self._c.close()
