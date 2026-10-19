# Tasks: Core Ledger and Merge Engine

**Input**: Design documents from `/specs/001-core-ledger-merge-engine/`

**Prerequisites**: plan.md, spec.md, research.md, data-model.md, contracts/, quickstart.md

**Tests**: INCLUDED and mandatory. Constitution Principle X (NON-NEGOTIABLE) requires law tests, order-tolerance tests, replay tests and the worked example, and requires tests to be written and observed failing before implementation. In every phase, run the test tasks first and confirm they fail before starting the implementation tasks.

**Organization**: Tasks are grouped by user story. All Python paths are under `ports/py/` (plan.md, Structure Decision); shared vectors live in `conformance/` at the repo root.

## Format: `[ID] [P?] [Story] Description`

- **[P]**: Can run in parallel (different files, no dependency on an incomplete task)
- **[Story]**: User story from spec.md (US1 to US8). Setup, Foundational and Polish tasks have no story label.

## Story map

| Story | Priority | Subject |
|---|---|---|
| US1 | P1 | Record contributions durably (admission, atomicity, retries, rejection audit) |
| US3 | P1 | Declare merge behavior per field in the central schema |
| US2 | P1 | Read one deterministic merged document |
| US4 | P2 | Defaults that contributions may replace |
| US5 | P2 | Delete fields with tombstones |
| US7 | P2 | Record data that breaks validation rules (model errors) |
| US8 | P2 | Re-evaluate documents when the schema or rules change |
| US6 | P3 | Explain why a field has its value |

US3 is built before US2 because the merged document needs the merge rules (both are P1).

---

## Phase 1: Setup

- [X] T001 Create `ports/py/pyproject.toml` for a uv project: Python 3.12, dependencies `amazon.ion` and a ULID library (for example `python-ulid`), dev dependencies `pytest`, `hypothesis`, `ruff`, `mypy`, console script `aduku = aduku.cli:main`, strict mypy for `src/aduku`, ruff config, pytest `testpaths`
- [X] T002 [P] Create the package skeleton `ports/py/src/aduku/__init__.py` with empty subpackage `__init__.py` files in `schema/`, `behaviors/`, `engine/`, `storage/`
- [X] T003 [P] Create `ports/py/tests/conftest.py` with two hypothesis profiles: `dev` (50 examples) and `ci` (`max_examples=1000`, deadline disabled), and empty `unit/`, `property/`, `golden/`, `integration/` test folders
- [X] T004 [P] Create `conformance/README.md` describing the case format from `contracts/conformance-vectors.md`, the canonical Ion text rule (R-3), and how fixture behaviors such as `acme.max_spouses_by_country@1` are named and defined

---

## Phase 2: Foundational (blocks every story)

**Purpose**: value model, schema loading, behavior registry, storage and clock interfaces that all stories share.

### Tests first (confirm they fail)

- [X] T005 [P] Write `ports/py/tests/unit/test_purity_imports.py`: assert modules under `aduku/engine/` and `aduku/schema/` do not import `aduku.storage`, `aduku.audit`, `sqlite3`, `time`, `datetime`, `random` or `os` (Principle III)
- [X] T006 [P] Write `ports/py/tests/unit/test_ion_roundtrip.py`: verify `amazon.ion` round-trips `deleted::null` (annotation kept, distinct from plain null and from absent), decimals exactly, structs, lists, timestamps (research R-2 open check)
- [X] T007 [P] Write `ports/py/tests/unit/test_canonical.py`: canonical Ion text has struct fields sorted by Unicode code point, compact, annotations first, identical bytes for equal values built in different orders (R-3)
- [X] T008 [P] Write `ports/py/tests/unit/test_paths.py`: path parsing and normalization per SPEC 3.2 (no empty, `.` or `..` segments), `*` and `**` pattern matching, keyed segment `[sku="A1"]` parsed but not evaluated
- [X] T009 [P] Write `ports/py/tests/unit/test_schema_loader.py`: loads the SPEC 5.2 subset (id, version, tiers, fields, type, merge, default, on_delete, authority, validate, lenient, rejection_audit with `mode`, derived declarations parsed only) and reports every load problem together as `SchemaError`
- [X] T010 [P] Write `ports/py/tests/unit/test_schema_registry.py`: versions are immutable (re-registering `(id, version)` with different content fails), `activate(version)`, per-document pin

### Implementation

