# Data Model: Snapshots, Time Travel, Compaction and Purge

Everything below is stored text that is canonical (SPEC 3.1) and byte-comparable, in both backends. Types are shown in SQLite
terms; the in-memory backend holds the same fields.

## Changed: `Entry` / `entries`

| Field | Type | Meaning |
|---|---|---|
| `valid_time` | timestamp, nullable | New. When the fact became true; set by the contributor; read only by `as_of` valid reads; erased by a purge. |
| `op` | `set` \| `delete` \| `layer` | `layer` is new and only written by compaction (R-7). |
| `synthetic` | text, nullable | New. The compaction id of a synthetic entry; null for a contributed entry. |
| `purged` | bool | New. True for a purged placeholder: `value`, `meta`, `valid_time`, `request_hash` are null; id, seq, tier, path, op, contributor, `tx_time`, `request_id`, `schema_version`, `lenient`, `idem_key` stay. |

`EntryInput.valid_time` (optional, timezone-aware). A purged entry has no effect on any derivation (`fold_entries`, `group_requests`
and `explain` skip it). The `entries` triggers allow update and delete only while `ledger_rewrite.active = 1`, inside a
compaction, an archive restore or a purge.

## New: saved layers

`layers(doc, tier, tier_seq, schema_version, rules_fingerprint, epoch, state)`; primary key `(doc, tier)`. `state` is the
canonical text of a layer (R-2). `tier_seq`: highest seq of that tier covered. Fit rules: R-3.

`views` gains `epoch` (integer, default 0). `doc_epoch(doc, epoch)`: bumped by compaction and purge (not by an expiry, which
changes no derivation); a layer or view with another epoch is not used, and neither is a state kept in memory.

## New: compaction

- `compactions(id, doc, watermark_seq, schema_version, fold_fingerprint, created, policy, base_errors, base_skipped, archived,
  first_tx_time, last_tx_time, has_valid_time)`:
  `policy` is the canonical text of the request (`through_seq` or `older_than`); `base_errors` is the canonical text of the
  attributed model errors at the watermark; `base_skipped` the skipped seqs; `archived` is true while the originals are kept;
  `first_tx_time` and `last_tx_time` are the earliest and latest recording times of the replaced entries and `has_valid_time`
  is true if any of them carried a `valid_time` (they decide what an expired range can still answer, R-11).
- `compaction_sources(compaction_id, source_id, seq, tier, idem_key)`: one row per replaced entry, kept after expiry.
- `archive_entries`: the columns of `entries`, plus `compaction_id`; removed only by an expiry.
- `request_entries(request_id, entry_id, seq)`: for the requests that lost entries; the answer to a retry reads it when the entry
  rows are gone.
- `retired_idem_keys(doc, idem_key)`: primary key; checked by `idem_key_seen` together with the live entries.
- `archive_expiry(id, doc, through_seq, at, expired)`: the recorded expiry and how many archived entries it removed.
- `request_effects(request_id, added, resolved)`: for each request a compaction replaced, the model errors it added and the ones it
  resolved (canonical text, written from the replay that computed the base). A retry of that request is answered from it, since the
  entries a replay would find them in are gone (FR-026). Kept after expiry.

A synthetic entry: `op = layer`, `path = "/"`, `value` = layer text (a string), `contributor = "aduku:compaction"`,
`request_id = synthetic = <compaction id>`, `seq` = highest source seq of its tier, `tx_time` = highest source tx_time, one per
tier that had entries in the range.

State: *live* (no compaction) → *compacted, archive kept* → *compacted, archive expired* (per compaction; an expiry through
seq N expires every compaction whose watermark ≤ N). A compaction of a compacted document takes the earlier synthetic entries
as sources like any entry; its archive holds them and the originals stay with the first compaction, so `compaction_sources` is a
tree and expanding a synthetic entry to its originals is recursive through the archives below it (R-8). Synthetic sources count
as keyed for the idempotency-key check.

## New: purge

`purge_log(id, doc, scope, reason, actor, at, counts)`. Insert only (trigger); never read by a derivation; no table and no
operation of this feature can erase a row. `scope` is the canonical text of the path, tier or entry ids. `counts` is a canonical
struct: `entries_erased`, `entries_rewritten`, `archive_rows`, `effects_erased`, `derived_erased`, `audit_scrubbed`,
`expired_sources` and `ids` (the ids of the entries made into placeholders, which `explain` uses to say when and why an entry was
erased). No value appears in any column.

## Changed: `Explanation`

`purged: tuple[PurgedMark, ...]` with `PurgedMark(entry_id, seq, purged_at, reason)`; `originals_kept: bool | None` (None
unless a synthetic entry took part: then true if answered from the archive, false after expiry); `sources: tuple[str, ...]` for
a synthetic winner/input.

## New: results

- `AsOf(seq=None, tx_time=None, valid_time=None)`: exactly one set.
- `AsOfInfo(kind, point, resolved_seq, included, inside_request)`; `DocumentResult.as_of: AsOfInfo | None`.
- `RebuildReport` gains `layers_differing: tuple[tuple[str, str], ...]`, `layers_refreshed: tuple[tuple[str, str], ...]`.
- `CompactionReport(compaction_id, doc, watermark_seq, replaced, synthetic, checked_paths, noop)`.
- `ArchiveReport(doc, checked, differences)`; `ExpiryReport(doc, through_seq, expired)`.
- `PurgeReport(purge_id, doc, entries_erased, entries_rewritten, archive_erased, effects_erased, derived_erased,
  audit_scrubbed, expired_sources)`.

## Schema

Top-level `purge_by: [name, ...]` (tiers or declared roles); absent or empty means nobody. `Schema.purge_by` and
`Schema.fold_fingerprint` (R-10) are new; `rules_fingerprint` is unchanged except that it now includes `purge_by`.

## Relationships and invariants

- A document's effective entries are: live entries (contributed and synthetic) plus, when needed, the archive (R-10, R-11).
- For every document: derive(effective entries with bases) = derive(originals) in document, errors, skipped, conflicts, status.
- Saved layers + view = a derivation state at the view's watermark, or they are not used.
- Purge: derive(ledger after) = derive(ledger without the erased contributions); no store holds an erased value.
