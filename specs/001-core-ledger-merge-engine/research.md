# Research: Core Ledger and Merge Engine

Resolves every open technical choice for the plan. No `NEEDS CLARIFICATION` remains. Items marked **Assumption** can
be changed without touching the spec.

## R-1. Where the Python code lives

- **Decision**: `ports/py/` inside this repo; split to an `aduku-py` repository when stable. Conformance vectors live in
  root `conformance/`.
- **Rationale**: spec, vectors and first implementation evolve together while the semantics settle; vectors at the root
  are the contract every port consumes.
- **Alternatives**: separate repo now (slows spec/vector changes, which constitution requires in the same change);
  root-level `src/` (blurs spec repo with one port).
- **Assumption**: taken from the proposed default in the handoff; change freely.

## R-2. Ion library and value model

- **Decision**: `amazon.ion` (`simpleion`, `IonPy*` types with `ion_annotations`). Internally, convert to immutable
  Python values (`Mapping`, `tuple`, `Decimal`, `str`, ...) plus a distinct `TOMBSTONE` sentinel for `deleted::null`;
  convert back only at the API boundary. Absent, null and tombstone stay three distinct states (SPEC 3.1).
- **Rationale**: the merge code must not depend on mutable `IonPy` wrappers; a sentinel makes "tombstone beats everything"
  a trivial pattern match and keeps merge rules independent of Ion.
- **Alternatives**: operate on `IonPy` directly (mutable, annotation handling leaks everywhere); plain JSON (loses
  annotations and decimals).
- **Open check for tasks**: confirm `amazon.ion` round-trips `deleted::null` annotations and `decimal` exactly; add a
  codec test before anything else.

## R-3. Canonical serialization (FR-019 byte-for-byte)

- **Decision**: documents and model errors serialize as compact Ion text with struct fields sorted by Unicode code
  point, lists in order, no insignificant whitespace, decimals preserved as written by the codec, annotations first.
  The normative definition is the "Canonical Ion text" section of `contracts/conformance-vectors.md`; this note records
  the reasons.
- **Rationale**: Ion structs are unordered, so any byte-identical guarantee needs a defined canonical form; text is
  diffable and portable across ports.
- **Alternatives**: Ion binary (smaller but byte layout varies by writer options); JSON (no annotations).
- **Risk**: decimal/float rendering differences across libraries; the vectors avoid floats, and ports must pin
  decimal text. Revisit when a second port is built.

## R-4. Merge, order and fold implementation

- **Decision**: order key is `(tier_rank, seq)`. P1 folds each tier's entries into a layer; P2 overlays layers with
  `combine`; tombstones are retained until P3. Work on a per-leaf-path basis: a `set` at a container path with a struct
  value is expanded into leaf writes only where the schema merges `struct` field-wise; whole-value rules (`lww`,
  `union`, `max`, `min`) treat their field as one value.
- **Rationale**: matches SPEC 6.2 and 6.7, gives one fold implementation so the flat-fold oracle used by tests is
  trivial (`reduce(combine)` over sorted entries) and independent of the layered code.
- **Alternatives**: materialising each tier as nested dicts then recursive merge (harder to prove equal to the flat fold).
- **As built (US2)**: a layer maps a path to a fold state. A `set` of a struct at a field merged by a field-wise rule
  (`struct`) is spread over its child paths, with an empty container marker at the path itself, so that writes at
  `/address` and `/address/city` interleave by (tier, seq). Overlaying a layer discards the lower layers' descendants
  of any tombstone or re-created path. The service keeps per-tier layers in memory and folds only unseen entries; the
  stored view is the durable cache. Measured: a cold read of 1,000 entries takes about 7 ms (memory) and 30 ms (SQLite).
  An append derives the new view inside its own transaction and stores it there (one write transaction); the in-memory
  state is a bounded LRU and is only updated after the commit. Entries a schema version cannot use (a tier it lacks, a
  value no rule can merge) are skipped as a whole, never half-applied, and listed in `DocumentResult.skipped` instead of
  making the document unreadable (SPEC 14: shadowed with a recorded reason). Values that merge inside each tier but clash across tiers (entries written under another schema version) are applied path
  by path: the incoming value of a clashing path is left out and the path is listed in `DocumentResult.conflicts`, as is a path
  that has no place in the document (it lies beneath an atomic value). Readers on SQLite use one read-only WAL connection per
  thread, so reads never wait for a writer; a cached derivation state is only continued under the very schema object it was
  built from. Admission dry-merges every written value,
  children of a struct included, so new entries cannot poison a document.
- **Tombstone detail**: `combine(old, TOMBSTONE) = TOMBSTONE`; `combine(TOMBSTONE, v) = Recreated(rule(absent, v))`;
  `combine(x, Recreated(v)) = Recreated(v)`; `combine(Recreated(u), v) = Recreated(rule(u, v))`. A tombstone on a struct
  path clears all descendants accumulated so far (subtree), later writes re-create written leaves only.
  **The first design (re-create gives a plain value) was not associative**: `max(0, [delete, -1])` is `-1` in the flat
  fold but `0` when the tier is squashed first. The property tests found it (counterexample 0, delete, -1); the
  `Recreated` reset marker fixes it, and associativity over `(absent, value, tombstone, recreated)` is property-tested
  for every rule (SPEC 6.7 item 2).