- [X] T011 Implement `ports/py/src/aduku/ion_codec.py`: `TOMBSTONE` sentinel, conversion between Ion and immutable internal values (Mapping, tuple, Decimal, str, ...), canonical serializer (R-3); makes T006 and T007 pass
- [X] T012 [P] Implement `ports/py/src/aduku/errors.py`: error codes from SPEC 12.5 (`STORAGE_ERROR`, `FIELD_NOT_WRITABLE`, `NOT_AUTHORIZED`, `TIER_NOT_PERMITTED`, `UNKNOWN_PATH`, `TYPE_MISMATCH`, `OP_NOT_ALLOWED`, `VALIDATION_FAILED`, `MODEL_ERROR_INTRODUCED`, `IMPURE_COMPUTE_FAILED`, `LENIENT_NOT_PERMITTED`, `DUPLICATE_IDEM_KEY`, `REQUEST_KEY_REUSED`, `IDEM_KEY_REQUIRED`, `SEQ_CONFLICT`, `SCHEMA_INACTIVE`), `Reason {entry_index?, code, rule?, path?, message}`, `RejectedError {code, reasons}`, `SchemaError`
- [X] T013 [P] Implement `ports/py/src/aduku/model.py`: frozen dataclasses (`Identity` carries the contributor id and permitted tiers, FR-032) `Entry` (fields per data-model.md: id, doc, seq, request_id, tier, path, op `set|delete`, value, contributor, tx_time, idem_key?, request_key?, request_hash?, schema_version, lenient, meta?), `EntryInput`, `Identity`, `ModelError {rule_id, path_scope, message, introduced_by}` where identity is `(rule_id, path_scope)`, `DocumentResult`, `AppendResult`
- [X] T014 [P] Implement `ports/py/src/aduku/paths.py` to satisfy T008
- [X] T015 Implement `ports/py/src/aduku/schema/model.py` (frozen `Schema`, `FieldSpec`, `BehaviorRef`, lookup order: exact path, then `*`, then `**`) and `ports/py/src/aduku/schema/loader.py` (Ion parse and structural validation; behavior-level rules are added in US3) to satisfy T009
- [X] T016 [P] Implement `ports/py/src/aduku/schema/registry.py` to satisfy T010
- [X] T017 [P] Implement `ports/py/src/aduku/behaviors/registry.py`: behavior registry entries `{name, version, kind: merge|validate, purity: pure, laws: {associative, commutative?, idempotent?}, impl}`; `register_merge`, `register_validate`
- [X] T018 [P] Implement `ports/py/src/aduku/storage/base.py`: `Storage` protocol (transactional append, entry reads, view cache, audit table access) `Clock` protocol (`Clock.system()`, fake clock for tests) and `IdSource` protocol (`IdSource.system()` makes ULIDs for entry ids and request ids; a deterministic fake for tests). Time and randomness enter only through these two injected interfaces, never in `engine/*`
- [X] T019 [P] Implement `ports/py/src/aduku/storage/memory.py`: in-memory `Storage` with the same transactional semantics
- [X] T020 [P] Write `ports/py/tests/integration/test_sqlite_ledger_immutable.py`: `UPDATE` and `DELETE` on `entries` abort; constraints quoted from data-model.md: "unique `(doc, seq)`, unique `(doc, idem_key)` where not null, unique `(doc, contributor, request_key)` per request where not null; `UPDATE`/`DELETE` forbidden"
- [X] T021 Implement `ports/py/src/aduku/storage/sqlite.py` (WAL mode; tables `entries`, `doc_seq`, `requests` (one row per request, unique `(doc, contributor, request_key)` where the key is not null), `views`, `rejection_audit`, `audit_meta`, `audit_override`; the constraints quoted in T020 enforced by unique indexes and by triggers that abort `UPDATE` and `DELETE` on `entries`; an `append_request` that fails rolls back to a savepoint so nothing of it remains); makes T020 pass
- [X] T022 [P] Implement `ports/py/src/aduku/log.py`: structured log events for admission decisions and rebuilds (constitution Development Workflow); used by T035 and T075
- [X] T023 [P] Create `conformance/customer.schema.ion`: the SPEC 5.2 schema subset (tiers `backfill`, `user`, `admin`; fields from SPEC 16.1) shared by golden tests and the quickstart
- [X] T024 Write the vector runner `ports/py/tests/conformance_runner.py`: loads `conformance/*.ion` cases, executes `append`, `get`, `explain`, `activate_schema`, `rebuild` steps against the service with a fake clock, compares canonical Ion text; fixture behaviors are registered from `ports/py/tests/fixtures.py` (`max_spouses_by_country@1`)

