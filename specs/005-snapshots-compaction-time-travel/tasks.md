# Tasks: Snapshots, Time Travel, Compaction and Purge

**Input**: Design documents from `/specs/005-snapshots-compaction-time-travel/`

**Prerequisites**: plan.md, spec.md, research.md, data-model.md, contracts/, quickstart.md

**Tests**: INCLUDED and mandatory. Constitution Principle X (NON-NEGOTIABLE): tests are written and observed failing before the implementation they cover; the 16.1 and 16.2 vectors run unchanged throughout, with layers on and off.

**Organization**: Tasks are grouped by user story. All Python paths are under `ports/py/` (plan.md, Structure Decision); shared vectors live in `conformance/` at the repo root. Features 001 to 004 are implemented and are the base.

## Format: `[ID] [P?] [Story] Description`

- **[P]**: Can run in parallel (different files, no dependency on an incomplete task)
- **[Story]**: User story from spec.md (US1 to US5). Setup, Foundational and Polish tasks have no story label.

## Story map

| Story | Priority | Subject |
|---|---|---|
| US1 | P1 | Reads stay fast as the ledger grows (saved per-tier layers, watermarks, incremental update) |
| US2 | P1 | `rebuild` proves the saved layers are exact |
| US3 | P2 | Read a document as it was (`valid_time`, `as_of` by seq, tx_time, valid_time) |
| US4 | P2 | Compact old history without changing any answer (compaction, archive, expiry, verification) |
| US5 | P3 | Erase data with an auditable purge |

Build order follows dependencies: Foundational first (layer codec, entry fields, storage additions), then US1 and US2 (the P1 pair, which ship together because layers need their proof), then US3, US4 (needs US1 for layers and US3 for `as_of` inside a range) and US5 (needs US4 for archives and synthetic entries), then Polish. Each phase ends with the full suite green on both backends.

---

## Phase 1: Setup
- [X] T001 Extend `ports/py/tests/unit/test_purity_imports.py` (Principle III): assert `engine/layer_codec.py`, `engine/asof.py`, `engine/compaction.py` and `engine/purge.py` exist and are pure (no clock, no IO, no storage import); it fails until the modules exist (T006, T026, T040, T057)
- [X] T002 [P] Add `ports/py/tests/snapshot_helpers.py`: a service builder over both backends with a `layers` flag; `bulk_history(db, doc, n, tiers)` that writes n entries directly through the storage interface (fast, for 100,000-entry documents); `restart(db)` (a new `Aduku` on the same storage, empty in-memory caches); counters for ledger entries read and derivations; counting doubles for hooks and deferred sources; a fault injector that makes the Nth storage step of an operation raise
- [X] T003 [P] Extend `conformance/README.md` with the new cases `time-travel`, `compaction`, `purge` and `snapshots`, the steps and expectations of `contracts/conformance-vectors.md`, and the rule that every vector is also run with layers off
- [X] T004 [P] Add the new error codes `BAD_VALID_TIME`, `AS_OF_AMBIGUOUS`, `HISTORY_COMPACTED` (with `earliest_seq`), `MIXED_SCHEMA_RANGE`, `IDEM_KEY_MISSING`, `COMPACTION_CHECK_FAILED`, `LEDGER_CHANGED`, `PURGE_REASON_REQUIRED` and `UNKNOWN_ENTRY` (and the exception class `HistoryCompactedError` carrying `earliest_seq`) to `ports/py/src/aduku/errors.py` (the existing `NOT_AUTHORIZED`, `UNKNOWN_PATH`, `FIELD_NOT_WRITABLE` are reused), with a unit test `ports/py/tests/unit/test_new_codes.py`

---