## R-5. Validation scope in this feature

- **Decision**: ship the validator *mechanism* (registry kind `validate`, protocol returning violations
  `(rule_id, path_scope, message)`), field-level and document-level hooks, and only the presets the normative examples
  use: `non_empty`, `range`, `max_len`. `max_spouses_by_country` is a test fixture registered in tests.
- **Rationale**: FR-006/021-030 need rules to exist to be evaluated; the broader rule library and per-field authority are
  Feature 4 (spec Assumptions).
- **Alternatives**: no presets (golden test 16.2 would need fixtures for everything); full library (scope creep, Principle XI).
- **Load-time rule**: a validator may declare `depends` paths; a dependency on a derived (computed or deferred) field
  that is deferred is refused at load (FR-021). Computed fields do not exist yet, so only deferred declarations need
  detection, and the schema parser accepts and records `computed`/`deferred` declarations without evaluating them.

## R-6. Atomic append, `seq` allocation and concurrency

- **Decision**: one SQLite transaction per request (`BEGIN IMMEDIATE`): read the document's counter, run admission
  against the entries read in that same transaction, insert entries with consecutive `seq` and a shared `request_id`,
  bump the counter, commit. Any rejection rolls back (nothing was written). `expected_seq` is checked inside the
  transaction and yields `SEQ_CONFLICT`. WAL mode; single writer per document is enforced by the write lock.
- **Rationale**: gives FR-002 and FR-029 for free, makes concurrent submissions strictly ordered, and admission sees a
  state that cannot change under it.
- **Alternatives**: optimistic retry without locking (more code, no benefit at this scale); an external sequencer
  (deployment concern, deferred with multi-region).
- **Immutability enforcement**: SQLite triggers abort `UPDATE` and `DELETE` on `entries` (Principle I). Compaction and
  purge later will use a dedicated path.

## R-7. Model-error attribution and replay (FR-023, FR-025, FR-026, FR-030)

- **Problem**: SPEC 10.1 attributes each error to the request that introduced it or, after re-derivation, to a schema
  version, while Principle III and conformance test 6 require the view to be reproducible from the ledger alone.
