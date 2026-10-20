# Tasks: Schema Versioning and Evolution

**Input**: Design documents from `/specs/006-schema-evolution/`

**Prerequisites**: plan.md, spec.md, research.md, data-model.md, contracts/, quickstart.md

**Tests**: INCLUDED and mandatory. Constitution Principle X (NON-NEGOTIABLE): tests are written and observed failing before the implementation they cover (run each test task and see it fail before its implementation task); the 16.1 and 16.2 vectors and those of Features 1 to 5 run unchanged throughout.

**Organization**: Tasks are grouped by user story. All Python paths are under `ports/py/` (plan.md, Structure Decision); shared vectors live in `conformance/` at the repo root. Features 001 to 005 are implemented and are the base.

## Format: `[ID] [P?] [Story] Description`

- **[P]**: Can run in parallel (different files, no dependency on an incomplete task)
- **[Story]**: User story from spec.md (US1 to US4). Setup, Foundational and Polish tasks have no story label.

## Story map

| Story | Priority | Subject |
|---|---|---|
| US1 | P1 | Versions stay immutable and moving between them is a recorded act (shared state, fingerprints, history, rollback) |
| US2 | P1 | See how risky a change is, and what it would do, before activating it (classification, dry run, acknowledgement, eager rebuild) |
| US3 | P2 | Hold a document on its version and move it when ready (pins, migration) |
| US4 | P2 | Know why an entry is not part of the document (shadow reasons) |

Build order follows dependencies: Foundational first (fingerprints, values, the pure shadow and fold pieces, the storage tables), then US1 (everything else writes through its state), US4 (the dry run counts shadowed entries), US2, US3 and Polish. Each phase ends with the full suite green on both backends.

---

## Phase 1: Setup
- [X] T001 Extend `ports/py/tests/unit/test_purity_imports.py` (Principle III): assert `schema/diff.py`, `engine/shadow.py` and `engine/dryrun.py` exist and are pure (no clock, no IO, no storage import, `datetime` only as a type where named); it fails until the modules exist (T032, T010, T034)
- [X] T002 [P] Add `ports/py/tests/schema_helpers.py`: build a schema text from a field list, make two services on one storage that share a registry file set, a `restart(db)` that builds a new `Aduku` and registry on the same storage, `versions(*texts)` that loads several versions, and counters for hook and source calls
- [X] T003 [P] Extend `conformance/README.md` with the new cases `schema-changes`, `schema-dry-run`, `schema-shadow` and `schema-pins`, the steps and expectations of `contracts/conformance-vectors.md`, and the rule that the vectors of Features 1 to 5 run unchanged
- [X] T004 [P] Add the error codes `SCHEMA_VERSION_CHANGED` (a `SchemaError` code), `BREAKING_CHANGE_NOT_ACKNOWLEDGED`, `SCHEMA_STATE_CHANGED` and `NOT_PINNED` to `ports/py/src/aduku/errors.py`, with a unit test `ports/py/tests/unit/test_schema_codes.py`

---