## Phase 2: Foundational (blocks all stories)
Entry fields, the layer codec and the storage additions that every story reads.
- [X] T005 [P] Write failing tests `ports/py/tests/unit/test_layer_codec.py` and `ports/py/tests/property/test_layer_codec_roundtrip.py`: a layer with plain values, decimals, timestamps, lists, `TOMBSTONE` (`deleted::null`) and `Recreated` values encodes to canonical text sorted by path text, decodes to an equal layer, and the same layer always gives the same bytes (R-2)
- [X] T006 Implement `ports/py/src/aduku/engine/layer_codec.py` (pure): `encode_layer(layer) -> str` and `decode_layer(text) -> Layer`, the reserved annotation `recreated`, sorted by path text; make T005 pass
- [X] T007 [P] Write failing tests `ports/py/tests/unit/test_entry_fields.py` and `ports/py/tests/integration/test_storage_entry_fields.py` (both backends): `Entry.valid_time`, `Entry.synthetic`, `Entry.purged` default to `None`, `None`, `False`; a stored `valid_time` round-trips as an Ion timestamp; a database created before this feature (no new columns) opens and reads its entries unchanged
- [X] T008 Add `valid_time`, `synthetic`, `purged` to `Entry`, `valid_time` to `EntryInput`, and `AsOf`, `AsOfInfo`, `PurgedMark` types to `ports/py/src/aduku/model.py`; add the columns to `ports/py/src/aduku/storage/sqlite.py` (with `ALTER TABLE ... ADD COLUMN` when missing) and the fields to `storage/memory.py`; `op` accepts `layer` in storage only; make T007 pass
- [X] T009 [P] Write failing tests `ports/py/tests/integration/test_storage_layers.py` (both backends): `put_layers` stores and returns rows; replaces a row only for an equal or higher `tier_seq` of the same epoch; `drop_layers`; `tier_last_seq(doc, tier, through)`; `epoch`/`bump_epoch`; `entries(..., until_seq=)`; `seq_at_tx_time` (including a `tx_time` that does not grow with seq); `ViewRow.epoch` round-trips and defaults to 0
- [X] T010 Add `LayerRow`, `layers`/`doc_epoch` tables, `views.epoch`, and the `Tx` methods of `contracts/storage.md` (layers, epoch, `until_seq`, `seq_at_tx_time`) to `ports/py/src/aduku/storage/base.py`, `memory.py` and `sqlite.py`; make T009 pass
- [X] T011 Add the `ledger_rewrite` one-row table and extend the `entries_no_update`/`entries_no_delete` triggers in `ports/py/src/aduku/storage/sqlite.py` with the `effects_pruning` pattern (lifted only inside a rewriting operation, cleared before commit), plus insert-only triggers for `purge_log` and `compaction_sources`; test in `ports/py/tests/integration/test_ledger_triggers.py` that plain updates and deletes of entries still fail and that the flag is cleared after a failed transaction
- [X] T012 Add a `layers: bool = True` option to `Aduku` in `ports/py/src/aduku/service.py` (False: no layer is read or written; every result stays the same), used by the `restart(db)` helper of T002 (a new `Aduku` on the same storage, so no separate test hook is needed)

---