- **Decision**: attribution is a pure, version-aware replay. Walk the requests in `request order`; evaluate validators
  under each request's recorded `schema_version`. A new error that appears after request R gets `introduced_by =
  request R`. When the active (or pinned) version differs from the last request's version, evaluate once more under the
  active version: errors that are new at that boundary get `introduced_by = schema vN`. An error keeps its attribution
  until it disappears; if it reappears later it is a new error with new attribution. The cache stores the result, so
  normal reads do not replay.
- **Rationale**: preserves SPEC 16.2 outcomes (E1, E2 attributed to requests; E3 to schema v8), keeps `rebuild`
  bit-identical to the cache, and needs only immutable schema versions (Principle IX), which the registry keeps.
- **Cost**: one derivation per request on a cold rebuild. Incremental fold (apply request, re-validate, diff errors)
  keeps this linear in requests; SC-006 is measured on a cold read.
- **Alternatives**: attribute only at write time and store (breaks replay); attribute everything to the final version
  (loses the per-request attribution FR-030 requires).
- **Follow-up (done)**: SPEC 10.1 now states that attribution is computed by replay.

## R-8. No-new-errors comparison and error identity

- **Decision**: error identity is `(rule_id, path_scope)` (SPEC 10.3, open question 17.10 stays deferred). On an unflagged
  request, admission computes `errors_after` on the dry-run document of the whole request and rejects with
  `MODEL_ERROR_INTRODUCED` iff `errors_after − errors_before ≠ ∅`. If the document has no model errors, any
  validation failure rejects with `VALIDATION_FAILED`. `errors_before` is computed under the version in force at admission.
- **Flagged request**: allowed when `lenient.allowed` lists the contributor, or lists every tier used in the request; otherwise
  `LENIENT_NOT_PERMITTED` even if the data is valid (spec scenario 7.4). V0 errors are checked first and never bypassed.
- **Rationale**: one comparison, one place, covers scenarios 7.3-7.8 and edge cases.

## R-9. Conformance vectors

- **Decision**: vectors are Ion files at `conformance/`, each a `case::{ schema, requests, expect }` document with the
  canonical output (R-3). The Python golden tests load these files; they are the only source of expected values for
  SPEC 16.1 and 16.2 (no duplicate expectations in Python).
- **Rationale**: Constitution requires shared vectors; also keeps the "change merge results ⇒ update vectors in the same
  change" gate mechanical.
- **Alternatives**: Python-only fixtures now, extract later (guaranteed drift).

## R-10. Idempotency and retries (FR-008)

- **Decision (user, 2026-10-18)**: a retry must never change the outcome or the shape of the response. Requests MAY carry a
  caller-supplied `request_key`. The request hash is SHA-256 over canonical Ion text of `(doc, contributor, lenient,
  entries)`; `expected_seq` is excluded so a retry after other writes still matches. A request with the same
  `(doc, contributor, request_key)` and the same hash is a duplicate: not admitted, not validated, not audited, and the
  response (original entries, `request_id`, model-error changes by replay, plus the latest materialized view) has the same
  shape as the original success with `duplicate: true`. The same key with a different hash is `REQUEST_KEY_REUSED`.
- **Why a key and not a pure hash**: a pure hash would treat a deliberate repeat (set Alice, someone sets Bob, set Alice
  again) as a duplicate and silently drop it. Without a `request_key` no duplicate detection happens.
- **Per-entry `idem_key`** stays for non-idempotent behaviors (`sum`, `append`; not in this feature's rules but the field and
  the unique index exist). Reuse is rejected with `DUPLICATE_IDEM_KEY`, which keeps requests atomic (no partial skipping).
- **Storage**: `request_key` and `request_hash` live on the request's entries; the duplicate check is one indexed lookup
  inside the same transaction as the append, so concurrent retries cannot both commit.
- **Rejected requests** store nothing, so their retries are processed again and give the same deterministic result.

## R-11. Rejection audit log (FR-027, FR-028)

- **Decision**: separate table written by `audit.py`, called by the service layer after the ledger transaction has rolled
  back, never imported by `engine/*`. Records: document, contributor, tier, flag, attempted entries, every reason,
  schema version, time. Settings come from schema (`rejection_audit`) with a per-document override table. Loading a
  schema with auditing enabled and no `max_entries`/`max_age` fails. Auditing is best effort: a failing audit write is logged and the rejection is still returned. Pruning removes the oldest records first (by recorded time) and runs after each audit write and via an admin
  call: delete oldest beyond `max_entries` per document, then anything older than `max_age`; the prune count is kept in
  log metadata.
- **Test**: deleting the whole table changes no document, no `seq`, no model error (conformance item 7).
- **Mode**: `rejection_audit.mode` is `full` (default) or `metadata_only`. In metadata-only mode the record keeps
  contributor, tier, time, `lenient`, attempted paths and operations, reasons and schema version, and never values
  (SC-013).
- **Redaction**: `redact` is a later feature; the plan stores attempted values as given and notes the dependency in the
  contract (values are subject to `redact` once it exists).

## R-12. Version change and caching (FR-026)

- **Decision**: `views` cache is keyed by `(doc, watermark_seq, schema_version, rules_fingerprint)`; a read compares to
  the active (or pinned) version and fingerprint; mismatch discards the row and re-derives. `append` writes the new view
  in its own transaction; a read that has to derive keeps the view in memory only and never takes the write lock; `rebuild`
  stores views. The rules fingerprint is a digest of everything in the schema version that decides a document. The
  ledger is never touched by re-derivation.
- **Rationale**: matches scenarios 8.1-8.2 with an observable "no re-derivation" counter for the test.

## R-13. Schema format and loading

- **Decision**: schema file is Ion following SPEC 5.2, parsed into frozen dataclasses and validated at load (fail
  closed: unknown behavior, missing `associative`, cycle among declared derived fields, unreachable tier, validator
  depending on a deferred field, auditing without retention, unknown tier in `lenient.allowed`). It is aduku's own format,
  not Ion Schema (the handoff's phrasing); a JSON Schema vocabulary for the annotations is the decided later direction (SPEC 17.7).
  See `contracts/schema-format.md`.
- **Validator faults**: a fault in a validator (an exception, not a violation) is never treated as "no violations": admission rejects the request and a read fails.
- **Activation**: activating a version that drops or renames a tier used by any ledger entry is refused (`TIER_IN_USE`); so is
  pinning a document to such a version, and starting a service over an already-active or pinned one. The schema registry stores versions immutably; `activate(version)` for new admissions and reads;
  per-document pinning supported as a field on the document record.

## R-14. Property-testing strategy

- **Decision**: hypothesis strategies generate (schema from a fixed pool of rule combinations) x (ledgers of entries with
  random tiers, paths, values, ops). Oracles: (a) flat fold is a separate 15-line reference implementation inside the
  tests; (b) permuting arrival order while keeping `(tier, seq)` equal, and rewriting `tx_time`, must not change output;
  (c) rule laws tested on generated triples; (d) atomicity: any request containing one invalid entry leaves the store
  byte-identical (compare a full dump).
- **Budget**: SC-002 requires at least 1,000 ledgers; configure `max_examples=1000` in CI profile, fewer locally.

## R-15. CLI

- **Decision**: thin `aduku` CLI over the service: `schema check`, `append`, `get`, `explain`, `rebuild`, `audit list`,
  `audit prune`. JSON in/out by default, Ion on request. It exists for the quickstart and manual checks, not as a product.
