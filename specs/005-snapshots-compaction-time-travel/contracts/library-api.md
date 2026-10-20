# Contract: Library API, delta to Features 001 to 004

Python rendering; other ports expose the same operations. Everything not mentioned is unchanged.

## Contributions

```python
EntryInput(tier, path, op="set", value=None, idem_key=None, meta=None, valid_time=None)   # valid_time: timezone-aware datetime
```

`valid_time` that is not a timezone-aware timestamp rejects the whole request with `BAD_VALID_TIME` (reason naming the entry's
index and path, never the value) before anything is recorded. A request without `valid_time` has the same hash as before.
`Entry.valid_time`, `Entry.synthetic` and `Entry.purged` are readable through `list_entries`. A contributor cannot send `op="layer"`.

## Reads

```python
@dataclass(frozen=True)
class AsOf:                       # exactly one of the three
    seq: int | None = None
    tx_time: datetime | None = None
    valid_time: datetime | None = None

db.get(doc, *, consistency="cached", include=(), as_of: AsOf | None = None) -> DocumentResult
db.explain(doc, path, *, as_of: AsOf | None = None) -> Explanation
```

- `AsOf` with none or more than one point: `RejectedError(AS_OF_AMBIGUOUS)` (a refusal; nothing is recorded or read).
- `DocumentResult.as_of: AsOfInfo | None` is set only for an `as_of` read: `kind` ("seq", "tx_time", "valid_time"), `point`,
  `resolved_seq` (the seq the read stands at; for `valid_time`, the highest seq that took part), `included` (the number of
  entries that took part), `inside_request` (true when the seq falls between two entries of one request).
- An `as_of` read writes nothing and calls no hook or source. Deferred fields are listed in `deferred` with status `unavailable`
  and no value in `document`. An impure compute without a recorded outcome at that point yields a model error with rule
  `NO_RECORDED_OUTCOME`, never a call. The read uses the document's current schema version.
- A point inside a compacted range is answered from the archived originals while they are kept; otherwise
  `HistoryCompactedError(HISTORY_COMPACTED, earliest_seq=<lowest answerable seq>)`. After expiry this also applies to a
  `tx_time` between the earliest and latest recording times of a compacted range and to any `valid_time` when a compacted
  entry carried one (research R-11); a seq at or above the watermark, a `tx_time` outside that interval, and a `valid_time`
  when none carried one are still answered.
- `explain(..., as_of)` selects the same entries.

`Explanation` gains `purged` (marks of purged entries touching the path), `originals_kept` and `sources` (R-9, data-model).
After a purge a purged entry is never the winner or in `inputs` or `shadowed`.

## Rebuild

`rebuild(doc=None) -> RebuildReport`; the report gains `layers_differing` and `layers_refreshed` (lists of `(doc, tier)`);
`identical` is false if a view or a layer differed or a document failed. It writes saved items only, never the ledger.

## Compaction

```python
db.compact(doc, *, through_seq: int | None = None, older_than: timedelta | None = None, verify: bool = True) -> CompactionReport
db.verify_archive(doc) -> ArchiveReport
db.expire_archive(doc, *, through_seq: int) -> ExpiryReport
```

- Exactly one of `through_seq`, `older_than` (relative to the service clock: entries with `tx_time` older than that); the
  watermark is moved down to a request boundary. Nothing in range: `CompactionReport(noop=True)` and nothing changes.
- Refusals (nothing changed): `MIXED_SCHEMA_RANGE`, `IDEM_KEY_MISSING` (names the field), `COMPACTION_CHECK_FAILED` (first
  differences named), `LEDGER_CHANGED` (the ledger up to the watermark changed during the check: try again).
- `verify=False` is not accepted by the CLI; the library keeps it for tests that inject a wrong plan and must see the check refuse
  it when `True`.
- `verify_archive`: derives from the originals and from the compacted ledger, compares document, errors, skipped, conflicts,
  status; reports differences; changes nothing. A document with no kept archive reports `checked=False`.
- `expire_archive`: deletes archived originals of compactions with watermark ≤ `through_seq`, records `archive_expiry`, keeps
  `compaction_sources`, the request index and retired idem keys. It does not touch the effect log, derived store or audit log.
- A retry of a compacted request returns its original `(id, seq)` list. A reused `idem_key` of a compacted entry is still
  `DUPLICATE_IDEM_KEY`. Reading `list_entries` shows synthetic entries (`synthetic` set) for the compacted range.
- Activating or pinning a schema version that folds differently (different `fold_fingerprint`) for a document with compacted
  history whose archive was expired: `HISTORY_COMPACTED`; while the archive is kept, the read derives from the originals.

## Purge

```python
@dataclass(frozen=True)
class PurgeScope:
    path: str | None = None        # every contribution at or below the path (and the part of a struct above it)
    tier: str | None = None        # with path: only that tier
    entries: tuple[str, ...] = ()  # or listed entry ids

db.purge(doc, scope: PurgeScope, *, reason: str, caller: Identity) -> PurgeReport
db.purge_log(doc) -> list[PurgeRecord]
```

- `caller` must hold a tier or role in the schema's `purge_by`, else `NOT_AUTHORIZED`; an empty `reason`: `PURGE_REASON_REQUIRED`;
  a scope with both or neither of `path` and `entries`: `ValueError`; an unknown entry id: `UNKNOWN_ENTRY`; a path the schema does
  not declare: `UNKNOWN_PATH`; a path that is derived (computed or deferred): `FIELD_NOT_WRITABLE` (nothing contributed there).
- Atomic. After it: the erased values are in no store (ledger, archive, synthetic layers, saved layers, views, effect log inputs
  and outputs of downstream fields, derived store rows of downstream fields, rejection audit values, in-memory caches); the
  document equals a derivation without the erased contributions; `PurgeReport` counts per store; one `purge_log` record exists.
- A retry of a request whose hash was erased is a duplicate and returns its original answer.
- No log line, error, reason or purge-log field contains a value.

## Guarantees

- Saved layers never change any result: with `layers=False` (a service option for tests and conformance) every result of every
  operation is identical.
- A rejected request leaves ledger, layers, views and errors unchanged.
- Compaction, expiry and purge are single transactions: after a failure at any step the ledger and every store are as before.
- Time travel, `rebuild` and `verify_archive` call no hook and no source.
