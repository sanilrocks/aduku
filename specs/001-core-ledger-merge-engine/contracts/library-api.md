# Contract: Library API (`aduku-py`)

Python rendering of SPEC 12. Other ports expose the same operations and shapes in their own idiom.

```python
from aduku import Aduku, SqliteStorage, Clock, IdSource

db = Aduku(storage=SqliteStorage("aduku.db"), clock=Clock.system(), ids=IdSource.system(), schemas=registry)
```

## Write

```python
db.append(
    doc: str,
    entries: Sequence[EntryInput],        # EntryInput(tier, path, op="set", value=..., idem_key=None, meta=None)
    *,
    contributor: Identity,                # authenticated identity + permitted tiers/roles (provided by the caller)
    lenient: bool = False,
    expected_seq: int | None = None,
    request_key: str | None = None,       # caller-supplied retry key; same key + same payload = no-op duplicate
) -> AppendResult | raises RejectedError
```

- `AppendResult`: `entries: [(id, seq)]`, `request_id`, `model_errors_added`, `model_errors_resolved`,
  `duplicate: bool`, `view` (latest `DocumentResult`). A duplicate returns the original entries and the same shape; its `view` is the latest view of the document (not a copy of
  the first attempt's), and a failure to read it is raised, because the retry wrote nothing.
- `RejectedError`: `code` (primary), `reasons: [Reason]`. Raised only after the transaction rolled back; the ledger and
  every view are unchanged. If auditing is on for the document, one audit record is written separately.
- All-or-nothing: no partial acceptance mode exists.

## Read

```python
db.get(doc: str) -> DocumentResult        # { document, model_errors, status, skipped, conflicts }
db.explain(doc: str, path: str) -> Explanation   # { path, value, winner, winner_seq, rule, inputs, shadowed, model_errors }
db.list_entries(doc: str, *, since_seq: int = 0, limit: int | None = None) -> list[Entry]
db.rebuild(doc: str | None = None) -> RebuildReport   # None = all; verifies cache equals fresh derivation
```

`get` re-derives when the cached view's schema version or rules fingerprint differs from the active (or the document's
pinned) version, then keeps the result in memory. It never writes to the ledger or takes the write lock, with one
exception added by feature 002: a read that finds an impure computed field with no recorded outcome calls its hook and
records the outcome in a short write transaction. The stored view is written by `append` and `rebuild`. A retried request is answered from a read, without the write lock.

`explain` names the entry that supplied the value (or `"default"`; `None` when nothing sets the path), the rule as
`name@version`, the entries the value depends on, and the entries that make no difference to it. It is worked out from the
ledger when asked. A path the schema does not declare is `UNKNOWN_PATH`.

`rebuild` derives from the ledger alone and compares with the cached view. `RebuildReport` has `checked`, `differing`
(a current view that was not what the ledger gives; it is repaired), `refreshed` (no usable view: missing, or from
another version; it is stored), `failed` (documents that could not be derived, with the reason; the run goes on) and
`identical` (no `differing` and no `failed`). The fingerprint is a digest of everything in the
schema version that decides a document, so the same version number with other rules is never served from the cache.

## Schema

```python
registry.load(path_or_text) -> Schema     # fail closed with SchemaError listing every problem; returns the schema, does not register it
registry.put(schema) / registry.activate(version) / registry.pin(doc, version | None)
# activate raises SchemaError (code TIER_IN_USE) when the version drops or renames a tier used by an existing entry;
# pin(doc, v) checks the same for that document's entries. Reading under an unregistered or inactive version: SCHEMA_INACTIVE
behaviors.register_merge(name, version, combine, laws=Laws(associative=True, ...))
behaviors.register_validate(name, version, check, check_args=None, *, args=None, applies="either")   # a field rule is given only its value; a document rule gets the document; see feature 004 for ArgSpec
```

`register_merge` without `associative=True` succeeds, but any schema referencing the behavior is refused at load (FR-011).

## Rejection audit (admin)

```python
db.list_rejections(doc, *, since=None, limit=None) -> list[AuditRecord]
db.set_rejection_audit(doc, "enabled" | "disabled" | "inherit")   # mode (full | metadata_only) comes from the schema
db.prune_rejections(doc=None) -> int
```

Enabling auditing for a document whose effective retention has no limit raises `SchemaError`/`ConfigError`.

## Error codes

`FIELD_NOT_WRITABLE`, `NOT_AUTHORIZED`, `TIER_NOT_PERMITTED`, `UNKNOWN_PATH`, `TYPE_MISMATCH`, `OP_NOT_ALLOWED`,
`VALIDATION_FAILED`, `MODEL_ERROR_INTRODUCED`, `LENIENT_NOT_PERMITTED`, `IDEM_KEY_REQUIRED`, `SEQ_CONFLICT`,
`SCHEMA_INACTIVE`, `REQUEST_KEY_REUSED`, `DUPLICATE_IDEM_KEY` (SPEC 12.5). `STORAGE_ERROR` is raised, not as a rejection, when
storage fails during a commit or a read; no entry of the request is visible.

## Guarantees

- Output documents and model errors are byte-identical (canonical Ion text) for identical ledgers and schema versions.
- `get` of a document with no entries returns the defaults only; since feature 002 it also holds each computed field as
  `computed::null` (or leaves it out under `on_missing: absent`).
- Concurrent `append` calls to one document receive distinct, strictly increasing `seq`.