## Phase 2: Foundational (blocks all stories)
Values, fingerprints, the pure engine pieces and the storage tables that every story reads.
- [X] T005 [P] Write failing tests `ports/py/tests/unit/test_schema_fingerprint.py`: `Schema.content_fingerprint` is the same for equal content and differs for any change; `fingerprint_parts` is `{top, fields}` with a digest per field pattern; a changed field changes only its own digest and the top digest changes for a top-level setting (R-2)
- [X] T006 Add `content_fingerprint` and `fingerprint_parts` to `Schema` in `ports/py/src/aduku/schema/model.py` and a `differing_parts(old, new, limit=5)` helper; make T005 pass
- [X] T007 [P] Write failing tests `ports/py/tests/unit/test_schema_values.py` for `Change`, `ChangeList`, `Shadowed`, `HistoryRecord`, `DocumentChange`, `DryRunReport`, `ActivationReport`, `MigrationReport` (frozen, defaults, ordering of a change list by `(path, kind)`, overall class is the worst, empty is compatible)
- [X] T008 Add those values to `ports/py/src/aduku/model.py` per `data-model.md`; make T007 pass
- [X] T009 [P] Write failing tests `ports/py/tests/unit/test_shadow_reasons.py`: one skipped entry of each reason (`tier_not_in_schema`, `now_derived` with the field kind, `no_longer_conforms` with the type, `cannot_be_merged` with the rule and, for an undeclared path, `undeclared path`), the order of the checks, a struct write checked child by child, and no reason for an entry that is used (FR-018, R-4)
- [X] T010 Implement `ports/py/src/aduku/engine/shadow.py` (pure): `shadow_reasons(schema, behaviors, prepared, entries, skipped) -> list[Shadowed]`; make T009 pass
- [X] T011 [P] Write failing tests `ports/py/tests/unit/test_fold_conformance.py`: an entry written under version 1 whose value no longer conforms to the type in version 2 is skipped when folded under 2; an entry written under the version in force is never checked; a struct written to a field-wise field is checked per child; folding under the version that wrote the entries skips none (R-4)
- [X] T012 Add the conformance rule to `fold_entries` in `ports/py/src/aduku/engine/derive.py` (only for entries whose `schema_version` differs from the schema's); run the whole existing suite unchanged to prove no earlier result moved; make T011 pass
- [X] T013 [P] Write failing tests `ports/py/tests/integration/test_storage_schema_state.py` (both backends): `apply_schema_change` writes state, fingerprints and a history record together and bumps the generation by one; a stale `expect_generation` raises `ConstraintError` and changes nothing; `served_versions`, `schema_state`, `schema_generation` and `schema_history` read them back; an injected failure inside leaves everything as before; the history accepts inserts only (SQLite trigger) and rolls back with its transaction
- [X] T014 Add `StateChange`, the `Tx` methods of `contracts/schema-state.md` and the tables `schema_versions`, `schema_state`, `doc_pins` and `schema_history` to `ports/py/src/aduku/storage/base.py`, `memory.py` and `sqlite.py`; make T013 pass
- [X] T015 Add a `schema_state: bool = True` option to `Aduku` in `ports/py/src/aduku/service.py` (False keeps the registry in-process only, for tests of the old behavior) and note it in `ports/py/README.md`

---

## Phase 3: User Story 1 - Versions stay immutable and moving between them is recorded (Priority: P1)
**Goal**: durable shared state, fingerprints, history, rollback. **Independent test**: US1 scenarios 1 to 6, SC-003, SC-004.
- [X] T016 [P] [US1] Write failing tests `ports/py/tests/unit/test_registry_binding.py` with an in-memory fake `SchemaState`: an unbound registry behaves exactly as before; a bound one reads `active()` and `version_for(doc)` through the state, reloads only when the generation changed, writes `activate`, `pin` and `unpin` through it, and adopts its own active version and pins when the store is empty (bootstrap) but lets stored state win otherwise
- [X] T017 [US1] Add the `SchemaState` protocol, `bind()` and the generation-checked, write-through behavior to `ports/py/src/aduku/schema/registry.py`; make T016 pass
- [X] T018 [P] [US1] Write failing tests `ports/py/tests/integration/test_schema_state.py` (both backends): two services on one storage see each other's activation and pin on their next call; a restart with identical version content is accepted; a restart with changed content under a served number raises `SCHEMA_VERSION_CHANGED` naming the differing fields and serves nothing; a registered but never activated version can still be changed; a stored active version missing from the registry makes reads and writes fail with `SCHEMA_INACTIVE` naming it; one generation read per request and no reload without a change (counted)
- [X] T019 [US1] Implement `ports/py/src/aduku/schema_state.py` (the adapter of `SchemaState` over a `Storage`, recording fingerprints at first activation or pin) and call it from `Aduku.__init__`; make T018 pass
- [X] T020 [US1] Gate: run the whole existing suite (Features 1 to 5, unchanged) with the shared state on by default; list every test that relied on a process choosing its own active version or pins (several services on one storage with different registries, an activation made on a registry the service shares with others), and for each either correct the test's setup or give it `schema_state=False`; record the list and the decision in `research.md` R-1; no test may be weakened and no assertion about a document may change (FR-021, A1)
- [X] T021 [P] [US1] Write failing tests `ports/py/tests/integration/test_schema_history.py`: activate, rollback, pin, unpin and bootstrap each leave one history record (operator, versions, class, time); activating the active version records nothing; a failure inside an activation leaves the active version and the history as before (injected storage fault); `schema_history(limit=, doc=)` filters
- [X] T022 [US1] Implement `activate_schema` (without the acknowledgement and the dry run yet), `pin_document`, `unpin_document` and `schema_history` in `ports/py/src/aduku/service.py`, with the operator required (`ValueError`) and structured logs for each act; make T021 pass
- [X] T023 [P] [US1] Write failing property test `ports/py/tests/property/test_rollback_is_byte_identical.py` (both backends, CI profile 1,000): on random ledgers and two versions, activating the second and then the first again gives every document, model errors and skipped list byte-identical to before, with the ledger and its layers untouched (SC-003)
- [X] T024 [P] [US1] Write failing tests `ports/py/tests/integration/test_request_under_one_version.py`: a request admitted while an activation happens carries one version on every entry; entries admitted before keep their version after the activation (FR-005, R-8)
- [X] T025 [US1] Make `append` take `version_for(doc)` once and use that schema for admission and for every entry's `schema_version` (if it does not already); make T023 and T024 pass

---

## Phase 4: User Story 4 - Know why an entry is not part of the document (Priority: P2)
Placed before story 2 because the dry run counts shadowed entries per reason. **Goal**: reasons with the document, in `explain` and in the CLI. **Independent test**: US4 scenarios 1 to 6.
- [X] T026 [P] [US4] Write failing tests `ports/py/tests/integration/test_shadowed_reads.py` (both backends): `get` returns `shadowed` with a reason for each skipped seq under a version that shadows an entry of each kind; reading under the version that wrote them shows none; `explain` lists the shadowed entries that touch the path with their reasons in `shadowed_by_schema` (the Feature 001 field `shadowed` keeps its meaning) and never as winner or input; the seqs in `skipped` are unchanged
- [X] T027 [US4] Add `shadowed` to `DocumentResult` and `shadowed_by_schema` to `Explanation` (worked out at the public read boundary from `shadow_reasons`, not in `_core`) in `ports/py/src/aduku/service.py` and `engine/explain.py`; make T026 pass
- [X] T028 [P] [US4] Write failing tests `ports/py/tests/integration/test_cli_shadowed.py`: `get` prints `shadowed` beside `skipped`, `explain` prints the shadowed entries for the path
- [X] T029 [US4] Update `ports/py/src/aduku/cli.py` per `contracts/cli.md`; make T028 pass
- [X] T030 [P] [US4] Write the vector `conformance/schema-shadow.ion` (one entry of each reason read under a shadowing version, and none under the version that wrote them) and the runner expectation `shadowed` in `ports/py/tests/conformance_runner.py`

---

## Phase 5: User Story 2 - See how risky a change is and what it would do before activating it (Priority: P1)
**Goal**: classification, the dry run, the acknowledgement, the eager rebuild. **Independent test**: US2 scenarios 1 to 8, SC-001, SC-002, SC-005.
- [X] T031 [P] [US2] Write failing tests `ports/py/tests/unit/test_schema_diff.py`: one case per row of the table in `contracts/changes.md` (kind, path, class), several changes at once with the overall class, an unclassified difference listed as visible and marked, a renamed field as a removal and an addition, the same list in the same order for the same two versions, identical versions give an empty compatible list, and no description holds a stored value
- [X] T032 [US2] Implement `ports/py/src/aduku/schema/diff.py` (pure) with the table and `schema_changes(old, new)`; make T031 pass
- [X] T033 [P] [US2] Write failing tests `ports/py/tests/unit/test_dry_run_compare.py`: two derivations of one document compare to `DocumentChange` (document differs, model errors added and resolved by identity, skipped before and after, shadowed by reason, would call a hook, failed with a code)
- [X] T034 [US2] Implement `ports/py/src/aduku/engine/dryrun.py` (pure); make T033 pass
- [X] T035 [P] [US2] Write failing property test `ports/py/tests/property/test_dry_run_predicts_the_read.py` (both backends, CI profile 1,000): on random ledgers over two versions that differ in a mix of changes, every document's reported change equals the difference between the document read now and the one read after `activate_schema`; the dry run changes no stored byte (layers, views, derived values, history, ledger) and makes no hook or source call (SC-002)
- [X] T036 [P] [US2] Write failing tests `ports/py/tests/integration/test_schema_plan.py` (both backends): scenarios 3 and 4 and 8 of story 2 (counts, `examined` and `left_out`, a `limit` picks the same documents each time and is ordered by `sha256(doc)`, a list of documents, pinned documents left out and counted, a document with no entries unchanged, a document that needs an impure call is `would_call_hook`, a document whose history cannot be derived is `failed` with `HISTORY_COMPACTED`, the report names the generation it compared against); the dry run writes one log line with its counts; the report counts shadowed entries per reason for a change that shadows (FR-020)
- [X] T037 [US2] Implement `plan_schema` in `ports/py/src/aduku/service.py` (replay under the current and the target version over the same entries and recorded outcomes, nothing stored); make T035 and T036 pass; log each dry run (target, generation, counts, never a value)
- [X] T038 [P] [US2] Write failing tests `ports/py/tests/integration/test_schema_activation.py` (both backends): a breaking target is refused without `acknowledge_breaking` (`BREAKING_CHANGE_NOT_ACKNOWLEDGED` naming the first breaking changes) and goes with it; a compatible or visible target needs none; a stale `expect_generation` is `SCHEMA_STATE_CHANGED`; the existing `TIER_IN_USE` and `HISTORY_COMPACTED` refusals still apply and record nothing; a lower version is recorded as `rollback`; a compatible activation touches no document (no view or layer written, counted); a refused and an accepted activation each write a log line with the operator, versions and class
- [X] T039 [P] [US2] Write failing tests `ports/py/tests/integration/test_schema_activation_rebuild.py`: `rebuild=True` refreshes every saved layer and view of the unpinned documents under the new version, reports those whose answer changed and those that could not be derived, writes nothing to the ledger, and the activation stands when one document fails (clarification 5)
- [X] T040 [US2] Complete `activate_schema` with the classification, the acknowledgement rule, `expect_generation` and the eager rebuild; make T038 and T039 pass; log a refused activation (code, class, first breaking changes) as well as an accepted one
- [X] T041 [P] [US2] Write failing test `ports/py/tests/integration/test_schema_activation_cost.py`: activating a compatible version on a store of 100 documents and of 100,000 documents (written through the storage interface) takes about the same time and reads no ledger entry (the SQLite run uses 10,000 instead of 100,000 documents, which writing through the interface makes slow; the in-memory run uses 100,000); a dry run limited to 1,000 of 10,000 documents reads about a tenth of the entries of an unlimited one (SC-005); one request on a bound registry adds exactly one generation read to the storage reads of the same request on an unbound one (E1)
- [X] T042 [P] [US2] Write failing tests `ports/py/tests/integration/test_cli_schema_evolution.py`: `schema diff` of two files prints the change list and the overall class and needs no store; `schema plan --target V --limit N --doc D` prints counts and what differs without values; `schema activate` with and without `--acknowledge-breaking` and `--rebuild`; `schema history`; a missing `--operator` is exit 2; repeated `--schema` and a directory of versions
- [X] T043 [US2] Add the commands and the repeatable `--schema` to `ports/py/src/aduku/cli.py` per `contracts/cli.md`; make T042 pass
- [X] T044 [P] [US2] Write the vectors `conformance/schema-changes.ion` (one pair per row of the classification table, several changes at once, an unclassified difference) and `conformance/schema-dry-run.ion` (a dry run then the activation and the reads it predicted, a limit, pinned documents left out, a breaking activation refused, a rollback reading byte-identically, `TIER_IN_USE`), and the runner steps `diff` and `plan` and the extended `activate_schema` in `ports/py/tests/conformance_runner.py`

---

## Phase 6: User Story 3 - Hold a document on its version and move it when ready (Priority: P2)
**Goal**: pins as planned state, single and batch migration. **Independent test**: US3 scenarios 1 to 6, SC-007.
- [X] T045 [P] [US3] Write failing tests `ports/py/tests/integration/test_schema_pins.py` (both backends): a pinned document is read and written under its version while others follow the active one; a pin is refused for the reasons an activation is; a pin made by one service is seen by another; an unpin returns the document to the active version; every act is in the history
- [X] T046 [US3] Finish `pin_document` and `unpin_document` (reasons, shared state) in `ports/py/src/aduku/service.py`; make T045 pass
- [X] T047 [P] [US3] Write failing tests `ports/py/tests/integration/test_schema_migration.py` (both backends): `plan_migration` shows one document's changes and writes nothing; `migrate` moves pinned documents to the target and unpins those whose target is the active version; a document that cannot be derived under the target is reported `failed` and stays; a re-run after a stop reports the moved ones `already_moved` and touches nothing; an unpinned document is `not_pinned` (single-document form raises `NOT_PINNED`); batches report progress; 1,000 pinned documents stopped and restarted move once each (SC-007); each document moved or failed writes a log line
- [X] T048 [US3] Implement `plan_migration` and `migrate` in `ports/py/src/aduku/service.py` (a transaction per document with its history record); make T047 pass; log each moved, failed and already-moved document and the end of a batch
- [X] T049 [P] [US3] Write failing tests `ports/py/tests/integration/test_cli_pins.py`: `pin`, `unpin`, `migrate` with `--to`, `--dry-run` and `--batch`, a refusal printed with its code, a missing `--operator`
- [X] T050 [US3] Add the commands to `ports/py/src/aduku/cli.py`; make T049 pass
- [X] T051 [P] [US3] Write the vector `conformance/schema-pins.ion` (pin, write under the pin, a single migration, a batch with a document that cannot be derived, a re-run) and the runner steps `pin`, `unpin` and `migrate`

---

## Phase 7: Polish and cross-cutting
- [X] T052 Update `docs/SPEC.md` (Draft 0.13): section 14 (the classification table with the added rows, the shared state and history, the dry run, pins and migration, rollback, `SCHEMA_VERSION_CHANGED`), 9.2 (reasons for shadowed entries), 10.4 (what a dry run and an activation do), 12.4 (the new admin operations), 12.5 (codes) and the changelog; check the plan, spec and constitution agree
- [X] T053 [P] Update `docs/notes.md` (decisions with reasons: shared state in the store, per-field fingerprints, the classification table and the removed-field row, the conformance rule in the fold, the acknowledgement living on the operation not on the registry), `README.md`, `ports/py/README.md`, `CLAUDE.md` and mark Feature 6 done in `docs/prompts/specify-features.md`
- [X] T054 [P] Add `ports/py/examples/demo_evolution.py` (diff, dry run, breaking activation, rollback, pin and migrate) and a smoke test `ports/py/tests/integration/test_demo_evolution.py`
- [X] T055 Run `uv run ruff format`, `uv run ruff check`, `uv run mypy src` and the full suite on both backends; run the property suites with the CI profile three times; run every vector of Features 1 to 5 unchanged; run a mutation check of at least ten deliberate breaks (classification row, fingerprint compare, generation reload, acknowledgement, dry run storing a view, shadow rule, migration resume) and add a test for any survivor
- [X] T056 Walk through `specs/006-schema-evolution/quickstart.md` by hand against a temporary database and fix any drift between it, the contracts and the code

## Phase 8: Convergence
- [X] T057 Add `Aduku.schema_changes(old_version, new_version) -> ChangeList` in `ports/py/src/aduku/service.py` (the registered versions compared with the pure `schema_changes`; `SCHEMA_INACTIVE` for an unregistered one), with a test in `ports/py/tests/integration/test_schema_plan.py`, per contracts/library-api.md "Changes between versions" and research R-9 (partial)
- [X] T058 Make the low-level `SchemaRegistry.activate` record the class of the change in the history when it is not given (computed with `schema_changes` from the version it replaces; `None` for the first activation), as research R-6 says, with a test in `ports/py/tests/unit/test_registry_binding.py` per research R-6 (partial)
- [X] T059 Let an operator see which fingerprint each served version has: add `Aduku.served_versions() -> dict[int, str]` (version to full fingerprint, read from the store) and print it in `schema history` of the command line, with tests, per the edge case "Two processes with different schema files" (the history shows which fingerprint was served) (partial)
