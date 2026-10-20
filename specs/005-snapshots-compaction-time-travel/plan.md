# Implementation Plan: Snapshots, Time Travel, Compaction and Purge

**Branch**: `005-snapshots-compaction-time-travel` | **Date**: 2026-10-20 | **Spec**: [spec.md](spec.md)

**Input**: Feature specification from `/specs/005-snapshots-compaction-time-travel/spec.md`

## Summary

Extend `aduku-py` so that reads and writes no longer replay the whole ledger, history can be read at a point, old history can be
compacted, and data can be erased for legal reasons, without changing any result of Features 001 to 004.

Technical approach, in four layers that can ship in order:

1. **Saved layers.** The per-tier layers the engine already folds in memory (`DerivationState.layers`) are saved, one row per
   document and tier, in the append's own transaction (a failure to write them is ignored, because they are a cache). Together with the view row (which already holds
   the attributed model errors and the notes) they are a complete saved derivation state at the view's watermark. A service
   that starts cold, or a process that was not the writer, rebuilds its state from them and folds only newer entries. Reads
   write nothing. `rebuild` compares layers as well as views.
2. **Time travel.** `valid_time` becomes an optional field of an entry (outside ordering, merging and the normal request hash).
   `get(..., as_of=...)` and `explain(..., as_of=...)` derive from a prefix of the ledger (seq, or a `tx_time` resolved to a
   seq) or from the ledger filtered by `valid_time`, with the engine's own replay, no writes and no calls.
