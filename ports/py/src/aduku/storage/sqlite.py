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

SCHEMA_VERSION = 6  # raise it when SCHEMA or the upgrade steps below change; features 001 to 005 left it at 0

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
    valid_time TEXT,
    synthetic TEXT,
    purged INTEGER NOT NULL DEFAULT 0,
    UNIQUE (doc, seq)
);
CREATE UNIQUE INDEX IF NOT EXISTS entries_idem_key ON entries (doc, idem_key) WHERE idem_key IS NOT NULL;
CREATE INDEX IF NOT EXISTS entries_request ON entries (request_id);
CREATE INDEX IF NOT EXISTS entries_tier ON entries (tier);
-- Feature 005: compaction, archive restore and purge are the only operations that change existing entries. They set
-- ledger_rewrite.active inside their own transaction; any other update or delete is still refused.
CREATE TABLE IF NOT EXISTS ledger_rewrite (id INTEGER PRIMARY KEY CHECK (id = 1), active INTEGER NOT NULL);
INSERT OR IGNORE INTO ledger_rewrite (id, active) VALUES (1, 0);
DROP TRIGGER IF EXISTS entries_no_update;
CREATE TRIGGER entries_no_update BEFORE UPDATE ON entries
WHEN (SELECT active FROM ledger_rewrite WHERE id = 1) = 0
BEGIN SELECT RAISE(ABORT, 'entries are append-only'); END;
DROP TRIGGER IF EXISTS entries_no_delete;
CREATE TRIGGER entries_no_delete BEFORE DELETE ON entries
WHEN (SELECT active FROM ledger_rewrite WHERE id = 1) = 0
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
    effects_seq INTEGER NOT NULL DEFAULT 0,
    epoch INTEGER NOT NULL DEFAULT 0
);

-- Feature 005: saved per-tier layers (disposable: always re-derivable from the ledger) and the document epoch.
CREATE TABLE IF NOT EXISTS layers (
    doc TEXT NOT NULL,
    tier TEXT NOT NULL,
    tier_seq INTEGER NOT NULL,
    schema_version INTEGER NOT NULL,
    rules_fingerprint TEXT NOT NULL,
    epoch INTEGER NOT NULL,
    state TEXT NOT NULL,
    PRIMARY KEY (doc, tier)
);
CREATE TABLE IF NOT EXISTS doc_epoch (doc TEXT PRIMARY KEY, epoch INTEGER NOT NULL);

