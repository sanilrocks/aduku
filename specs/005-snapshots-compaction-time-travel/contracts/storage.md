# Contract: Storage interface and saved formats

Added to the `Tx` protocol (both backends; each method runs inside the caller's transaction). Existing methods keep their meaning.

```python
# layers
get_layers(doc) -> list[LayerRow]                       # LayerRow(doc, tier, tier_seq, schema_version, rules_fingerprint, epoch, state)
put_layers(rows: Sequence[LayerRow]) -> None            # replaces a row only for an equal or higher tier_seq of the same epoch
drop_layers(doc) -> None
tier_last_seq(doc, tier, through: int) -> int           # highest seq of the tier at or below `through`, 0 if none
epoch(doc) -> int
bump_epoch(doc) -> int

# time
entries(doc, since_seq=0, limit=None, *, until_seq=None) -> list[Entry]      # + until_seq
seq_at_tx_time(doc, moment) -> int                      # highest seq (entries and archive) with tx_time <= moment, 0 if none
archived_entries(doc, *, through_seq=None) -> list[Entry]

# compaction
apply_compaction(plan: CompactionPlan) -> None          # one atomic step: archive, remove, insert synthetic, record, bump epoch
compactions(doc) -> list[CompactionRecord]
sources_of(compaction_id) -> list[SourceRow]
request_entries(request_id) -> list[tuple[str, int]]    # (entry_id, seq) from entries, else from request_entries
expire_archive(doc, through_seq, at) -> int
idem_key_seen(doc, key) -> bool                         # also checks retired_idem_keys

# purge
apply_purge(application: PurgeApplication) -> (int, int)  # one atomic step across entries, archive, layers, views, request
                                                        # hashes, effect log and derived store; returns (effects, derived rows) deleted
append_purge_log(record: PurgeRecord) -> None           # called in the same transaction
purge_log(doc) -> list[PurgeRecord]
request_effects(request_id) -> (added, resolved) | None # the model errors a compacted request added and resolved
# the rejection audit backend gains update_attempted(row_id, attempted), used to scrub values in the same transaction
```

Rules:
- `apply_compaction`, `expire_archive` and `apply_purge` are the only methods that update or delete entries, archive rows, effect
  records for a purge, and they lift the SQLite triggers only for their own statements (`ledger_rewrite` flag set and cleared
  inside the transaction). A crash leaves the flag cleared (it is part of the transaction).
- `purge_log` rows and `compaction_sources` rows can only be inserted.
- The in-memory backend swaps its structures after all steps succeed.

## Saved formats

- Layer text: canonical Ion list of `[path_text, state]` sorted by `path_text`; a tombstone is `deleted::null`; a reset value is
  `recreated::<value>`. Two ports derive byte-identical layer text for the same ledger and schema version.
- Compaction base errors: canonical Ion list of structs `{rule, scope, message, request, schema_version}` sorted by `(rule, scope)`.
- Synthetic entry value: the layer text as an Ion string.