## Phase 3: User Story 1 - Reads stay fast as the ledger grows (Priority: P1)
**Goal**: saved per-tier layers with watermarks and incremental update. **Independent test**: US1 acceptance scenarios 1 to 6, SC-001, SC-002.
- [X] T013 [P] [US1] Write failing tests `ports/py/tests/unit/test_state_from_layers.py`: `DerivationState` to layer rows and back is equal; a state continued from saved layers folds only entries after each tier's `tier_seq`; the fit rules of R-3 (version, fingerprint, epoch, `tier_seq` mismatch, view watermark) accept and refuse as specified (FR-002)
- [X] T014 [US1] Implement `layers_of(state, schema)` and `state_from_layers(rows, view, schema, last)` in `ports/py/src/aduku/engine/derive.py` (pure, using `layer_codec`); make T013 pass
- [X] T015 [P] [US1] Write failing property test `ports/py/tests/property/test_layers_equal_replay.py` (both backends, dev 50 and CI 1,000 examples): random histories (tiers, deletes, re-creations, a computed field, a model-error rule, several schema versions) read through saved layers, after a restart, after appends at one tier and with some layers deleted equal the document, model errors, status and `explain` of a ledger-only replay (FR-004, SC-002)
- [X] T016 [P] [US1] Write failing tests `ports/py/tests/integration/test_snapshot_reads.py` (both backends): US1 scenario 1 (after one entry at `user`, the rows of the other tiers are byte-identical and unread), 2 (a restarted service reads no entry below the watermarks), 3 (a rules change makes layers unused and they are saved again), 4 (missing, damaged, and unwritable layers: reads correct, writes accepted, no rejection), 5 (a lagging tier is folded in), 6 (a rejected request changes no layer, view or watermark) (FR-001 to FR-007)
- [X] T017 [P] [US1] Write failing test `ports/py/tests/integration/test_snapshot_performance.py`: a 100,000-entry document over three tiers; the first write after a restart, the read after it, and the first read after a restart each take at most 5 times the same step on a 100-entry document (SC-001), the write reading only the saved layers and the entries after their watermarks, and read at most the entries after the watermarks
- [X] T018 [US1] Wire saved state into `ports/py/src/aduku/service.py`: `_load` reconstructs the state from the layer rows and the view when they fit (R-3) instead of returning `None`; `append` saves the touched tiers' rows in its commit transaction, and all tiers for a document that has none (R-4); a failure is caught and logged; `layers=False` bypasses both; structured log entries for a layer repair; make T015 to T017 pass
- [X] T019 [US1] Make `layers_of` in `ports/py/src/aduku/engine/derive.py` and the save in `service.py` set `tier_seq` correctly for tiers whose entries are unusable (unknown tier, derived field, unmergeable value): skipped seqs stay in the view notes and the layer still covers them; extend T016 with a document holding such entries

---

## Phase 4: User Story 2 - `rebuild` proves the saved layers are exact (Priority: P1)
**Goal**: rebuild compares and repairs layers. **Independent test**: US2 acceptance scenarios 1 to 5, SC-003.
- [X] T020 [P] [US2] Write failing tests `ports/py/tests/integration/test_rebuild_layers.py` (both backends): correct layers report no difference and stay byte-identical; an altered layer, a wrong `tier_seq` and an altered view are each reported as `(doc, tier)` or the view and repaired; a missing layer or one of another schema version is refreshed, not a difference; a document that cannot be derived is reported and the others are still checked; a second rebuild reports nothing; the ledger is byte-identical; zero hook and source calls (FR-008 to FR-010, SC-003)
- [X] T021 [US2] Extend `rebuild` in `ports/py/src/aduku/service.py` and `RebuildReport` in `ports/py/src/aduku/model.py` (`layers_differing`, `layers_refreshed`) to encode the derived layers and compare them byte for byte with the saved rows, repairing only when nothing was appended meanwhile (R-5); make T020 pass
- [X] T022 [P] [US2] Extend `ports/py/tests/golden/test_worked_example.py` and the replay test (SPEC 16 item 6) so a rebuild of the 16.1 history reproduces the saved layers bit for bit
- [X] T023 [P] [US2] Write failing tests `ports/py/tests/integration/test_cli_rebuild_layers.py` and update `ports/py/src/aduku/cli.py` so `rebuild` prints the layers that differed or were refreshed
- [X] T024 [P] [US2] Write the vector `conformance/snapshots.ion` (a long history read, appended to, restarted and read; a damaged layer repaired by rebuild) and the runner steps `restart` and `rebuild` in `ports/py/tests/conformance_runner.py`; run all existing vectors with layers on and off (SC-008)

---

