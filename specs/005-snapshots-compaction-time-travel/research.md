# Research: Snapshots, Time Travel, Compaction and Purge

Spec clarifications (2026-10-20) are taken as given: entries with no `valid_time` always take part in a valid-time read; purge is
physical removal only; a purge of a path whose originals were expired erases the whole compacted contribution there; an
`as_of` read takes exactly one point; archive expiry is an explicit recorded act.

What the code does today, which these decisions build on: `fold_entries` already continues from a `DerivationState` (per-tier
layers, watermark, skipped seqs, attributed errors, conflicts, effects_seq); `append` writes a `ViewRow` (document text, errors,
notes, watermark, schema version, rules fingerprint, effects_seq) in the commit transaction; a cold process has no state and
replays the ledger (`_load` returns `None`); `replay` walks requests in order, each under the schema version recorded on it, to
attribute model errors; `explain` is worked out from the whole ledger; `entries` has triggers that forbid update and delete (and
`effects` has a pruning flag that lifts them, the pattern reused here).

## R-1. Saved layers: one row per document and tier, next to the view row

- **Decision**: a table `layers(doc, tier, tier_seq, schema_version, rules_fingerprint, epoch, state)`. `state` is the tier's
  folded layer as canonical text (R-2). `tier_seq` is the highest seq of that tier the layer covers. The view row keeps the
  errors, skipped and conflicts and gains an `epoch` column (R-12). The view is written in the append's commit transaction as
  today, and the layer rows of the tiers the request touched are written in that same transaction (a failure to write them is
  caught and ignored, so it never fails the request), and by `rebuild`, compaction and purge. A layer is only replaced by one with an equal or higher `tier_seq` of the
  same epoch, so a slow writer cannot regress a newer one. The saved state at a document's view watermark W is: the
  layers plus the view row. A reader reconstructs `DerivationState` from them (schema_ref set to the live schema when the
  fingerprints agree) and folds the entries after W.
- **Invariant**: at every commit, `view.watermark = last seq`, and for each tier `layer.tier_seq` = the highest seq of that tier
  at or below the watermark. A tier with no saved row has no entries up to the watermark (or the document has no layers yet).
- **Rationale**: this makes a write at one tier rewrite one row (incremental invalidation, FR-003), a cold start load a few rows
  (FR-007), and reuses the view row's attributed errors instead of a second copy. A tier's layer grows with the document, not
  with the ledger.
- **Alternatives considered**: all layers in the view row (rewrites every tier per append); a whole-state blob (same);
  per-path rows (many rows, no gain: the fold needs the whole layer).

## R-2. The layer codec

- **Decision**: `engine/layer_codec.py`, pure. A layer maps a path to a fold state: a value, `TOMBSTONE`, or `Recreated(value)`.
  The text is the canonical Ion of a list of `[path_text, state]` pairs sorted by path text, where a tombstone is `deleted::null`
  and a reset value is annotated `recreated::value` (the annotation name is reserved; contributed values cannot carry it, as
  they are normalized on entry). Decoding checks every path against the schema only through the fold that uses it. The codec has
  a round-trip property test over random layers, and the text is byte-comparable, which is what `rebuild` compares.
- **Rationale**: SPEC 3.1 requires deterministic canonical text; ports must produce the same bytes. Sorting by path text, not by
  the in-memory tuple order, keeps it language-neutral.
- **Alternatives considered**: pickling the Python objects (not portable, unsafe); JSON (loses Ion types such as decimals and
  timestamps).

## R-3. When a saved layer is used, and why losing them is safe