-- Feature 005: compaction. The archive holds the originals a compaction replaced until an operator expires them; the sources,
-- the request index and the retired idempotency keys outlive the archive.
CREATE TABLE IF NOT EXISTS archive_entries (
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
    valid_time TEXT,
    synthetic TEXT,
    purged INTEGER NOT NULL DEFAULT 0,
    compaction_id TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS archive_doc_seq ON archive_entries (doc, seq);
CREATE INDEX IF NOT EXISTS archive_compaction ON archive_entries (compaction_id, seq);
CREATE INDEX IF NOT EXISTS archive_doc_tx_time ON archive_entries (doc, tx_time);
CREATE TABLE IF NOT EXISTS compactions (
    id TEXT PRIMARY KEY,
    doc TEXT NOT NULL,
    watermark_seq INTEGER NOT NULL,
    schema_version INTEGER NOT NULL,
    fold_fingerprint TEXT NOT NULL,
    created TEXT NOT NULL,
    policy TEXT NOT NULL,
    base_errors TEXT NOT NULL,
    base_skipped TEXT NOT NULL,
    archived INTEGER NOT NULL,
    first_tx_time TEXT,
    last_tx_time TEXT,
    has_valid_time INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS compactions_doc ON compactions (doc, watermark_seq);
CREATE TABLE IF NOT EXISTS compaction_sources (
    compaction_id TEXT NOT NULL,
    source_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    tier TEXT NOT NULL,
    idem_key TEXT,
    PRIMARY KEY (compaction_id, source_id)
);
CREATE TABLE IF NOT EXISTS request_entries (
    request_id TEXT NOT NULL,
    entry_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    PRIMARY KEY (request_id, entry_id)
);
CREATE TABLE IF NOT EXISTS purge_log (
    id TEXT PRIMARY KEY,
    doc TEXT NOT NULL,
    scope TEXT NOT NULL,
    reason TEXT NOT NULL,
    actor TEXT NOT NULL,
    at TEXT NOT NULL,
    counts TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS purge_log_doc ON purge_log (doc, at);
DROP TRIGGER IF EXISTS purge_log_no_update;
CREATE TRIGGER purge_log_no_update BEFORE UPDATE ON purge_log
BEGIN SELECT RAISE(ABORT, 'the purge log is append-only'); END;
DROP TRIGGER IF EXISTS purge_log_no_delete;
CREATE TRIGGER purge_log_no_delete BEFORE DELETE ON purge_log
BEGIN SELECT RAISE(ABORT, 'the purge log is append-only'); END;
CREATE TABLE IF NOT EXISTS request_effects (request_id TEXT PRIMARY KEY, added TEXT NOT NULL, resolved TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS retired_idem_keys (doc TEXT NOT NULL, idem_key TEXT NOT NULL, PRIMARY KEY (doc, idem_key));
CREATE TABLE IF NOT EXISTS archive_expiry (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    doc TEXT NOT NULL,
    through_seq INTEGER NOT NULL,
    at TEXT NOT NULL,
    expired INTEGER NOT NULL
);
DROP TRIGGER IF EXISTS archive_no_update;
CREATE TRIGGER archive_no_update BEFORE UPDATE ON archive_entries
WHEN (SELECT active FROM ledger_rewrite WHERE id = 1) = 0
BEGIN SELECT RAISE(ABORT, 'the archive is append-only'); END;
DROP TRIGGER IF EXISTS archive_no_delete;
CREATE TRIGGER archive_no_delete BEFORE DELETE ON archive_entries
WHEN (SELECT active FROM ledger_rewrite WHERE id = 1) = 0
BEGIN SELECT RAISE(ABORT, 'the archive is append-only'); END;
DROP TRIGGER IF EXISTS sources_no_update;
CREATE TRIGGER sources_no_update BEFORE UPDATE ON compaction_sources
BEGIN SELECT RAISE(ABORT, 'compaction sources are append-only'); END;
DROP TRIGGER IF EXISTS sources_no_delete;
CREATE TRIGGER sources_no_delete BEFORE DELETE ON compaction_sources
BEGIN SELECT RAISE(ABORT, 'compaction sources are append-only'); END;

CREATE INDEX IF NOT EXISTS entries_doc_tx_time ON entries (doc, tx_time);
CREATE INDEX IF NOT EXISTS entries_doc_tier ON entries (doc, tier, seq);

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

-- Results of deferred fields (feature 003): the latest per document and field, mutable by design, never read by the core.
CREATE TABLE IF NOT EXISTS derived_values (
    doc TEXT NOT NULL,
    field TEXT NOT NULL,
    value TEXT,
    has_value INTEGER NOT NULL,
    as_of TEXT,
    input_fingerprint TEXT NOT NULL,
    hook TEXT NOT NULL,
    shape TEXT NOT NULL,
    schema_version INTEGER NOT NULL,
    status TEXT NOT NULL,
    error TEXT,
    attempts INTEGER NOT NULL,
    last_attempt TEXT,
    invalidated INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (doc, field)
);
CREATE INDEX IF NOT EXISTS derived_values_field ON derived_values (field, doc);
CREATE TABLE IF NOT EXISTS scheduler_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    hook TEXT NOT NULL,
    started TEXT NOT NULL,
    finished TEXT NOT NULL,
    attempted INTEGER NOT NULL,
    succeeded INTEGER NOT NULL,
    failed INTEGER NOT NULL,
    skipped INTEGER NOT NULL,
    calls INTEGER NOT NULL,
    reason TEXT
);
CREATE INDEX IF NOT EXISTS scheduler_runs_hook ON scheduler_runs (hook, id);
CREATE TABLE IF NOT EXISTS scheduler_leases (hook TEXT PRIMARY KEY, owner TEXT NOT NULL, expires TEXT NOT NULL);

-- Shared schema state (feature 006): active version, pins, served fingerprints, and an insert-only history.
CREATE TABLE IF NOT EXISTS schema_state (
    schema_id TEXT PRIMARY KEY,
    active_version INTEGER,
    generation INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS doc_pins (
    schema_id TEXT NOT NULL,
    doc TEXT NOT NULL,
    version INTEGER NOT NULL,
    PRIMARY KEY (schema_id, doc)
);
CREATE TABLE IF NOT EXISTS schema_versions (
    schema_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    fingerprint TEXT NOT NULL,
    parts TEXT NOT NULL,
    first_served TEXT NOT NULL,
    PRIMARY KEY (schema_id, version)
);
CREATE TABLE IF NOT EXISTS schema_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    schema_id TEXT NOT NULL,
    at TEXT NOT NULL,
    operator TEXT NOT NULL,
    action TEXT NOT NULL,
    doc TEXT,
    from_version INTEGER,
    to_version INTEGER,
    change_class TEXT,
    note TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS schema_history_id ON schema_history (schema_id, id);
DROP TRIGGER IF EXISTS schema_history_no_update;
CREATE TRIGGER schema_history_no_update BEFORE UPDATE ON schema_history
BEGIN SELECT RAISE(ABORT, 'the schema history is append-only'); END;
DROP TRIGGER IF EXISTS schema_history_no_delete;
CREATE TRIGGER schema_history_no_delete BEFORE DELETE ON schema_history
BEGIN SELECT RAISE(ABORT, 'the schema history is append-only'); END;
DROP TRIGGER IF EXISTS schema_versions_no_update;
CREATE TRIGGER schema_versions_no_update BEFORE UPDATE ON schema_versions
BEGIN SELECT RAISE(ABORT, 'schema versions are append-only'); END;
DROP TRIGGER IF EXISTS schema_versions_no_delete;
CREATE TRIGGER schema_versions_no_delete BEFORE DELETE ON schema_versions
BEGIN SELECT RAISE(ABORT, 'schema versions are append-only'); END;

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


def _maybe_stamp(moment: datetime | None) -> str | None:
    return None if moment is None else _stamp(moment)


def _maybe_unstamp(text: str | None) -> datetime | None:
    return None if text is None else _unstamp(text)


def _derived(row: sqlite3.Row) -> DerivedRecord:
    return DerivedRecord(
        doc=row["doc"],
        field=row["field"],
        value=row["value"],
        has_value=bool(row["has_value"]),
        as_of=_maybe_unstamp(row["as_of"]),
        input_fingerprint=row["input_fingerprint"],
        hook=row["hook"],
        shape=row["shape"],
        schema_version=row["schema_version"],
        status=row["status"],
        error=row["error"],
        attempts=row["attempts"],
        last_attempt=_maybe_unstamp(row["last_attempt"]),
        invalidated=bool(row["invalidated"]),
    )


def _run(row: sqlite3.Row) -> SchedulerRun:
    return SchedulerRun(
        run_id=row["run_id"],
        hook=row["hook"],
        started=_unstamp(row["started"]),
        finished=_unstamp(row["finished"]),
        attempted=row["attempted"],
        succeeded=row["succeeded"],
        failed=row["failed"],
        skipped=row["skipped"],
        calls=row["calls"],
        reason=row["reason"],
    )


def _chunks(items: Sequence[str], size: int = 400) -> Iterator[Sequence[str]]:
    for start in range(0, len(items), size):
        yield items[start : start + size]


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
        valid_time=_maybe_unstamp(row["valid_time"]),
        synthetic=row["synthetic"],
        purged=bool(row["purged"]),
    )


_ENTRY_COLUMNS = (
    "id, doc, seq, request_id, tier, path, op, value, contributor, tx_time, idem_key, request_key, request_hash,"
    " schema_version, lenient, meta, valid_time, synthetic, purged"
)


def _entry_values(entry: Entry) -> tuple[object, ...]:
    return (
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
        _maybe_stamp(entry.valid_time),
        entry.synthetic,
        int(entry.purged),
    )


def _record(row: sqlite3.Row) -> CompactionRecord:
    return CompactionRecord(
        id=row["id"],
        doc=row["doc"],
        watermark_seq=row["watermark_seq"],
        schema_version=row["schema_version"],
        fold_fingerprint=row["fold_fingerprint"],
        created=_unstamp(row["created"]),
        policy=row["policy"],
        base_errors=row["base_errors"],
        base_skipped=tuple(int(n) for n in row["base_skipped"].split(",") if n),
        archived=bool(row["archived"]),
        first_tx_time=_maybe_unstamp(row["first_tx_time"]),
        last_tx_time=_maybe_unstamp(row["last_tx_time"]),
        has_valid_time=bool(row["has_valid_time"]),
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
            # a loose index scan: one probe of the tier index per tier, not a pass over every entry
            rows = self._c.execute(
                "WITH RECURSIVE t(tier) AS (SELECT MIN(tier) FROM entries UNION ALL"
                " SELECT (SELECT MIN(tier) FROM entries WHERE tier > t.tier) FROM t WHERE tier IS NOT NULL)"
                " SELECT tier FROM t WHERE tier IS NOT NULL"
            )
        else:
            rows = self._c.execute("SELECT DISTINCT tier FROM entries WHERE doc = ?", (doc,))
        return {str(row[0]) for row in rows}

    @live
    def compacted_docs(self) -> list[str]:
        return [str(row[0]) for row in self._c.execute("SELECT doc FROM doc_epoch WHERE epoch > 0 ORDER BY doc")]

    @live
    def doc_ids(self) -> list[str]:
        return [str(row[0]) for row in self._c.execute("SELECT doc FROM doc_seq ORDER BY doc")]

    @live
    def entries(
        self, doc: str, since_seq: int = 0, limit: int | None = None, *, until_seq: int | None = None
    ) -> list[Entry]:
        rows = self._c.execute(
            "SELECT * FROM entries WHERE doc = ? AND seq > ? AND (? IS NULL OR seq <= ?) ORDER BY seq LIMIT ?",
            (doc, since_seq, until_seq, until_seq, -1 if limit is None else limit),
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
            or self._c.execute(
                "SELECT 1 FROM retired_idem_keys WHERE doc = ? AND idem_key = ?", (doc, idem_key)
            ).fetchone()
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
                    f"INSERT INTO entries ({_ENTRY_COLUMNS}) VALUES ({','.join('?' * 19)})", _entry_values(entry)
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
            row["epoch"],
        )

    @live
    def put_view(self, view: ViewRow) -> None:
        self._c.execute(
            "INSERT INTO views (doc, watermark_seq, schema_version, rules_fingerprint, document, model_errors, provenance,"
            " effects_seq, epoch) VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT (doc) DO UPDATE SET"
            " watermark_seq = excluded.watermark_seq,"
            " schema_version = excluded.schema_version, rules_fingerprint = excluded.rules_fingerprint,"
            " document = excluded.document, model_errors = excluded.model_errors, provenance = excluded.provenance,"
            " effects_seq = excluded.effects_seq, epoch = excluded.epoch",
            (
                view.doc,
                view.watermark_seq,
                view.schema_version,
                view.rules_fingerprint,
                view.document,
                view.model_errors,
                view.provenance,
                view.effects_seq,
                view.epoch,
            ),
        )

    @live
    def drop_view(self, doc: str) -> None:
        self._c.execute("DELETE FROM views WHERE doc = ?", (doc,))

    # -- saved layers and the epoch (feature 005) --------------------------------------------------------------------

    @live
    def get_layers(self, doc: str) -> list[LayerRow]:
        rows = self._c.execute("SELECT * FROM layers WHERE doc = ? ORDER BY tier", (doc,))
        return [
            LayerRow(
                r["doc"], r["tier"], r["tier_seq"], r["schema_version"], r["rules_fingerprint"], r["epoch"], r["state"]
            )
            for r in rows
        ]

    @live
    def put_layers(self, rows: Sequence[LayerRow]) -> None:
        for r in rows:
            self._c.execute(
                "INSERT INTO layers (doc, tier, tier_seq, schema_version, rules_fingerprint, epoch, state)"
                " VALUES (?,?,?,?,?,?,?) ON CONFLICT (doc, tier) DO UPDATE SET tier_seq = excluded.tier_seq,"
                " schema_version = excluded.schema_version, rules_fingerprint = excluded.rules_fingerprint,"
                " epoch = excluded.epoch, state = excluded.state"
                " WHERE excluded.epoch > layers.epoch OR (excluded.epoch = layers.epoch AND excluded.tier_seq >= layers.tier_seq)",
                (r.doc, r.tier, r.tier_seq, r.schema_version, r.rules_fingerprint, r.epoch, r.state),
            )

    @live
    def drop_layers(self, doc: str) -> None:
        self._c.execute("DELETE FROM layers WHERE doc = ?", (doc,))

    @live
    def tier_last_seq(self, doc: str, tier: str, through: int) -> int:
        row = self._c.execute(
            "SELECT MAX(seq) FROM entries WHERE doc = ? AND tier = ? AND seq <= ?", (doc, tier, through)
        ).fetchone()
        return int(row[0] or 0)

    @live
    def epoch(self, doc: str) -> int:
        row = self._c.execute("SELECT epoch FROM doc_epoch WHERE doc = ?", (doc,)).fetchone()
        return int(row[0]) if row else 0

    @live
    def bump_epoch(self, doc: str) -> int:
        self._c.execute(
            "INSERT INTO doc_epoch (doc, epoch) VALUES (?, 1) ON CONFLICT (doc) DO UPDATE SET epoch = epoch + 1", (doc,)
        )
        return self.epoch(doc)

    @live
    def seq_at_tx_time(self, doc: str, moment: datetime) -> int:
        stamp = _stamp(moment)
        row = self._c.execute("SELECT MAX(seq) FROM entries WHERE doc = ? AND tx_time <= ?", (doc, stamp)).fetchone()
        best = int(row[0] or 0)
        row = self._c.execute(
            "SELECT MAX(seq) FROM archive_entries WHERE doc = ? AND tx_time <= ?", (doc, stamp)
        ).fetchone()
        return max(best, int(row[0] or 0))

    # -- compaction, archive and expiry (feature 005) ------------------------------------------------------------------

    @live
    def apply_compaction(self, plan: CompactionPlan) -> None:
        doc, record = plan.record.doc, plan.record
        found = self._c.execute(
            "SELECT * FROM entries WHERE doc = ? AND seq <= ? ORDER BY seq", (doc, record.watermark_seq)
        )
        present = {row["id"]: _entry(row) for row in found}
        if present != {entry.id: entry for entry in plan.replaced}:
            raise ConstraintError("the entries up to the watermark are not the ones the compaction expects")
        self._c.execute("SAVEPOINT apply_compaction")
        self._c.execute("UPDATE ledger_rewrite SET active = 1 WHERE id = 1")
        try:
            self._c.execute(
                f"INSERT INTO archive_entries ({_ENTRY_COLUMNS}, compaction_id)"
                f" SELECT {_ENTRY_COLUMNS}, ? FROM entries WHERE doc = ? AND seq <= ?",
                (record.id, doc, record.watermark_seq),
            )
            self._c.execute("DELETE FROM entries WHERE doc = ? AND seq <= ?", (doc, record.watermark_seq))
            for entry in plan.synthetic:
                self._c.execute(
                    f"INSERT INTO entries ({_ENTRY_COLUMNS}) VALUES ({','.join('?' * 19)})", _entry_values(entry)
                )
            self._c.execute(
                "INSERT INTO compactions (id, doc, watermark_seq, schema_version, fold_fingerprint, created, policy,"
                " base_errors, base_skipped, archived, first_tx_time, last_tx_time, has_valid_time)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    record.id,
                    doc,
                    record.watermark_seq,
                    record.schema_version,
                    record.fold_fingerprint,
                    _stamp(record.created),
                    record.policy,
                    record.base_errors,
                    ",".join(str(n) for n in record.base_skipped),
                    int(record.archived),
                    _maybe_stamp(record.first_tx_time),
                    _maybe_stamp(record.last_tx_time),
                    int(record.has_valid_time),
                ),
            )
            self._c.executemany(
                "INSERT INTO compaction_sources (compaction_id, source_id, seq, tier, idem_key) VALUES (?,?,?,?,?)",
                [(s.compaction_id, s.source_id, s.seq, s.tier, s.idem_key) for s in plan.sources],
            )
            self._c.executemany(
                "INSERT OR IGNORE INTO request_entries (request_id, entry_id, seq) VALUES (?,?,?)",
                list(plan.request_index),
            )
            self._c.executemany(
                "INSERT OR IGNORE INTO retired_idem_keys (doc, idem_key) VALUES (?,?)",
                [(doc, k) for k in plan.retired_keys],
            )
            self._c.executemany(
                "INSERT OR REPLACE INTO request_effects (request_id, added, resolved) VALUES (?,?,?)",
                list(plan.request_effects),
            )
            self._c.execute("UPDATE ledger_rewrite SET active = 0 WHERE id = 1")
            self.bump_epoch(doc)
        except sqlite3.IntegrityError as error:
            self._undo_compaction()
            raise ConstraintError(str(error)) from error
        except BaseException:
            self._undo_compaction()
            raise
        self._c.execute("RELEASE apply_compaction")

    def _undo_compaction(self) -> None:
        self._c.execute("ROLLBACK TO apply_compaction")
        self._c.execute("RELEASE apply_compaction")
        self._c.execute("UPDATE ledger_rewrite SET active = 0 WHERE id = 1")

    @live
    def compactions(self, doc: str) -> list[CompactionRecord]:
        rows = self._c.execute("SELECT * FROM compactions WHERE doc = ? ORDER BY watermark_seq, created, id", (doc,))
        return [_record(row) for row in rows]

    @live
    def sources_of(self, compaction_id: str) -> list[SourceRow]:
        rows = self._c.execute(
            "SELECT * FROM compaction_sources WHERE compaction_id = ? ORDER BY seq", (compaction_id,)
        )
        return [SourceRow(r["compaction_id"], r["source_id"], r["seq"], r["tier"], r["idem_key"]) for r in rows]

    @live
    def archive_of(self, compaction_id: str) -> list[Entry]:
        rows = self._c.execute("SELECT * FROM archive_entries WHERE compaction_id = ? ORDER BY seq", (compaction_id,))
        return [_entry(row) for row in rows]

    @live
    def archived_entries(self, doc: str, through_seq: int | None = None) -> list[Entry]:
        rows = self._c.execute(
            "SELECT * FROM archive_entries WHERE doc = ? AND (? IS NULL OR seq <= ?) ORDER BY seq, compaction_id",
            (doc, through_seq, through_seq),
        )
        return [_entry(row) for row in rows]

    @live
    def request_entries(self, request_id: str) -> list[tuple[str, int]]:
        rows = self._c.execute(
            "SELECT id, seq FROM entries WHERE request_id = ? ORDER BY seq", (request_id,)
        ).fetchall()
        if not rows:
            rows = self._c.execute(
                "SELECT entry_id, seq FROM request_entries WHERE request_id = ? ORDER BY seq", (request_id,)
            ).fetchall()
        return [(str(row[0]), int(row[1])) for row in rows]

    @live
    def request_effects(self, request_id: str) -> tuple[str, str] | None:
        row = self._c.execute(
            "SELECT added, resolved FROM request_effects WHERE request_id = ?", (request_id,)
        ).fetchone()
        return None if row is None else (str(row[0]), str(row[1]))

    @live
    def expire_archive(self, doc: str, through_seq: int, at: datetime) -> int:
        doomed = [
            row["id"]
            for row in self._c.execute(
                "SELECT id FROM compactions WHERE doc = ? AND archived = 1 AND watermark_seq <= ?", (doc, through_seq)
            )
        ]
        if not doomed:
            return 0
        marks = ",".join("?" * len(doomed))
        self._c.execute("UPDATE ledger_rewrite SET active = 1 WHERE id = 1")
        try:
            removed = self._c.execute(f"DELETE FROM archive_entries WHERE compaction_id IN ({marks})", doomed).rowcount
        finally:
            self._c.execute("UPDATE ledger_rewrite SET active = 0 WHERE id = 1")
        self._c.execute(f"UPDATE compactions SET archived = 0 WHERE id IN ({marks})", doomed)
        self._c.execute(
            "INSERT INTO archive_expiry (doc, through_seq, at, expired) VALUES (?,?,?,?)",
            (doc, through_seq, _stamp(at), removed),
        )
        return int(removed)

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

    # -- purge (feature 005) -----------------------------------------------------------------------------------------

    @live
    def apply_purge(self, application: PurgeApplication) -> tuple[int, int]:
        doc = application.doc
        for table, entries in (("entries", application.live), ("archive_entries", application.archive)):
            for entry in entries:
                found = self._c.execute(f"SELECT 1 FROM {table} WHERE id = ? AND doc = ?", (entry.id, doc)).fetchone()
                if found is None:
                    raise ConstraintError(f"entry {entry.id!r} is not in {table}")
        self._c.execute("SAVEPOINT apply_purge")
        self._c.execute("UPDATE ledger_rewrite SET active = 1 WHERE id = 1")
        try:
            for table, entries in (("entries", application.live), ("archive_entries", application.archive)):
                for entry in entries:
                    self._c.execute(
                        f"UPDATE {table} SET value = ?, meta = ?, valid_time = ?, request_hash = ?, purged = ? WHERE id = ?",
                        (
                            None if entry.purged else to_canonical(entry.value),
                            to_canonical(entry.meta) if entry.meta is not None else None,
                            _maybe_stamp(entry.valid_time),
                            entry.request_hash,
                            int(entry.purged),
                            entry.id,
                        ),
                    )
            self._c.execute("UPDATE ledger_rewrite SET active = 0 WHERE id = 1")
            for start in range(0, len(application.requests), 400):
                chunk = application.requests[start : start + 400]
                self._c.execute(
                    f"UPDATE requests SET request_hash = NULL WHERE request_id IN ({','.join('?' * len(chunk))})", chunk
                )
            self._c.execute("DELETE FROM layers WHERE doc = ?", (doc,))
            self._c.execute("DELETE FROM views WHERE doc = ?", (doc,))
            self.bump_epoch(doc)
            effects = derived = 0
            names = list(application.fields)
            for start in range(0, len(names), 400):
                fields_chunk = names[start : start + 400]
                marks = ",".join("?" * len(fields_chunk))
                self._c.execute("UPDATE effects_pruning SET active = 1 WHERE id = 1")
                try:
                    effects += self._c.execute(
                        f"DELETE FROM effects WHERE doc = ? AND field IN ({marks})", (doc, *fields_chunk)
                    ).rowcount
                finally:
                    self._c.execute("UPDATE effects_pruning SET active = 0 WHERE id = 1")
                derived += self._c.execute(
                    f"DELETE FROM derived_values WHERE doc = ? AND field IN ({marks})", (doc, *fields_chunk)
                ).rowcount
        except BaseException:
            self._c.execute("ROLLBACK TO apply_purge")
            self._c.execute("RELEASE apply_purge")
            self._c.execute("UPDATE ledger_rewrite SET active = 0 WHERE id = 1")
            self._c.execute("UPDATE effects_pruning SET active = 0 WHERE id = 1")
            raise
        self._c.execute("RELEASE apply_purge")
        return int(effects), int(derived)

    @live
    def append_purge_log(self, record: PurgeRecord) -> None:
        self._c.execute(
            "INSERT INTO purge_log (id, doc, scope, reason, actor, at, counts) VALUES (?,?,?,?,?,?,?)",
            (record.id, record.doc, record.scope, record.reason, record.actor, _stamp(record.at), record.counts),
        )

    @live
    def purge_log(self, doc: str) -> list[PurgeRecord]:
        rows = self._c.execute("SELECT * FROM purge_log WHERE doc = ? ORDER BY at, rowid", (doc,))
        return [
            PurgeRecord(r["id"], r["doc"], r["scope"], r["reason"], r["actor"], _unstamp(r["at"]), r["counts"])
            for r in rows
        ]

    # -- the derived store of deferred fields (feature 003) ----------------------------------------------------------

    @live
    def get_derived(self, doc: str) -> list[DerivedRecord]:
        rows = self._c.execute("SELECT * FROM derived_values WHERE doc = ? ORDER BY field", (doc,)).fetchall()
        return [_derived(row) for row in rows]

    @live
    def put_derived(self, records: Sequence[DerivedRecord]) -> None:
        for r in records:
            self._c.execute(
                "INSERT INTO derived_values (doc, field, value, has_value, as_of, input_fingerprint, hook, shape,"
                " schema_version, status, error, attempts, last_attempt, invalidated)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
                " ON CONFLICT (doc, field) DO UPDATE SET value = excluded.value, has_value = excluded.has_value,"
                " as_of = excluded.as_of, input_fingerprint = excluded.input_fingerprint, hook = excluded.hook,"
                " shape = excluded.shape, schema_version = excluded.schema_version, status = excluded.status,"
                " error = excluded.error, attempts = excluded.attempts, last_attempt = excluded.last_attempt,"
                " invalidated = excluded.invalidated",
                (
                    r.doc,
                    r.field,
                    r.value,
                    int(r.has_value),
                    _maybe_stamp(r.as_of),
                    r.input_fingerprint,
                    r.hook,
                    r.shape,
                    r.schema_version,
                    r.status,
                    r.error,
                    r.attempts,
                    _maybe_stamp(r.last_attempt),
                    int(r.invalidated),
                ),
            )

    def _where(self, fields: Sequence[str] | None, docs: Sequence[str] | None) -> Iterator[tuple[str, list[str]]]:
        """WHERE clauses covering the given fields and documents, split so no statement has too many parameters."""
        field_parts = list(_chunks(fields)) if fields is not None else [None]
        doc_parts = list(_chunks(docs)) if docs is not None else [None]
        for field_part in field_parts:
            for doc_part in doc_parts:
                clauses: list[str] = []
                params: list[str] = []
                if field_part is not None:
                    clauses.append(f"field IN ({','.join('?' * len(field_part))})")
                    params.extend(field_part)
                if doc_part is not None:
                    clauses.append(f"doc IN ({','.join('?' * len(doc_part))})")
                    params.extend(doc_part)
                yield (" AND ".join(clauses) or "1"), params

    @live
    def list_derived(self, fields: Sequence[str], docs: Sequence[str] | None = None) -> list[DerivedRecord]:
        if not fields or (docs is not None and not docs):
            return []
        found: list[DerivedRecord] = []
        for clause, params in self._where(fields, docs):
            rows = self._c.execute(f"SELECT * FROM derived_values WHERE {clause}", params).fetchall()
            found.extend(_derived(row) for row in rows)
        return sorted(found, key=lambda r: (r.doc, r.field))

    @live
    def mark_invalidated(self, fields: Sequence[str], docs: Sequence[str] | None = None) -> int:
        if not fields or (docs is not None and not docs):
            return 0
        total = 0
        for clause, params in self._where(fields, docs):
            total += self._c.execute(f"UPDATE derived_values SET invalidated = 1 WHERE {clause}", params).rowcount
        return total

    @live
    def delete_derived(self, docs: Sequence[str] | None = None, fields: Sequence[str] | None = None) -> int:
        if (docs is not None and not docs) or (fields is not None and not fields):
            return 0
        total = 0
        for clause, params in self._where(fields, docs):
            total += self._c.execute(f"DELETE FROM derived_values WHERE {clause}", params).rowcount
        return total

    @live
    def append_run(self, run: SchedulerRun) -> None:
        self._c.execute(
            "INSERT INTO scheduler_runs (run_id, hook, started, finished, attempted, succeeded, failed, skipped, calls,"
            " reason) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                run.run_id,
                run.hook,
                _stamp(run.started),
                _stamp(run.finished),
                run.attempted,
                run.succeeded,
                run.failed,
                run.skipped,
                run.calls,
                run.reason,
            ),
        )

    @live
    def list_runs(self, hook: str, limit: int | None = None) -> list[SchedulerRun]:
        rows = self._c.execute("SELECT * FROM scheduler_runs WHERE hook = ? ORDER BY id", (hook,)).fetchall()
        found = [_run(row) for row in rows]
        return found if limit is None else found[max(len(found) - limit, 0) :]

    @live
    def take_lease(self, hook: str, owner: str, expires: datetime, now: datetime) -> bool:
        cursor = self._c.execute(
            "INSERT INTO scheduler_leases (hook, owner, expires) VALUES (?,?,?) ON CONFLICT (hook) DO UPDATE SET"
            " owner = excluded.owner, expires = excluded.expires"
            " WHERE scheduler_leases.owner = excluded.owner OR scheduler_leases.expires <= ?",
            (hook, owner, _stamp(expires), _stamp(now)),
        )
        return cursor.rowcount > 0

    @live
    def renew_lease(self, hook: str, owner: str, expires: datetime) -> bool:
        cursor = self._c.execute(
            "UPDATE scheduler_leases SET expires = ? WHERE hook = ? AND owner = ?", (_stamp(expires), hook, owner)
        )
        return cursor.rowcount > 0

    @live
    def release_lease(self, hook: str, owner: str) -> None:
        self._c.execute("DELETE FROM scheduler_leases WHERE hook = ? AND owner = ?", (hook, owner))

    @live
    def schema_generation(self, schema_id: str) -> int:
        row = self._c.execute("SELECT generation FROM schema_state WHERE schema_id = ?", (schema_id,)).fetchone()
        return int(row[0]) if row else 0

    @live
    def schema_state(self, schema_id: str) -> tuple[int, int | None, dict[str, int]]:
        row = self._c.execute(
            "SELECT generation, active_version FROM schema_state WHERE schema_id = ?", (schema_id,)
        ).fetchone()
        pins = {
            str(r[0]): int(r[1])
            for r in self._c.execute("SELECT doc, version FROM doc_pins WHERE schema_id = ?", (schema_id,))
        }
        if row is None:
            return 0, None, pins
        return int(row[0]), (None if row[1] is None else int(row[1])), pins

    @live
    def served_versions(self, schema_id: str) -> dict[int, tuple[str, str]]:
        rows = self._c.execute(
            "SELECT version, fingerprint, parts FROM schema_versions WHERE schema_id = ?", (schema_id,)
        )
        return {int(r[0]): (str(r[1]), str(r[2])) for r in rows}

    @live
    def apply_schema_change(self, change: StateChange, expect_generation: int | None) -> int:
        sid = change.schema_id
        row = self._c.execute(
            "SELECT generation, active_version FROM schema_state WHERE schema_id = ?", (sid,)
        ).fetchone()
        generation = int(row[0]) if row else 0
        active = None if row is None or row[1] is None else int(row[1])
        if expect_generation is not None and expect_generation != generation:
            raise ConstraintError(f"schema state generation is {generation}, expected {expect_generation}")
        for version, fingerprint, parts in change.served:
            known = self._c.execute(
                "SELECT fingerprint FROM schema_versions WHERE schema_id = ? AND version = ?", (sid, version)
            ).fetchone()
            if known is None:
                self._c.execute(
                    "INSERT INTO schema_versions (schema_id, version, fingerprint, parts, first_served) VALUES (?,?,?,?,?)",
                    (sid, version, fingerprint, parts, _stamp(change.at)),
                )
            elif known[0] != fingerprint:
                raise ConstraintError(f"schema version {version} was already served with another fingerprint")
        if change.pin_doc is not None:
            if change.pin_version is None:
                self._c.execute("DELETE FROM doc_pins WHERE schema_id = ? AND doc = ?", (sid, change.pin_doc))
            else:
                self._c.execute(
                    "INSERT INTO doc_pins (schema_id, doc, version) VALUES (?,?,?)"
                    " ON CONFLICT (schema_id, doc) DO UPDATE SET version = excluded.version",
                    (sid, change.pin_doc, change.pin_version),
                )
        for doc, version in change.pins:
            self._c.execute(
                "INSERT INTO doc_pins (schema_id, doc, version) VALUES (?,?,?)"
                " ON CONFLICT (schema_id, doc) DO UPDATE SET version = excluded.version",
                (sid, doc, version),
            )
        new_active = active if change.active is None else change.active
        self._c.execute(
            "INSERT INTO schema_state (schema_id, active_version, generation) VALUES (?,?,?)"
            " ON CONFLICT (schema_id) DO UPDATE SET active_version = excluded.active_version,"
            " generation = excluded.generation",
            (sid, new_active, generation + 1),
        )
        self._c.execute(
            "INSERT INTO schema_history (schema_id, at, operator, action, doc, from_version, to_version,"
            " change_class, note) VALUES (?,?,?,?,?,?,?,?,?)",
            (
                sid,
                _stamp(change.at),
                change.operator,
                change.action,
                change.doc,
                change.from_version,
                change.to_version,
                change.change_class,
                change.note,
            ),
        )
        return generation + 1

    @live
    def schema_history(self, schema_id: str, limit: int | None = None, doc: str | None = None) -> list[HistoryRecord]:
        sql = "SELECT * FROM schema_history WHERE schema_id = ?"
        params: list[object] = [schema_id]
        if doc is not None:
            sql += " AND doc = ?"
            params.append(doc)
        found = [
            HistoryRecord(
                id=int(r["id"]),
                at=_unstamp(r["at"]),
                operator=str(r["operator"]),
                action=str(r["action"]),
                doc=r["doc"],
                from_version=r["from_version"],
                to_version=r["to_version"],
                change_class=r["change_class"],
                note=str(r["note"]),
            )
            for r in self._c.execute(sql + " ORDER BY id", params).fetchall()
        ]
        return found if limit is None else found[max(len(found) - limit, 0) :]


class _SqliteReadTx(_SqliteTx):
    """A read-only handle: reads like any transaction, refuses every write."""

    def append_request(self, request: RequestRecord, entries: Sequence[Entry]) -> None:
        raise RuntimeError("read-only transaction")

    def put_view(self, view: ViewRow) -> None:
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

    def update_attempted(self, row_id: int, attempted: str) -> None:
        self._run("UPDATE rejection_audit SET attempted = ? WHERE id = ?", (attempted, row_id))

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
            if connection.execute("PRAGMA user_version").fetchone()[0] < SCHEMA_VERSION:
                # only a database made by older code is brought up to date: opening a current one writes nothing, so it
                # never waits for (or blocks) a writer to run DDL
                connection.executescript(SCHEMA)
                columns = {row["name"] for row in connection.execute("PRAGMA table_info(views)")}
                if "effects_seq" not in columns:  # a database made before feature 002
                    connection.execute("ALTER TABLE views ADD COLUMN effects_seq INTEGER NOT NULL DEFAULT 0")
                if "epoch" not in columns:  # a database made before feature 005
                    connection.execute("ALTER TABLE views ADD COLUMN epoch INTEGER NOT NULL DEFAULT 0")
                entry_columns = {row["name"] for row in connection.execute("PRAGMA table_info(entries)")}
                for column, definition in (
                    ("valid_time", "TEXT"),
                    ("synthetic", "TEXT"),
                    ("purged", "INTEGER NOT NULL DEFAULT 0"),
                ):
                    if column not in entry_columns:
                        connection.execute(f"ALTER TABLE entries ADD COLUMN {column} {definition}")
                connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
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