## Phase 5: User Story 3 - Read a document as it was (Priority: P2)
**Goal**: `valid_time` on entries and `as_of` reads. **Independent test**: US3 acceptance scenarios 1 to 8, SC-004.
- [X] T025 [P] [US3] Write failing tests `ports/py/tests/unit/test_asof_select.py`: seq prefix, a seq beyond the last and before the first, `tx_time` resolution with a `tx_time` that does not grow with seq, `valid_time` filtering (entries without it always included), `inside_request`, exactly-one-point rule (FR-013 to FR-016)
- [X] T026 [US3] Implement `ports/py/src/aduku/engine/asof.py` (pure): `select(entries, asof, seq_for_tx) -> (entries, AsOfInfo)`; make T025 pass
- [X] T027 [P] [US3] Write failing tests `ports/py/tests/unit/test_request_hash_valid_time.py`: the hash of a request without `valid_time` equals pinned digests computed with Feature 1's code (including every retry vector), and with `valid_time` it differs and is stable (FR-012)
- [X] T028 [US3] Add `valid_time` to the entry struct in `ports/py/src/aduku/engine/request_hash.py` only when not null; make T027 pass
- [X] T029 [P] [US3] Write failing tests `ports/py/tests/integration/test_valid_time_admission.py` (both backends): a timezone-aware `valid_time` is stored and listed; a naive one, a string or a number rejects the whole request with `BAD_VALID_TIME` naming the entry and path, never the value, and records nothing; a contributor cannot submit `op="layer"` or any operation other than `set` and `delete` (FR-035: there is no way to purge through a contribution); `valid_time` never changes a normal read, ordering or validation (FR-011)
- [X] T030 [US3] Check `valid_time` in `ports/py/src/aduku/engine/admission.py` (stage V0) and store it in `service._stored`; contributors cannot submit `op="layer"`; make T029 pass
- [X] T031 [P] [US3] Write failing property test `ports/py/tests/property/test_as_of_history.py`: for every seq of random histories, `get(as_of=seq)` equals the live document returned when that entry was committed, with the same model errors (FR-014, SC-004)
- [X] T032 [P] [US3] Write failing tests `ports/py/tests/integration/test_as_of_reads.py` (both backends): scenarios 1 to 4, 6 and 8, including `explain(as_of)`, `resolved_seq` and `included`, that an `as_of` read writes no view, layer or derived row, and that after a schema version change an `as_of` read uses the document's current version (FR-020)
- [X] T033 [P] [US3] Write failing tests `ports/py/tests/integration/test_as_of_isolation.py`: no hook or deferred source is called for any point; an impure compute uses its recorded outcome, and where none exists the field gets the model error `NO_RECORDED_OUTCOME`; deferred fields are listed `unavailable` with no value (FR-017, FR-018, scenario 5)
- [X] T034 [US3] Implement `get(..., as_of=)` and `explain(..., as_of=)` in `ports/py/src/aduku/service.py`: select the entries, replay under the current schema without the state caches and without writing, build the effect view from recorded outcomes only, list deferred fields as `unavailable`, and return `DocumentResult.as_of`; `AS_OF_AMBIGUOUS` for zero or several points (FR-013, FR-019, FR-020); make T031 to T033 pass
- [X] T035 [P] [US3] Write failing tests `ports/py/tests/integration/test_cli_as_of.py` and update `ports/py/src/aduku/cli.py`: `get --as-of`, `explain --as-of`, `append --valid-time`, `entries` showing `valid_time`; a repeated `--as-of` flag, or none of `seq=`, `tx=`, `valid=`, is `AS_OF_AMBIGUOUS`
- [X] T036 [P] [US3] Write the vector `conformance/time-travel.ion` (the 16.1 history at every seq, `tx_time` between commits, valid-time cases, before the first and beyond the last, ambiguity, bad `valid_time`, pinned hash, impure and deferred at a point) and the runner steps `as_of` and the expectations `resolved_seq`, `included`, `refused`

---