**Checkpoint**: Foundation ready; user stories can begin.

---

## Phase 3: User Story 1 - Record contributions durably (P1)

**Goal**: contributions become immutable ledger entries with sequence numbers, in atomic requests, with schema checks, retry no-ops and an optional rejection audit log.

**Independent Test**: append contributions to one document and list them back in sequence order; a rejected request leaves no entries; a retried request leaves no new entries.

### Tests first (confirm they fail)

- [X] T025 [P] [US1] Write `ports/py/tests/unit/test_admission_v0.py`: `UNKNOWN_PATH`, `TYPE_MISMATCH`, `OP_NOT_ALLOWED`, `TIER_NOT_PERMITTED`, `NOT_AUTHORIZED`, `FIELD_NOT_WRITABLE` for a path the schema declares derived, or beneath one (FR-031, scenario 8; not bypassable by the lenient flag); every schema error of every entry is listed together (scenarios 3 and 4); a request with a schema error and a rule violation lists only the schema errors (scenario 6)
- [X] T026 [P] [US1] Write `ports/py/tests/integration/test_atomic_append.py`: consecutive `seq` and one shared `request_id` per accepted request; a failing entry leaves a full store dump unchanged; concurrent appends get distinct, strictly increasing `seq`; stale `expected_seq` gives `SEQ_CONFLICT`; a simulated storage failure mid-commit makes no entry visible and raises `STORAGE_ERROR`, which is not a rejection (FR-029); a request with interdependent entries is valid
- [X] T027 [P] [US1] Write `ports/py/tests/integration/test_request_key.py`: same `request_key`, payload and identity is a no-op duplicate that returns the original entries (`duplicate: true`); same key with another payload is `REQUEST_KEY_REUSED`; the same payload without a key is applied again (set Alice, another writer sets Bob, set Alice again ends as Alice); a request key whose original request was rejected is processed as new; two simultaneous requests with the same key commit exactly one and the other gets the duplicate response; a duplicate is not validated or audited (FR-008); a reused entry `idem_key` rejects its request with `DUPLICATE_IDEM_KEY`; `expected_seq` is excluded from the request hash (scenario 2)
- [X] T028 [P] [US1] Write `ports/py/tests/integration/test_rejection_audit.py`: auditing off leaves no record; on writes exactly one record per rejected request; `metadata_only` keeps paths, operations and reasons but no values (scenario 7, SC-013); dropping the audit table changes no document, `seq` or model error; `max_entries` and `max_age` pruning (SC-011); schema with auditing enabled and no retention is refused at load; a failing audit write still returns the rejection (best effort, FR-027); oldest-first pruning order; per-document override (`enabled|disabled|inherit`)
- [X] T029 [P] [US1] Write `ports/py/tests/property/test_atomicity.py`: for generated requests with at least one invalid entry the store is byte-identical before and after (SC-005)
- [X] T030 [P] [US1] Write vectors `conformance/atomicity.ion` and `conformance/retries.ion` per `contracts/conformance-vectors.md`

### Implementation