- **Decision**: a state is built from saved layers only if: the view row fits (`_fits` as today, including `effects_seq`); every
  layer has the view's `schema_version`, `rules_fingerprint` and the document's `epoch` (R-8, R-12); and every tier with entries
  at or below the watermark has a row whose `tier_seq` matches (one indexed query per tier). If any check fails the saved state is
  ignored and the state comes from the ledger as today (a replay), then saved again by the next writer. A layer
  that could not be written (or an older process's) leaves the view newer than it: that is a mismatch above and costs a
  replay, never a wrong answer. A failure to save a layer is caught, logged and ignored, because the layers are a cache
  (FR-005); the next writer saves it.
- **Rationale**: correctness never depends on a layer being present or right; `rebuild` is the proof (R-5). The `epoch` guards
  against a layer that survived a restore from a backup that predates a compaction or purge.
- **Alternatives considered**: trusting `watermark` alone (cannot detect a layer made before a purge); a checksum of the
  covered ledger (cost proportional to the ledger).

## R-4. Who writes layers; documents from before this feature

- **Decision**: `append` writes the touched tiers' rows in its commit transaction, using the folded state it already has in
  hand. The first append of a document that has no layers writes all its tiers (the state is already complete in memory after
  the warm-up fold). `rebuild` writes all of them. A read never writes (the existing rule: a read takes no write lock).
  `rebuild all` is the documented migration for an existing database; without it a document upgrades at its next append.
- **Rationale**: no new write path for reads; no big migration step; the one-time replay is the cost already paid today.
- **Alternatives considered**: write on first read (breaks "a read never takes the write lock"); a background job (new moving part).

## R-5. `rebuild` compares layers as well as views

- **Decision**: `rebuild` already derives from the ledger alone. It now also encodes the derived layers (R-2) and compares each
  tier's saved text, `tier_seq`, version and fingerprint with them. `RebuildReport` gains `layers_differing:
  tuple[tuple[str, str], ...]` of `(doc, tier)` and `layers_refreshed` (missing or other version). A difference is repaired in
  the same way as a view (only if nothing was appended meanwhile). `identical` is false if any view or layer differs. A stale
  layer from another epoch counts as refreshed, not as a difference. It makes no hook or source call (unchanged).
- **Rationale**: SPEC 16 item 6 says replay reproduces cached snapshots bit for bit; this is that statement for layers.

## R-6. `valid_time` on an entry

- **Decision**: `EntryInput.valid_time: datetime | None` and `Entry.valid_time`, stored in a nullable column and in the Ion text
  as an Ion timestamp. It must be a timezone-aware datetime (a naive one is `BAD_VALID_TIME`, as is anything that is not a
  timestamp); it is checked in admission with the other schema-level checks (stage V0), so a bad value rejects the whole request
  and records nothing. It is never read by ordering, merging, validation, defaults or computed fields. `request_hash` adds a
  `valid_time` key to an entry's canonical struct only when it is not null, so the hash of every request without it is
  byte-identical to Feature 1's (FR-012; a test pins hashes of the existing vectors).
- **Rationale**: keeps retries of old requests matching and keeps the entry's meaning of "informational" extended by exactly one
  field that only `as_of` reads.
- **Alternatives considered**: `valid_time` inside `meta` (untyped, no validation, cannot be filtered in storage); a separate
  validity interval `[from, to)` (SPEC has one point; intervals are future work).

## R-7. Synthetic entries use a `layer` op

- **Decision**: a compaction writes one entry per tier with `op = "layer"`, `path = "/"`, `value` = the tier's folded layer text
  from R-2 as a string, `contributor = "aduku:compaction"`, `request_id` = the compaction id, `seq` = the highest seq among its
  sources of that tier, `tx_time` = the highest tx_time among its sources, and `synthetic` = the compaction id. `entry_layer()`
  returns the decoded layer for this op, so `fold_entries`, replay and the squash equivalence need no other change. A
  contributor cannot submit `layer` (admission accepts only `set` and `delete`). The layer is the fold of the tier's sources
  under the schema version they were written under, so a replay that folds it gives the same tier layer by associativity.
- **Rationale**: one entry per tier is the smallest ledger; tombstones and reset marks survive because they are part of the layer.
  This satisfies "one synthetic entry per tier and group of paths" with a single group per tier.
- **Alternatives considered**: one entry per path (no shrink for wide documents, loses reset marks); keeping the layer only in
  `layers` (the ledger would then not be self-contained, breaking replay).

## R-8. What compaction records so nothing observable changes

- **Decision**: a compaction is one transaction that writes:
  - `compactions(id, doc, watermark_seq, schema_version, fold_fingerprint, created, policy, base_errors, base_skipped,
    epoch)`: the attributed model errors and the skipped seqs the original request walk had at the watermark (computed by the
    verification pass, R-9). `replay` takes a map `request_id -> base`; after folding a synthetic group it seeds `errors` and
    `skipped` from the base instead of attributing everything to the compaction (so `introduced_by_request` is unchanged).
  - `compaction_sources(compaction_id, source_id, seq, tier, idem_key)`: kept forever, even after expiry.
  - `archive_entries`: full copies of the originals (every `entries` column) with the compaction id.
  - `request_entries(request_id, entry_id, seq)`: for every request that had an entry in the range, so a retry still returns its
    original `(id, seq)` list (FR-026); `requests` rows are untouched.
  - `request_effects(request_id, added, resolved)`: the model errors each such request added and resolved, taken from the replay
    that computed the base (`replay(..., trail=)`), so a retry's answer keeps its `model_errors_added` and `_resolved`.
  - `retired_idem_keys(doc, idem_key)`: every key of the sources, so `DUPLICATE_IDEM_KEY` still fires; `idem_key_seen` checks both.
  - the synthetic entries (R-7) in place of the originals, and a new `epoch` for the document.
  The effect log, derived store, rejection audit log, layers of newer entries and the view of the document are not changed;
  the layers and the view are rewritten with the new epoch because a replay of the compacted ledger must give the same bytes.
  The compaction watermark is the policy's seq or the cutoff age's last seq, moved down to the end of a request, and only a range
  written under one schema version is allowed (`MIXED_SCHEMA_RANGE`). A non-idempotent field's entries must all carry an
  `idem_key`, else `IDEM_KEY_MISSING` (the refusal of FR-024). A synthetic entry from an earlier compaction counts as keyed:
  its sources were checked then and their keys are in `retired_idem_keys`.
  A compaction of a compacted document takes the earlier synthetic entries as sources like any other entry. Its archive holds
  them, and the originals stay with the first compaction; `compaction_sources` therefore forms a tree, and every place that needs
  originals (restoring the effective ledger, `verify_archive`, `explain`'s `sources`, purge) expands a synthetic entry
  recursively through the archives of the compactions below it. If any archive in that chain was expired, the expansion is
  incomplete and the rules of R-10 and R-11 for an expired archive apply.
  The compaction record also keeps `first_tx_time`, `last_tx_time` (the earliest and latest recording times of the range) and
  `has_valid_time` (true if any source carried a `valid_time`), which decide what an expired range can still answer (R-11).
- **Rationale**: every observable thing a replay computes from requests is either carried (errors, skipped) or independent of the
  request boundaries (document, conflicts, status).
- **Alternatives considered**: not letting compaction cross a request with model-error introduction (too restrictive); recomputing
  errors without attribution (changes `introduced_by_request`).

## R-9. Verifying a compaction, and what `explain` can promise

- **Decision**: the planner (pure, `engine/compaction.py`) builds the compacted ledger in memory: the synthetic entries plus the
  entries after the watermark. The service derives, under the same schema and the effect view of the moment, (a) from the
  original entries and (b) from the compacted ledger with the bases, and compares: document text, attributed errors, skipped,
  conflicts, status, and for every path that any entry in the range touches the explain answer, with this comparison: winner
  maps to winner (an original id maps to its synthetic entry through `compaction_sources`); every id in the original `inputs`
  maps into the compacted `inputs`; every compacted input has an original input among its sources (a synthetic entry is an input
  iff some source was; shadowed iff none was). Any difference refuses with the first few named (`COMPACTION_CHECK_FAILED`), and
  nothing is written. The check is allowed to refuse a case that it cannot prove equal (for example a synthetic entry that
  would be an input although no single source was).
  While the archive keeps the originals `explain` is computed over the original entries (the archive is merged back into the
  entry list in memory), so it is exact by construction; after expiry it uses the synthetic entries as single entries.
- **Cost**: `explain` is quadratic in the entries touching a path (existing). The check runs it for each touched path; for the
  10,000-entry, 20-field case that is about 500 entries per path and measured about 30 seconds (a 1,000-entry document with 20
  fields takes 2 to 3 seconds). Incremental compaction (compact the newest range, earlier ranges are already synthetic units)
  keeps later runs cheap; the library's `verify=False` skips the comparison for a caller that has proved it another way.
  Known refusal: a delete and a re-creation in the range that leave a value where a lower tier already had it make the compacted
  ledger name another winner than the originals did (`explain` reports "the entry after which the value last changed"); the check
  refuses it, over `explain` only and never over the document or its errors, which is the conservative side of FR-023.
- **Rationale**: the check exercises exactly the code that will answer reads after expiry, and the archive path is exact because
  it is the old computation.
- **Alternatives considered**: storing per-path roles on the synthetic entry (shown to be wrong in general: whether an entry is
  essential depends on the entries after the range, as a union rule with a later duplicate value demonstrates); skipping the
  explain check (FR-023 requires it).

## R-10. Schema versions on a document with compacted history

- **Decision**: `Schema.fold_fingerprint` is a digest of what decides how entries fold into layers: tiers and their order, each
  field pattern with its type, merge behavior and version, field-wise flag, `on_delete`, and which fields are derived. It is a
  sub-part of `rules_fingerprint` and is recorded in each compaction. The effective ledger of a document at a read:
  - if no compaction exists: the entries;
  - if every compaction's `fold_fingerprint` equals the read schema's, or the read schema's version is the one the entries were
    written under: the compacted ledger (synthetic entries and bases);
  - otherwise the originals restored from the archive (merged by seq), when every compaction's archive is kept;
  - otherwise `HISTORY_COMPACTED` is raised by the read and by `schemas.pin`/`activate` for that document (it names the
    compaction), so a version change is refused up front and never produces a wrong document.
  A version that only changes validation or computed rules has the same fold fingerprint and reuses synthetic entries; the
  base errors of a compaction are then recomputed by the replay's version-change step from the synthetic layers, which is the
  same step a replay of the originals performs (`done` = the synthetic entries; the equality holds because the layers equal).
- **Rationale**: Feature 6 (schema evolution) can build on a clear rule; no guess about a changed merge rule.
- **Alternatives considered**: always requiring the archive after any version change (too strict); refusing every version change
  once compacted (too strict).

## R-11. `as_of` reads

- **Decision**: `AsOf(seq=None, tx_time=None, valid_time=None)` with exactly one set (`AS_OF_AMBIGUOUS` otherwise, also when none
  is set in a call that passes an `AsOf`). `engine/asof.py` selects entries:
  - `seq N`: entries with seq ≤ N (N above the last reads the current state and reports the seq it resolved to; N below 1 is the
    empty document);
  - `tx_time T`: N = highest seq whose `tx_time ≤ T` (one indexed query over entries and archive), then as `seq N`;
  - `valid_time T`: entries with `valid_time` null or ≤ T, in the usual order, and the count of entries that took part.
  The answer is `DocumentResult` plus `as_of: AsOfInfo(kind, point, resolved_seq, included, inside_request)`. The derivation is a
  `replay` of the selected entries under the document's current schema (FR-020), without writing a view, a layer or a derived
  value, and without the in-memory state caches. Impure computes use `EffectView` built from the recorded outcomes of the
  document (latest per input fingerprint); where none exists the field becomes the model error `NO_RECORDED_OUTCOME` and no hook
  is called. Deferred fields are listed with status `unavailable` and reason `history not kept`, never read from the derived
  store. The effective ledger is R-10's (the originals from the archive when the point lies inside a compacted range and the
  originals are kept). After expiry a point is answerable only if: its seq is at or above the compaction's watermark; its `tx_time`
  T is below `first_tx_time` or at or above `last_tx_time` of the compaction (a T between them cannot be resolved without the
  originals, because `tx_time` need not grow with seq); or it is a `valid_time` and `has_valid_time` is false (the synthetic
  entries carry no `valid_time`, so none can be filtered). Anything else is `HISTORY_COMPACTED` with `earliest_seq` = the highest
  watermark among the expired compactions that contain the point. `explain(..., as_of)` selects the same entries and calls the
  same explain.
- **Performance**: as-of is a replay of a prefix; no goal beyond correctness is set for it in this feature (not in SC-001).
- **Alternatives considered**: reusing the live saved layers when N is the last seq (a small optimization, left for later);
  answering deferred fields from the derived store (would show present values as past).

## R-12. Purge

- **Decision**: `purge(doc, scope, reason, caller)`; `scope` is `PurgeScope(path=..., tier=None)` or `PurgeScope(entries=[ids])`.
  Pure `engine/purge.py` decides, from the effective entries (live and archived), which entries are erased and what remains:
  - entries at or below the path: erased whole (value, meta, valid_time, request_hash set to null; `purged = 1`);
  - entries above the path whose struct value contains it: that part is removed from their value (the entry stays, not purged,
    but its request hash is erased too);
  - synthetic entries: re-folded from the remaining archived sources of their compaction; if those are expired, the layer's
    paths at or below the erased path are dropped from the layer (FR-036) and the purge log says `expired_sources`.
  The service then, in one transaction: rewrites the entries (the controlled trigger bypass, R-13), the archive and the
  synthetic layers; deletes the doc's saved layers and view and bumps the epoch (the next read or `rebuild` derives afresh,
  without hooks being called by the purge itself); deletes effect-log records and derived-store rows of the fields downstream of
  the erased paths through the dependency graph (`engine/graph.py`), for the document; scrubs rejection-audit values for the
  erased paths (`audit.redact`); clears `request_hash` on the `requests` rows of touched requests; drops the in-memory caches; and
  appends to `purge_log(id, doc, scope, reason, actor, at, counts)` (never a value). A retry whose stored hash is null is a
  duplicate and gets the original answer (no payload comparison). Idempotency keys stay (they are caller-chosen tokens, not
  values; the spec's placeholder list is extended by this one field, noted in the data model).
  Authority: the schema top-level `purge_by: [tier or role names]` (default empty = nobody), checked as `invalidate_by` is;
  `NOT_AUTHORIZED` otherwise; `reason` is required (`PURGE_REASON_REQUIRED`).
- **Explain**: `Explanation.purged` lists purged entries touching the path as `(entry_id, seq, purged_at, reason)`; a purged entry
  is never winner, input or shadowed.
- **Rationale**: erased entries having no effect makes the document equal a derivation without them, which the property test
  checks directly; scrubbing downstream fields removes every derived copy.
- **Alternatives considered**: leaving effect records and relying on the fingerprint change (the old output stays on disk);
  deleting the entries (breaks seq continuity and `expected_seq`); a deferred refetch in the purge (IO inside a destructive step).

## R-13. Atomicity, locking and the append-only triggers

- **Decision**: compaction, expiry and purge each run as one storage transaction on the document. The SQLite `entries` triggers
  gain the `effects_pruning` pattern: a one-row table `ledger_rewrite(active)` set to 1 inside the transaction (and to 0 before
  commit) lifts the update/delete trigger; the archive and purge log have their own triggers (archive: insert and the expiry
  delete only; purge log: insert only). The planner and verification run before the transaction on a snapshot at the watermark;
  inside the transaction the service re-checks that the ledger up to the watermark is the one verified (same entry ids digest
  and same epoch) and refuses with `LEDGER_CHANGED` otherwise; writes made meanwhile have seq above the watermark and are kept in
  order (FR-027). An error anywhere rolls the transaction back (injected-failure tests per step). The in-memory backend does the
  same with a copy-on-write swap.
- **Rationale**: the same safety pattern as effect pruning; no new locking concept.

## R-14. Interfaces

- **Decision**: library: `db.get(doc, as_of=AsOf(...))`, `db.explain(doc, path, as_of=...)`, `db.compact(doc, policy, *, verify=True)`,
  `db.verify_archive(doc)`, `db.expire_archive(doc, *, through_seq)`, `db.purge(doc, scope, *, reason, caller)`,
  `db.purge_log(doc)`. CLI: `get --as-of seq=N|tx=ISO|valid=ISO`, `explain --as-of`, `compact DOC --through-seq N | --older-than 90d`,
  `archive verify DOC`, `archive expire DOC --through-seq N`, `purge DOC --path P [--scope-tier T] | --entry ID... --reason TEXT
  --contributor C --tier T [--role R]` (the caller flags `recompute` uses; `--scope-tier` narrows the scope), `purge-log DOC`. `compact` and `expire_archive` take no caller (like `rebuild`
  and `prune_effects`: operator-only library calls); `purge` takes one because the spec makes the schema decide who may purge.
- **Rationale**: matches the style of the earlier admin operations.

## R-15. Conformance

- **Decision**: new vectors with new runner directives `as_of`, `compact`, `expire`, `purge`, `rebuild`, and result checks
  (`document`, `model_errors`, `explain`, `resolved_seq`, `absent_everywhere`). A vector may run the same steps with layers
  disabled (a service flag `layers=False`, SC-008) and compare. The existing 16.1/16.2 and Feature 2 to 4 vectors are run
  unchanged with layers on and off.
- **Rationale**: ports implement the same behavior by passing the same files.