## Phase 6: User Story 4 - Compact old history without changing any answer (Priority: P2)
**Goal**: compaction, archive, expiry, verification. **Independent test**: US4 acceptance scenarios 1 to 9, SC-005, SC-006.
- [X] T037 [P] [US4] Write failing property test `ports/py/tests/property/test_layer_op_equivalence.py`: for every built-in merge behavior and a custom associative one, folding the originals of a tier equals folding the single `layer` entry made from their layer (Principle V, FR-021)
- [X] T038 [US4] Support `op="layer"` in `ports/py/src/aduku/engine/squash.py` (`entry_layer` returns the decoded layer) and make `fold_entries` skip purged entries in `engine/derive.py`; make T037 pass
- [X] T039 [P] [US4] Write failing tests `ports/py/tests/unit/test_compaction_plan.py`: the watermark moves down to a request boundary; a range with two schema versions is refused `MIXED_SCHEMA_RANGE`; a second compaction whose sources include earlier synthetic entries on a keyed non-idempotent field is not refused `IDEM_KEY_MISSING` (synthetic sources count as keyed); a non-idempotent field with an entry lacking `idem_key` is refused `IDEM_KEY_MISSING`; one synthetic entry per tier with the highest source seq and tx_time; sources, idem keys and request index are listed; nothing in range is a no-op; the comparison of two derivations names the first differences; the id mapping and the input/shadowed rule of R-9
- [X] T040 [US4] Implement `ports/py/src/aduku/engine/compaction.py` (pure): `plan(entries, schema, policy, now)`, `compacted_ledger(plan, entries)`, `compare(before, after, mapping)`; make T039 pass
- [X] T041 [P] [US4] Write failing tests `ports/py/tests/unit/test_replay_bases.py`: `replay` with a compaction base after a synthetic group gives the same errors, attribution (`introduced_by_request`, `introduced_by_schema_version`) and skipped seqs as a replay of the originals, including a schema version change after the compaction (R-8, R-10)
- [X] T042 [US4] Extend `replay` in `ports/py/src/aduku/engine/model_errors.py` to take compaction bases (`request_id -> errors, skipped`) and seed them after a synthetic group; add `Schema.fold_fingerprint` to `ports/py/src/aduku/schema/model.py`; make T041 pass
- [X] T043 [P] [US4] Write failing tests `ports/py/tests/unit/test_explain_compacted.py`: `explain` over the compacted ledger treats a synthetic entry as one entry (input if a source was, else shadowed) and lists `sources`; over originals merged back from the archive it is exactly the old answer; `originals_kept` is set; a purged-free history only
- [X] T044 [US4] Update `ports/py/src/aduku/engine/explain.py` and `Explanation` in `model.py` (`originals_kept`, `sources`) for synthetic entries and archive expansion; make T043 pass
- [X] T045 [P] [US4] Write failing storage tests `ports/py/tests/integration/test_storage_compaction.py` (both backends): `apply_compaction` archives, removes, inserts synthetic entries, records the compaction (with `first_tx_time`, `last_tx_time` and `has_valid_time`), sources, request index and retired keys; a second compaction records the first one's synthetic entries as sources and expanding them recurses to the originals and bumps the epoch in one step; an injected failure at each step leaves everything unchanged; `request_entries` and `idem_key_seen` read the new tables; `expire_archive` removes only archive rows and records the expiry; the effect log, derived store and audit log are untouched (FR-025 to FR-027)
- [X] T046 [US4] Implement the compaction storage methods of `contracts/storage.md` in `ports/py/src/aduku/storage/base.py`, `memory.py` and `sqlite.py` (tables `compactions`, `compaction_sources`, `archive_entries`, `request_entries`, `retired_idem_keys`, `archive_expiry`; archive insert-only apart from expiry); make T045 pass
- [X] T047 [P] [US4] Write failing property test `ports/py/tests/property/test_compaction_invariance.py` (both backends, CI profile 1,000): on random histories (tombstones, re-creations, a keyed counter-style field, model errors, several tiers), compact at random watermarks; document, model errors with attribution, status, skipped, conflicts and, with the archive kept, every `explain` answer are unchanged; after expiry `explain` follows R-9; a layers-off run is identical (SC-005)
- [X] T048 [P] [US4] Write failing tests `ports/py/tests/integration/test_compaction_service.py` (both backends): scenarios 1 to 9 of US4, a second compaction of an already compacted document (keyed counter field, no `IDEM_KEY_MISSING`, document and `explain` unchanged while the archive is kept), including 10,000 entries on 20 fields in 3 tiers leaving at most 60 entries (SC-006), a repeated idem key still `DUPLICATE_IDEM_KEY`, a retry returning the original `(id, seq)` list, writes during a compaction kept in order after the watermark, `LEDGER_CHANGED`, and `list_entries` showing synthetic entries
- [X] T049 [P] [US4] Write failing tests `ports/py/tests/integration/test_compaction_check_refuses.py`: a deliberately wrong plan (a changed value, a dropped tombstone, a dropped idem key, a wrong base error) is refused `COMPACTION_CHECK_FAILED` naming the first differences and nothing changes (FR-023)
- [X] T050 [P] [US4] Write failing tests `ports/py/tests/integration/test_history_compacted.py` (both backends): `as_of` inside a compacted range is answered from the archive while kept (seq, `tx_time` and `valid_time`) and, after expiry, a seq, `tx_time` or `valid_time` inside the range is refused `HISTORY_COMPACTED` with `earliest_seq` after expiry, while a seq at or above the watermark, a `tx_time` below `first_tx_time` or at or above `last_tx_time`, and a `valid_time` when `has_valid_time` is false are still answered exactly; a schema version with a different `fold_fingerprint` derives from the archive while kept and is refused on read, activate and pin after expiry; a validation-only version reuses the synthetic entries; `verify_archive` reports identical, and a difference after a tampered archive row (FR-028, FR-029)
- [X] T051 [US4] Implement `compact`, `verify_archive` and `expire_archive` in `ports/py/src/aduku/service.py`: derive at the watermark from the originals and from the compacted ledger in memory, compare, then one transaction that re-checks the ledger digest and epoch (`LEDGER_CHANGED`) and applies the plan; rewrite the layers and the view with the new epoch; effective-ledger selection of R-10 for reads, `rebuild`, `explain` and `as_of`; refuse `schemas.activate/pin` as specified; structured logs for compaction and expiry without values; make T047 to T050 pass
- [X] T052 [P] [US4] Write failing tests `ports/py/tests/integration/test_cli_compaction.py` and update `ports/py/src/aduku/cli.py`: `compact`, `archive verify`, `archive expire`, `nothing to compact`, `entries` showing synthetic entries
- [X] T053 [P] [US4] Write the vector `conformance/compaction.ion` (watermarks on and inside a request, tombstones and re-creations, keyed counter field, missing keys, mixed versions, retried request, attribution unchanged, `as_of` inside a range before and after expiry, a wrong compaction refused) and the runner steps `compact` and `expire` with the before/after comparison