3. **Compaction.** A range of entries up to a request boundary is replaced by one synthetic `layer` entry per tier (the tier's
   folded layer as the entry's value), the originals go to an archive table, and a compaction record keeps what a replay of
   requests would have computed on the way (attributed model errors, skipped seqs). Verification runs on the compacted ledger in
   memory before anything is written.
4. **Purge.** Erased entries stay as value-less placeholders; every store that holds a copy or a derivation of the erased
   values is cleaned in one transaction, and a purge log records the act without values.

The engine modules stay pure; the new edge work (saving, archive, purge, locks) sits in the service and the storage backends.

## Technical Context

**Language/Version**: Python 3.12 (as Features 001 to 004)

**Primary Dependencies**: none new at runtime (`amazon.ion` as before). Dev only: unchanged.

**Storage**: SQLite (WAL) and the in-memory backend behind the one storage interface. New tables: `layers`, `compactions`,
`compaction_sources`, `archive_entries`, `retired_idem_keys`, `request_entries`, `purge_log`, `doc_epoch`; new columns on `entries`
(`valid_time`, `synthetic`, `purged`). Details in [data-model.md](data-model.md).

**Testing**: `pytest` with `hypothesis`; test-first (constitution X). New: unit tests for the layer codec, the as-of resolution
and the compaction planner; property tests that a read through saved layers equals a ledger replay on random histories, that
`as_of` N equals the live document at N, that compaction changes nothing observable, that purge equals derivation without the
erased entries and leaves no copy; integration tests on both backends (including 100,000-entry documents and crash
injection in each operation); shared conformance vectors for `valid_time`, `as_of`, compaction and purge; the 16.1 and 16.2
vectors and all earlier vectors unchanged.

**Target Platform**: Linux/macOS/Windows, same as Feature 001

**Project Type**: library with a thin CLI (extends `ports/py/`)

**Performance Goals**: SC-001: the first write after a restart (which today must fold the whole ledger to get its state), the read
after it, and the first read after a restart, on a 100,000-entry document, each within 5× the same step on a 100-entry document. Compaction of 10,000 entries on 20 fields verifies in seconds, not minutes (explain verification is
quadratic per path, see research R-9).

**Constraints**: `engine/`, `schema/`, `behaviors/` stay pure (no clock, no IO); saved layers are disposable and never change a
result (FR-004, FR-005); a time-travel read writes and calls nothing; compaction and purge are single transactions and exclude
only the commit moment of writes; no value appears in logs, errors or the purge log.

**Scale/Scope**: documents of up to a few hundred thousand entries and tens of thousands of paths per tier layer; purge scopes of
one path or a list of entries.

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

| Principle | Status | How the plan complies |
|---|---|---|
| I. Ledger Is Truth | **Pass after amendment** | Entries stay append-only for every contribution. Compaction and purge are the only operations that change existing entries; both are single recorded transactions, compaction proves the result equal before it commits, and purge never changes what remains. Principle I (and SPEC 4.2 item 1) is amended to name these two exceptions (constitution 1.5.0, task in tasks.md). Rejected requests still record nothing. |
| II. Schema Is Source of Truth | Pass | Who may purge is declared in the schema (`purge_by`), like `invalidate_by`; no semantics live only in the service. |
| III. Determinism and Purity | Pass | Fold, replay, as-of derivation, compaction planning and verification are pure functions of (entries, schema, effect log, compaction bases); wall-clock enters only at the service edge (purge time, expiry time). Saved layers are canonical and byte-comparable. |
| IV. IO Is Confined | Pass | No new IO in the engine. A time-travel read and `rebuild` call no hook and no source (tested with counting doubles). |
| V. Algebraic Laws (NON-NEGOTIABLE) | Pass | Compaction is correct because overlay is associative (squash equivalence). A property test checks `fold(originals) == fold(synthetic layers)` for every built-in and custom merge behavior; no merge rule changes. |
| VI. Derived Fields Never Writable | Pass | `valid_time` and the new `layer` op cannot be submitted by contributors; purge targets contributed paths only. |
| VII. Explainability | Pass | `explain` is answered from the originals while archived and by the synthetic entry after expiry, and reports purged entries with date and reason. |
| VIII. Sandboxed Behaviors | Pass | No behavior sees more; time travel uses recorded outcomes only. |
| IX. Immutable Schema Versions | Pass | Layers record the version and rules fingerprint they were made under; a version that folds differently on a compacted document is derived from the archive or refused, never guessed. |
| X. Test-First and Conformance (NON-NEGOTIABLE) | Pass | Failing tests and vectors first; replay test (SPEC 16 item 6) extended to layers; 16.1/16.2 unchanged. |
| XI. Simplicity | Pass, justified | Crypto-shredding, automatic expiry, bitemporal combination and cross-document purge stay out. New tables are justified in Complexity Tracking. |

Scope and technology constraints: canonical text and stable key ordering are used for every saved item; new schema syntax
(`purge_by`), codes and vectors are in `contracts/`. Re-check after design: no violation.

## Project Structure

### Documentation (this feature)

```text
specs/005-snapshots-compaction-time-travel/
├── plan.md              # This file
├── research.md          # Phase 0 output
├── data-model.md        # Phase 1 output
├── quickstart.md        # Phase 1 output
├── contracts/
│   ├── schema-format.md         # delta: purge_by
│   ├── library-api.md           # delta: valid_time, as_of, rebuild report, compact, archive, purge, errors
│   ├── storage.md               # the storage interface additions and the saved formats
│   ├── cli.md                   # delta: get/explain --as-of, compact, archive, purge, entries
│   └── conformance-vectors.md   # delta: new vectors and runner directives
├── checklists/
└── tasks.md             # Phase 2 output (/speckit-tasks; not created here)
```

### Source Code (repository root)

```text
conformance/
├── time-travel.ion          # new: as_of seq / tx_time / valid_time, deferred and impure at a point, ambiguity, bad valid_time
├── compaction.ion           # new: compact then compare, idem keys, retries, tombstones, as_of inside a range, refusals
├── purge.ion                # new: purge scopes, authority, explain marks, retries, stores free of the marker
├── retries.ion              # unchanged (request_hash without valid_time is byte-identical)
└── README.md                # new directives documented

ports/py/src/aduku/
├── engine/
│   ├── layer_codec.py       # NEW, pure: canonical text of a layer (paths, values, tombstones, reset marks) and back
│   ├── asof.py              # NEW, pure: choose the entries of a point (prefix by seq, filter by valid_time) and describe it
│   ├── compaction.py        # NEW, pure: plan (which entries, request boundary, one layer per tier), synthetic entries,
│   │                        #            the compacted ledger, comparison of two derivations, the id mapping for explain
│   ├── purge.py             # NEW, pure: which entries a scope matches, the value rewrite for structs above, downstream fields
│   ├── squash.py            # + the `layer` op: entry_layer() of a synthetic entry is its decoded layer
│   ├── derive.py            # + DerivationState from saved layers; fold_entries skips purged entries
│   ├── model_errors.py      # + replay() takes compaction bases (attributed errors, skipped) after a synthetic group
│   ├── explain.py           # + purged marks; synthetic entries as single entries; id mapping for archived expansion
│   └── request_hash.py      # + valid_time only when given
├── storage/
│   ├── base.py              # + Tx methods (layers, epoch, archive, compactions, purge log, request index, retired keys)
│   ├── memory.py            # same, in memory
│   └── sqlite.py            # tables, triggers with a controlled bypass for the two rewriting operations
├── service.py               # + saved state in _load/append/rebuild; get/explain as_of; compact, verify_archive,
│                            #   expire_archive, purge, purge_log
├── model.py                 # + Entry.valid_time/synthetic/purged; EntryInput.valid_time; AsOf; reports; Explanation.purged
├── schema/model.py, loader.py  # + purge_by, Schema.fold_fingerprint
├── errors.py                # + codes (see contracts/library-api.md)
└── cli.py                   # + --as-of, compact, archive, purge

ports/py/tests/
├── unit/                    # layer codec, as-of selection, compaction planner and comparison, purge matching
├── property/                # layers == replay; as_of == live history; compaction invariance; purge == derivation without
├── integration/             # both backends: snapshots, restart, rebuild repair, as_of, compaction + concurrent writes,
│                            #   expiry, purge across stores, crash injection, 100,000-entry performance, CLI
└── golden/                  # 16.1 / 16.2 unchanged; the new vectors
```

**Structure Decision**: extend the single library under `ports/py/`. Pure logic goes in new `engine/` modules; the only impure
additions are in `service.py` and the storage backends. The four layers ship as four phases of tasks so each is releasable.

## Complexity Tracking

| Choice | Why Needed | Simpler Alternative Rejected Because |
|---|---|---|
| A table of saved layers beside the existing view row (R-1) | A cold process must rebuild its state without replaying; the view row has errors but not the layers | Storing the layers inside the view row would rewrite every tier on every append, defeating incremental invalidation |
| A `layer` op for synthetic entries (R-7) | One entry per tier must carry tombstones, reset marks and many paths | One `set` entry per path cannot express tombstones/reset marks and does not shrink the ledger enough |
| Compaction base records (R-8) | Model-error attribution is computed by walking requests; after compaction the walk must start from what it had computed | Re-attributing everything to the compaction would change `introduced_by_request` (violates FR-022) |
| `fold_fingerprint` on a schema (R-10) | To know whether a new version may reuse synthetic entries | Always requiring the archive would make every validation-only schema change depend on it |
| A retained request index and retired idem keys (R-8) | Retries and duplicate-key checks must survive compaction and expiry | Reading them from entries/archive would break when originals are expired |
| Principle X deviation (recorded, as in Feature 1): for the tasks T020/T021, T039/T040, T047 to T051, T056/T057 and T060 to T062 the code was written before, or in the same step as, its tests, so those tests were not observed failing first | The work was done as one session across storage, engine and service where the test and the code shape each other | Re-writing the tests after deleting the code was judged less informative than a **mutation check**: twelve deliberate breaks of the code (fold ignores the purged flag, layers accepted across epochs, `rebuild` comparing no layers, expired points answered, compaction check disabled, idempotency keys not carried, watermark inside a request, `valid_time` always in the hash, request hash kept by a purge, `valid_time` filter ignored, model-error bases not applied, `tx_time` resolved by a search) were each run against the Feature 5 tests; eleven were caught at once and the twelfth (epoch) by a test added for it (`test_a_layer_of_an_older_epoch_is_never_used_even_if_it_otherwise_fits`) |