- [X] T031 [US1] Implement `ports/py/src/aduku/engine/admission.py` stage 1 (V0): path to field resolution, type check, op allowed, tier permitted for the contributor, field `authority`, derived field `FIELD_NOT_WRITABLE`; collects all reasons; pure (no storage access)
- [X] T032 [P] [US1] Implement `ports/py/src/aduku/engine/request_hash.py`: SHA-256 of canonical Ion text of `(doc, contributor, lenient, entries)` where each entry is `(tier, path, op, value, idem_key, meta)`; `expected_seq` excluded (SPEC 4.2 item 4)
- [X] T033 [US1] Implement the transactional append in `ports/py/src/aduku/storage/sqlite.py` and `ports/py/src/aduku/storage/memory.py`: `BEGIN IMMEDIATE`, duplicate lookup by `(doc, contributor, request_key)` inside the transaction, `expected_seq` check, consecutive `seq` from `doc_seq`, insert entries with shared `request_id`, rollback on any rejection
- [X] T034 [US1] Implement `ports/py/src/aduku/audit.py`: write rejected requests after the ledger transaction has rolled back (never imported by `engine/*`), `full` and `metadata_only` modes, retention pruning by `max_entries` per document then `max_age`, prune count in log metadata, `list_rejections`, `set_rejection_audit`, `prune_rejections`; extend `schema/loader.py` to refuse auditing without retention (FR-028)
- [X] T035 [US1] Implement `ports/py/src/aduku/service.py` facade `Aduku.append(...)` per `contracts/library-api.md` (constructed with `storage`, `clock`, `ids`, `schemas`; entry ids, request ids and `tx_time` are assigned here): duplicate check, V0, commit or rollback then audit, structured log event (T022), `AppendResult` (until T070 lands, `model_errors_added` and `model_errors_resolved`, including those in a duplicate request's response, are empty; the `view` field is wired in T053); `list_entries`
- [X] T036 [US1] Add CLI commands `append`, `entries`, `audit list|prune|set` in `ports/py/src/aduku/cli.py` per `contracts/cli.md` (exit code 1 for rejected requests; global options `--db` and `--schema` fall back to the environment variables `ADUKU_DB` and `ADUKU_SCHEMA`, then to `aduku.db`)

**Checkpoint**: US1 is complete when T025 to T030 pass.

---

## Phase 4: User Story 3 - Declare merge behavior per field (P1)

**Depends on**: US1 for T038 and T043 (admission and `append`); the merge behaviors and loader rules do not.

**Goal**: one central schema decides how conflicting values combine: last-write-wins, maximum, minimum, union, field-by-field.

**Independent Test**: define a schema with one field per rule, contribute conflicting values from different tiers, check each field resolves per its rule.

### Tests first (confirm they fail)

- [X] T037 [P] [US3] Write `ports/py/tests/property/test_merge_laws.py`: for each merge behavior, associativity over `absent | value | TOMBSTONE`; commutativity and idempotence where declared; `absent` is the left identity (Principle V)
- [X] T038 [P] [US3] Write `ports/py/tests/unit/test_schema_merge_rules.py`: scenarios 1 to 3 (age `max` 30 vs 28 gives 30; tags `union` in tier order without duplicates; nested address field by field); a merge type conflict is rejected at admission, never merged (SPEC 6.5); SC-007: loading schema version 2 with one more merge-governed field makes that field merge per its rule with no change to the writers' requests
- [X] T039 [P] [US3] Write `ports/py/tests/unit/test_schema_load_failures.py`: custom merge without `associative` refused (scenario 4, FR-011); validator, computed field or default depending on a deferred field refused (FR-021); unknown behavior; derived-field cycle; default not conforming to `type`; `on_delete: default` on a field without a default; unknown tier in `lenient.allowed` or `authority`; duplicate tier; `max`/`min` on an unordered type refused (FR-010); all errors reported together
- [X] T040 [P] [US3] Write vector `conformance/schema-load-failures.ion`

### Implementation

- [X] T041 [US3] Implement `ports/py/src/aduku/behaviors/merge.py`: `lww`, `max`, `min`, `union`, `struct` with declared laws; combine handles `absent`, values and `TOMBSTONE` (`combine(old, TOMBSTONE) = TOMBSTONE`; `combine(TOMBSTONE, v) = combine(absent, v)`); registers them in `behaviors/registry.py`
- [X] T042 [US3] Extend `ports/py/src/aduku/schema/loader.py` with the fail-closed rules from T039 (FR-011, FR-021, cycles, default typing, tiers), reporting every problem together
- [X] T043 [US3] Merge type safety (SPEC 6.5): the loader refuses a rule on a type it cannot merge (`MERGE_TYPE_UNSUITABLE`) and admission's type check keeps every value to its field type; a rule applied to mismatched values raises `MergeTypeError` and never merges. Comparing a new value with the current document value for untyped fields needs the dry-run derive, so that mapping to a rejection is done in the dry-run task of US7
- [X] T044 [US3] Add CLI command `schema check FILE` in `ports/py/src/aduku/cli.py`

**Checkpoint**: US3 is complete when T037 to T040 pass.

---

## Phase 5: User Story 2 - Read one deterministic merged document (P1)

**Goal**: a reader gets one document that depends only on tier, sequence and the schema, never on wall-clock time or write races.

**Independent Test**: derive the same ledger with different recorded times and in different arrival order; results are byte-identical; per-tier squash then merge equals the flat fold.

**Depends on**: US1 (entries), US3 (merge rules). The full 13-entry golden test needs defaults (US4) and tombstones (US5) too, so it lives in Polish (T080).

### Tests first (confirm they fail)

- [X] T045 [P] [US2] Write `ports/py/tests/oracle.py`: the reference flat fold (sort by `(tier rank, seq)`, left fold of `combine` from the defaults layer), independent of the layered code
- [X] T046 [P] [US2] Write `ports/py/tests/property/test_squash_equivalence.py`: generated ledgers; per-tier squash then merge equals the oracle flat fold (SC-002; run with the `ci` profile for at least 1,000 ledgers)
- [X] T047 [P] [US2] Write `ports/py/tests/property/test_order_tolerance.py`: rewriting only `tx_time`, and permuting arrival order within fixed `(tier, seq)`, never changes the document (SC-003)
- [X] T048 [P] [US2] Write `ports/py/tests/unit/test_derive_basic.py`: scenario 3 (late lower-tier write cannot beat an earlier higher-tier `lww` value), byte-identical output for identical ledgers (FR-019), stable field ordering
- [X] T049 [P] [US2] Write `ports/py/tests/integration/test_read_performance.py`: a document with 1,000 accepted entries (1,000 single-entry requests, the worst case for replay) reads uncached in under 1 second on the CI runner (SC-006); re-run after T070 and T074, since attribution by replay adds work to a cold read

### Implementation

- [X] T050 [P] [US2] Implement `ports/py/src/aduku/engine/squash.py` (P1: fold each tier's entries in `seq` order) and `ports/py/src/aduku/engine/merge_layers.py` (P2: overlay layers lowest tier first using each field's `combine`, tombstones retained)
- [X] T051 [P] [US2] Implement `ports/py/src/aduku/engine/tombstones.py` (P3, minimal): remove tombstoned paths from the merged document; US5 and US4 extend it
- [X] T052 [US2] Implement `ports/py/src/aduku/engine/derive.py` composing P1 to P3 and `ports/py/src/aduku/engine/project.py` (P7, minimal: canonical document plus `model_errors` list, initially empty)
- [X] T053 [US2] Implement `Aduku.get(doc)` in `ports/py/src/aduku/service.py` with the `views` cache in `storage/sqlite.py` and `storage/memory.py` (key `(doc, watermark_seq, schema_version, rules_fingerprint)`, invalidated on append); wire `AppendResult.view` and the duplicate-request response to return the latest view (T035, SC-012)
- [X] T054 [US2] Add CLI command `get DOC` in `ports/py/src/aduku/cli.py`

**Checkpoint**: US2 is complete when T045 to T049 pass. P1 stories (US1, US3, US2) form the MVP.

---

## Phase 6: User Story 4 - Defaults that contributions may replace (P2)

**Goal**: schema defaults fill a document until someone contributes; deletes follow the field's `on_delete` setting.

**Independent Test**: read a document with no contribution to a defaulted field, contribute, read again, then delete under each `on_delete` setting.

### Tests first (confirm they fail)

- [X] T055 [P] [US4] Write `ports/py/tests/unit/test_defaults.py`: scenarios 1 to 4 (default `free`, contribution `EUR` replaces `USD`, delete with `on_delete: absent` removes the field, delete with `on_delete: default` shows the default); empty ledger gives only defaults; struct defaults apply per child
- [X] T056 [P] [US4] Write vector `conformance/defaults-on-delete.ion`

### Implementation

- [X] T057 [US4] Add the defaults layer (static Ion values only; dynamic defaults are out of scope for this feature) to `ports/py/src/aduku/engine/merge_layers.py`, with per-child struct defaults
- [X] T058 [US4] Implement `on_delete` handling in `ports/py/src/aduku/engine/tombstones.py`: `absent` (default) or fall back to the default

**Checkpoint**: US4 complete when T055 and T056 pass.

---

## Phase 7: User Story 5 - Delete fields with tombstones (P2)

**Goal**: a delete wins over lower tiers and earlier writes, and a later same-or-higher-tier write re-creates the field; deleting a struct removes its subtree.

**Independent Test**: contribute at a low tier, delete at a high tier, read; then contribute again at the high tier, read.

### Tests first (confirm they fail)

- [X] T059 [P] [US5] Write `ports/py/tests/unit/test_tombstones.py`: scenarios 1 to 4; edge cases (a delete arriving before any value still suppresses lower tiers; a write beneath a deleted parent re-creates only the written leaves); `rebuild` after deletes gives the same document
- [X] T060 [P] [US5] Write vector `conformance/tombstones.ion` (SPEC 6.7 rules)

### Implementation

- [X] T061 [US5] Subtree clearing is already implemented, because the overlay cannot be associative without it: `apply_layer` in `ports/py/src/aduku/engine/merge_layers.py` discards lower-layer descendants of a tombstone or re-created value, and the `Recreated` marker is in `behaviors/merge.py` (T041, T050). This task now adds the remaining US5 behaviors, if any test in T059 finds one missing, and confirms the SPEC 6.7 rules end to end
- [X] T062 [US5] Review `ports/py/src/aduku/engine/tombstones.py` and `engine/document.py` against T059 (P3 already drops tombstones and reset markers and nests the values; US4 adds `on_delete`)

**Checkpoint**: US5 complete when T059 and T060 pass. Done: no engine change was needed beyond what US2 and US4 built; the tests were checked by breaking the engine four ways (no subtree wipe, no reset marker, deletes ignored, tombstones left in the document) and each is caught.

---

## Phase 8: User Story 7 - Record data that breaks validation rules (P2)

**Goal**: rule violations either reject the request, or, for permitted writers using the non-rejection flag, are kept as model errors returned with the document; unflagged requests may never add a model error.

**Independent Test**: submit a backfill that violates a rule with the flag, then without it, and read the document each time (SPEC 16.2 steps 1 to 6).

### Tests first (confirm they fail)

- [X] T063 [P] [US7] Write `ports/py/tests/unit/test_validators.py`: presets `non_empty`, `range`, `max_len`; the fixture document rule `max_spouses_by_country`; violations carry `(rule_id, path_scope)`
- [X] T064 [P] [US7] Write `ports/py/tests/integration/test_model_errors.py`: scenarios 1 to 8 and the SPEC 16.2 steps 1 to 6; a duplicate of a request that changed model errors reports the same `model_errors_added` and `model_errors_resolved` as the original (FR-008) (E1 recorded; unrelated update accepted; `MODEL_ERROR_INTRODUCED`; `LENIENT_NOT_PERMITTED` even for valid data; flagged request skips the no-new-errors rule; fix resolves E1); a schema error is never bypassed by the flag; rules run once per request on the document that results from all its entries
- [X] T065 [P] [US7] Write `ports/py/tests/property/test_no_new_errors.py`: an unflagged accepted request never adds a model error to a document that has errors; a permitted flagged request on a validation failure always succeeds; V0 failures always reject
- [X] T066 [P] [US7] Write vector `conformance/model-errors-16-2.ion` steps 1 to 6 (step 7 is added in T076)

### Implementation

- [X] T067 [US7] Implement `ports/py/src/aduku/behaviors/validate.py`: validator protocol `check(view) -> [Violation {rule_id, path_scope, message}]`, presets `non_empty`, `range`, `max_len`
- [X] T068 [US7] Implement `ports/py/src/aduku/engine/validate.py` (P6): field validators on merged values and `document_validate` rules; deferred values are never inputs; a validator fault raises an error and is never treated as "no violations" (admission rejects, a read fails)
- [X] T069 [US7] Admission dry run (in `ports/py/src/aduku/service.py` `_admit` with `engine/model_errors.py`; `engine/admission.py` holds V0 and the struct-child checks): after a clean V0, dry-run derive the whole request (a `MergeTypeError` from the fold becomes a `TYPE_MISMATCH` reason, SPEC 6.5), run V1 to V3, apply `LENIENT_NOT_PERMITTED` per `lenient.allowed` (contributor listed, or every tier in the request listed, FR-022), the no-new-errors comparison (`errors_after − errors_before ≠ ∅` gives `MODEL_ERROR_INTRODUCED`, or `VALIDATION_FAILED` when the document had no errors), and the flagged-writer exemption (SPEC 10.2, 10.3)
- [X] T070 [US7] Implement model-error attribution by version-aware replay in `ports/py/src/aduku/engine/model_errors.py` (research R-7, SPEC 10.1; `derive.py` holds the state it continues from) as an incremental fold (apply one request, validate, diff the error set; never re-derive the whole document per request) so a cold read stays linear in requests: requests walked in order under their recorded schema version; `introduced_by` is a request id or, at a version boundary, a schema version; return errors from `get`, `model_errors_added` and `model_errors_resolved` from `append`; accept an optional lenient reason in entry `meta`
- [X] T071 [US7] Add `--lenient` to the CLI `append` command and show `model_errors` in `get` in `ports/py/src/aduku/cli.py`

**Checkpoint**: US7 complete when T063 to T066 pass.

---

## Phase 9: User Story 8 - Re-evaluate documents on schema or rule change (P2)

**Goal**: a document read under a newer schema or rule version is re-derived from the ledger and its model errors recomputed; a read under the current version does not re-derive.

**Independent Test**: read a document, tighten a validation rule, read again; the ledger is byte-identical.

### Tests first (confirm they fail)

- [X] T072 [P] [US8] Write `ports/py/tests/integration/test_version_change.py`: reading under an inactive version gives `SCHEMA_INACTIVE`; activating a version that drops or renames a tier used by an entry is refused (FR-009); a model error whose path was deleted disappears unless the rule still fails (FR-025); scenarios 1 and 2 (E3 `(max_len, /name)` appears under schema v8 and is attributed to the version; a second read makes no re-derivation, checked with a derivation counter); the first read after a change equals a fresh derivation; the ledger is byte-identical
- [X] T073 [P] [US8] Write `ports/py/tests/integration/test_rebuild.py`: `rebuild` reproduces the cached view bit-for-bit, including model-error attribution (conformance item 6)

### Implementation

- [X] T074 [US8] Add `rules_fingerprint` to `ports/py/src/aduku/schema/model.py` and discard-and-re-derive on mismatch in `Aduku.get` (`service.py`), with a derivation counter hook for tests
- [X] T075 [US8] Implement `Aduku.rebuild(doc|None)` in `ports/py/src/aduku/service.py` returning a `RebuildReport` and emitting a structured log event (T022); add pin and `activate` plumbing through `schema/registry.py` (activation refuses a version that drops or renames a tier used by any entry); add CLI command `rebuild`
- [X] T076 [US8] Add step 7 (schema v8 adds `/name max_len 2`) to `conformance/model-errors-16-2.ion`

**Checkpoint**: US8 complete when T072 and T073 pass and T049 (SC-006) still passes on the cold read.

---

## Phase 10: User Story 6 - Explain why a field has its value (P3)

**Goal**: an auditor gets the winning entry, the rule and the superseded entries (or the default as source) for any path.

**Independent Test**: ask for the explanation of each field in a small ledger and verify the winner and rule.

### Tests first (confirm they fail)

- [X] T077 [P] [US6] Write `ports/py/tests/unit/test_explain.py`: scenarios 1 and 2 (`/age` names the `max` rule and the entry that supplied 30; an untouched defaulted field names `default`); `shadowed` lists superseded entries; `model_errors` limited to errors whose scope includes the path; one request per field (SC-004)

### Implementation

- [X] T078 [US6] Decided differently from the first draft: provenance is not recorded on every append. `ports/py/src/aduku/engine/explain.py` works it out from the ledger when asked (an entry is essential to a path if deriving without it changes the value there; the winner is the last essential entry in merge order), so it is right for any merge rule and adds no cost to writes or the cached view; `derive` takes an optional `prepared` for it
- [X] T079 [US6] Implement `ports/py/src/aduku/engine/explain.py`, `Aduku.explain(doc, path)` in `service.py` and CLI `explain DOC PATH`

**Checkpoint**: US6 complete when T077 passes.

---

## Phase 11: Polish and cross-cutting

- [X] T080 [P] Write `conformance/worked-example-16-1.ion` (SPEC 16.1 through P3: layers, merged document, the rejected `FIELD_NOT_WRITABLE` request with audit on and off) and `ports/py/tests/golden/test_worked_example.py` running it through the conformance runner (SC-001); also run `conformance/model-errors-16-2.ion` there **Recorded deviation from Principle X**: this golden test was written after the implementation as the acceptance gate and was green at its first run, so it was never observed failing; the behaviors it combines each had a failing test first (the US1 to US8 tests and vectors).
- [X] T081 [P] Add the clock-only vector pair (cases differing only in `tx_time`) to `conformance/` and a golden test that both give identical output
- [X] T082 [P] Write `ports/py/README.md` (install with uv, test commands, the `ports/py/` to `aduku-py` split assumption)
- [X] T083 Run `uv run ruff check . && uv run mypy src` in `ports/py/` and fix findings
- [X] T084 Run `uv run pytest tests/property --hypothesis-profile=ci` and confirm SC-002 and SC-003 (at least 1,000 ledgers, 0 mismatches)
- [X] T085 Execute every step of `specs/001-core-ledger-merge-engine/quickstart.md` and correct the guide or code where they disagree
- [X] T086 At commit time only (not a build step): remove the Sync Impact Report comment from `.specify/memory/constitution.md`

---

## Requirements coverage

| Requirement | Tasks |
|---|---|
| FR-001, FR-002, FR-029 | T013, T026, T033, T035 |
| FR-003 | T047 |
| FR-004, FR-005 | T006, T011, T014, T041, T062 |
| FR-006, FR-007, FR-031 | T025, T031, T043, T069 |
| FR-008 | T027, T032, T033, T053 |
| FR-009 to FR-011 | T015, T039, T041, T042, T072, T075 |
| FR-012, FR-013, FR-019 | T007, T011, T045, T046, T047, T050 |
| FR-014 to FR-016 | T055, T057, T058, T059, T061, T062 |
| FR-017, FR-018, SC-004 | T053, T077, T078, T079 |
| FR-020 | T037 |
| FR-021 | T039 |
| FR-022 to FR-026, FR-030 | T063 to T070, T072, T074 |
| FR-027, FR-028 | T028, T034 |
| FR-032 | T013, T035 |
| FR-033 | T073, T075 |
| SC-001, SC-002, SC-003 | T080, T046, T047, T084 |
| SC-005, SC-010 to SC-013 | T029, T028, T027 |
| SC-006 | T049 |
| SC-007 | T038 |
| SC-008, SC-009 | T064, T072 |

## Dependencies and execution order

- **Phase 1 → Phase 2 → stories → Polish.** Phase 2 blocks all stories.
- **US1** needs Phase 2 only.
- **US3** needs Phase 2 and **US1**: T038 submits requests through `append` and T043 extends `admission.py` created in T031. Its merge behaviors and loader rules (T037, T039 to T042) can start in parallel with US1.
- **US2** needs US1 (entries) and US3 (merge rules).
- **US4, US5** need US2. **US7** needs US2 (dry-run derive) and US1 (admission). **US8** needs US2 and US7 (attribution). **US6** needs US2.
- **Polish T080** needs US4 and US5 (defaults and tombstones) and US7 and US8 for the 16.2 vector.
- Within each story: tests, confirmed failing, then implementation.

### Parallel opportunities

- Phase 2 tests T005 to T010 together; T012, T013, T014, T016, T017, T018, T019, T022, T023 touch different files.
- US1 tests T025 to T030 together; US3 tests T037 to T040 together; US2 tests T045 to T049 together.
- After US2: US4, US5 and US6 can proceed in parallel (US4 and US5 both touch `tombstones.py` and `merge_layers.py`, so sequence T058 and T062 and T057 and T061 when done by one person).

### Parallel example (US1)

```text
T025 admission V0 tests      T026 atomic append tests      T027 request key tests
T028 rejection audit tests   T029 atomicity property       T030 conformance vectors
```

## Implementation strategy

1. **MVP (P1 stories)**: Phases 1 and 2, then US1, US3, US2. At that point a ledger with schema-driven merge and deterministic reads works; validate with SC-002, SC-003, SC-005 and SC-006.
2. **Increment**: add US4 and US5 (defaults, tombstones), then US7 and US8 (model errors, version change), then US6 (explain).
3. **Finish**: Polish; the worked example (T080) is the final acceptance gate for SC-001.

## Out of scope for this feature (deferred, see spec Assumptions)

Computed fields (including impure ones), deferred fields, dynamic defaults, per-field authority beyond the parsed `authority` check, snapshots, compaction, time-travel reads, redaction, the sandbox or CEL spike, and detailed schema evolution.

---

## Phase 12: Convergence

- [X] T087 CRITICAL: Pass a field validator only the value at its path, not the whole document: in `ports/py/src/aduku/engine/validate.py` build the field-rule `Subject` with an empty `document` (document rules keep the full document, which is their declared scope); add a test in `ports/py/tests/unit/test_validators.py` that a registered field validator cannot read a sibling field per Constitution VIII (contradicts)
- [X] T088 Export the public API from `ports/py/src/aduku/__init__.py` (`Aduku`, `SqliteStorage`, `MemoryStorage`, `Clock`, `IdSource`, `SchemaRegistry`, `load_schema`, and the model types the contract names) so `from aduku import Aduku, SqliteStorage, Clock, IdSource` works; add a test that runs that exact import, and use it in `ports/py/README.md` per contracts/library-api.md (missing)
- [X] T089 Add `SchemaRegistry.load(path_or_text, behaviors=None) -> Schema` in `ports/py/src/aduku/schema/registry.py`: accept a file path or schema text, fail closed with `SchemaError` listing every problem, return the schema without registering it; add a unit test in `ports/py/tests/unit/test_schema_registry.py` per contracts/library-api.md (missing)