---

## Phase 7: User Story 5 - Erase data with an auditable purge (Priority: P3)
**Goal**: purge across every store, with a purge log. **Independent test**: US5 acceptance scenarios 1 to 8, SC-007.
- [X] T054 [P] [US5] Write failing tests `ports/py/tests/unit/test_purge_schema.py`: `purge_by` loads (tiers and roles), an unknown name is `UNKNOWN_TIER`, absent or empty means nobody, it is part of the fingerprint, and `schema check` prints it
- [X] T055 [US5] Add `purge_by` to the loader and to `Schema` in `ports/py/src/aduku/schema/loader.py` and `model.py`, and to `schema check` in `cli.py`; make T054 pass
- [X] T056 [P] [US5] Write failing tests `ports/py/tests/unit/test_purge_plan.py`: a path scope matches entries at or below it, optionally one tier; listed entry ids; a struct above the path has the part removed and keeps the rest; a synthetic entry is re-folded from remaining sources, or its layer paths dropped when the sources are expired; downstream fields through the dependency graph; `UNKNOWN_ENTRY`, a derived path, an undeclared path
- [X] T057 [US5] Implement `ports/py/src/aduku/engine/purge.py` (pure): `plan(entries, archive, compactions, schema, scope) -> PurgePlan`; make T056 pass
- [X] T058 [P] [US5] Write failing tests `ports/py/tests/integration/test_storage_purge.py` (both backends): `apply_purge` erases values, meta, valid_time and request hashes of entries, archive rows and synthetic layers, removes the doc's layers and view and bumps the epoch, removes effect-log and derived rows of downstream fields, scrubs rejection-audit values, appends one purge-log row, all in one step with an injected failure at each step changing nothing; the purge log accepts inserts only
- [X] T059 [US5] Implement `apply_purge`, `purge_log` and the audit scrub (`redact` in `ports/py/src/aduku/audit.py`) in `ports/py/src/aduku/storage/base.py`, `memory.py` and `sqlite.py` (table `purge_log`, insert-only trigger; effect-log rows are deleted through the existing `effects_pruning` flag, inside the same transaction); make T058 pass
- [X] T060 [P] [US5] Write failing property test `ports/py/tests/property/test_purge_equals_derivation.py`: after purging random scopes, the document and model errors equal a derivation of the ledger with the erased contributions removed, a second purge of the same scope changes nothing, `rebuild` reports no difference (FR-032, FR-037)
- [X] T061 [P] [US5] Write failing tests `ports/py/tests/integration/test_purge_service.py` (both backends): scenarios 1 to 8 of US5; no contribution can carry a purge (FR-035); a marker value placed in contributions at three tiers, a computed field, an impure compute (its record erased), a deferred value, a struct write above the path, compaction with the archive kept and expired, a rejected attempt in the audit log, and a retried request key, then every table scanned for the marker finds nothing (SC-007); unauthorized and reason-less purges change nothing; `explain` shows the purge marks and never lists a purged entry as winner or input; no log line, error or purge-log field contains the marker
- [X] T062 [US5] Implement `purge` and `purge_log` in `ports/py/src/aduku/service.py`: check `purge_by` and the reason, plan, run `apply_purge`, drop the in-memory state and view caches, log the act without values; a retry with an erased hash is a duplicate (`_known_request`); `explain` fills `Explanation.purged`; make T060 and T061 pass
- [X] T063 [P] [US5] Write failing tests `ports/py/tests/integration/test_cli_purge.py` and update `ports/py/src/aduku/cli.py`: `purge`, `purge-log`, required `--reason`, counts printed, no value printed
- [X] T064 [P] [US5] Write the vector `conformance/purge.ion` (scopes, authority, reasons, explain marks, retried request, `rebuild` clean) and the runner step `purge` with the expectations `absent_everywhere`, `purge_log_count`

---

## Phase 8: Polish and cross-cutting
- [X] T065 Update `docs/SPEC.md` (Draft 0.12): 4.1 (`valid_time` already there; add `layer` op and `purged`), 4.2 item 1 (compaction and purge as the only exceptions), 12.2 (`as_of` points, results, `HISTORY_COMPACTED`), 12.3 (purge marks, synthetic entries), 12.4 (`compact`, `verify_archive`, `expire_archive`, `purge`, `purge_log`), 15.1 to 15.4 (layers, epoch, archive, purge log, time), 16 item 6 (layers), 14 change-class row for `purge_by`, and the changelog
- [X] T066 Amend `.specify/memory/constitution.md` Principle I to name compaction and purge as the only operations that change existing entries, recorded and verified, and bump to 1.5.0 with the rationale in the sync report; check the plan, spec and SPEC agree
- [X] T067 [P] Update `docs/notes.md` (decisions with reasons: layers beside the view row, `layer` op, compaction bases, verification rule, purge scope and retry hashes, deferred items), `README.md`, `ports/py/README.md`, `CLAUDE.md` and mark Feature 5 done in `docs/prompts/specify-features.md`
- [X] T068 [P] Add `ports/py/examples/demo_history.py` (snapshots, `as_of`, compaction, purge) and run it in a smoke test
- [X] T069 Run `uv run ruff format`, `uv run ruff check`, `uv run mypy src` and the full suite on both backends; run the property suites with the CI profile (1,000 examples) three times to check for flaky failures; run every vector with layers on and off (SC-008)
- [X] T070 Walk through `specs/005-snapshots-compaction-time-travel/quickstart.md` by hand against a temporary database and fix any drift between it, the contracts and the code

---

## Phase 9: Analysis fixes (after `/speckit-analyze` on the implementation)
- [X] T071 Resolve I1: reword FR-009, US2 scenario 5 and SPEC 12.4 (`rebuild` calls no deferred source and no impure hook while outcomes cover the inputs; a missing outcome is filled once) in `spec.md` and `docs/SPEC.md`
- [X] T072 Resolve G1: add `test_rebuild_calls_no_hook_and_no_source_while_the_recorded_outcomes_cover_the_inputs` and `test_rebuild_fills_a_missing_impure_outcome_once_and_never_calls_a_source` to `ports/py/tests/integration/test_rebuild_layers.py`
- [X] T073 Resolve S1: reword FR-031 and US5 in `spec.md` to the attempted values at the erased paths in the rejection audit records
- [X] T074 Resolve V1: add `conformance/compaction-versions.ion`, the `explain` steps to `compaction.ion` and `purge.ion` and a struct-above-the-path case; runner support for `activate_schema` refusals and `purged_entries`; make `contracts/conformance-vectors.md` and `conformance/README.md` say exactly what the vectors hold and what stays Python-only
- [X] T075 Resolve T1: say in `test_snapshot_performance.py` that the entries-read count carries the guarantee, not the 50 ms floor
- [X] T076 Resolve T2: add `ports/py/tests/integration/test_operation_faults.py` (an injected failure at each step of a compaction, an expiry and a purge changes nothing, and the retry succeeds; a layer that cannot be saved does not fail a compaction)
- [X] T077 Resolve C1: record the Principle X deviation in `plan.md` Complexity Tracking and run the twelve-mutation check; add the epoch test the one survivor called for


---

## Phase 10: Convergence
- [X] T078 Print `purged` (a list of entry id, seq, when and why), `originals_kept` (`kept`, `expired` or null) and `sources` in the `explain` output of `ports/py/src/aduku/cli.py`, with a failing test first in `ports/py/tests/integration/test_cli_purge.py` and `test_cli_compaction.py` per contracts/cli.md ("`explain` prints purged marks and, for a synthetic entry, `originals: kept|expired` and its sources") and FR-040 (partial)
- [X] T079 Document the `request_effects` table (a compacted request's added and resolved model errors, kept so a retry is answered as before) and the `request_effects` storage call in `specs/005-snapshots-compaction-time-travel/data-model.md` and research R-8, which the code needs for FR-026 but the design documents only mention in `contracts/storage.md` per plan: data-model (unrequested)
- [X] T080 Make the `purge_by` line of `aduku schema check` and `contracts/schema-format.md` agree: the output is the JSON list `purge_by`, empty meaning nobody, not the text `purge_by: nobody`; add a case to `ports/py/tests/integration/test_cli.py` per contracts/schema-format.md (partial)

---

## Phase 11: Convergence
- [X] T081 Make the `purge_log` and `archive_expiry` descriptions in `specs/005-snapshots-compaction-time-travel/data-model.md` match the stored tables (`purge_log(id, doc, scope, reason, actor, at, counts)` where `counts` is the canonical struct of per-store counts and the erased entry ids that `explain` uses for its purge marks, no `layers_rewritten`; `archive_expiry(id, doc, through_seq, at, expired)`) per plan: data-model (partial)
- [X] T082 Remove the unused `n INTEGER` column from the `purge_log` table in `ports/py/src/aduku/storage/sqlite.py`, with the storage purge test in `ports/py/tests/integration/test_storage_purge.py` still passing, per plan: data-model (unrequested)

---

## Phase 12: Code review fixes (after `/code-review`)
- [X] T083 Fix review 1: a purge limited to a tier no longer drops the path from the expired synthetic layers of other tiers (`engine/purge.py`, test in `tests/unit/test_purge_plan.py`)
- [X] T084 Fix review 2: a purge by entry id reaches a synthetic layer that cannot be folded again (rules that fold differently), using the path and tier each erased placeholder keeps (`engine/purge.py`, test in `tests/unit/test_purge_plan.py`)
- [X] T085 Fix review 3: a cached state is stamped with the epoch it was derived in, never the epoch seen when it is remembered (`service.py`, test in `tests/integration/test_purge_service.py`, checked to fail against the old behavior)
- [X] T086 Review 4 and 5 kept by design and recorded in `docs/notes.md` (a purged request key is a duplicate whatever its payload; an expired range refuses `valid_time` reads whenever it held one)
- [X] T087 Fix review 6: a compaction is refused (`LEDGER_CHANGED`) if the effect log gained an outcome during its check (`service.py`, test in `tests/integration/test_compaction_service.py`)
- [X] T088 Fix review 7: `explain` at a past point reports `originals_kept` like a live `explain` (`service.py`, test in `tests/integration/test_purge_service.py`)

