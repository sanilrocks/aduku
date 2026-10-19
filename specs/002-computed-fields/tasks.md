# Tasks: Computed Fields and Non-Writability

**Input**: Design documents from `/specs/002-computed-fields/`

**Prerequisites**: plan.md, spec.md, research.md, data-model.md, contracts/, quickstart.md

**Tests**: INCLUDED and mandatory. Constitution Principle X (NON-NEGOTIABLE): tests are written and observed failing before the implementation they cover. Unlike Feature 001, the worked-example vector (T018) is written first.

**Organization**: Tasks are grouped by user story. All Python paths are under `ports/py/` (plan.md, Structure Decision); shared vectors live in `conformance/` at the repo root. Feature 001 is implemented and is the base.

## Format: `[ID] [P?] [Story] Description`

- **[P]**: Can run in parallel (different files, no dependency on an incomplete task)
- **[Story]**: User story from spec.md (US1 to US7). Setup, Foundational and Polish tasks have no story label.

## Story map

| Story | Priority | Subject |
|---|---|---|
| US2 | P1 | A computed value can never be overridden (non-writability) |
| US1 | P1 | See computed values in the document (preset macros, P4) |
| US3 | P2 | Register custom macros that run safely (expression language, limits) |
| US4 | P2 | Refuse impossible dependencies when the schema loads |
| US5 | P3 | Decide what happens when an input is missing (`on_missing`) |
| US6 | P2 | Impure computes with recorded outcomes (effect log, hooks) |
| US7 | P2 | Choose what an impure failure does, and recover (failure modes, `recompute`) |

Build order follows dependencies, not numbers: US2 first (it is mostly a guard over Feature 001), then US1, then US3 (the worked example's `label` needs a custom macro, so T018 stays red until T036), then US4, US5, US6, US7.

---

## Phase 1: Setup

- [X] T001 Update `ports/py/tests/unit/test_purity_imports.py` (Principle III, IV): add `expr/` to the pure packages (no `aduku.storage`, `aduku.audit`, `aduku.log`, `aduku.service`, `aduku.hooks`, `sqlite3`, `time`, `datetime`, `random`, `secrets`, `uuid`, `os`) and assert that nothing under `engine/`, `schema/`, `behaviors/` or `expr/` imports `aduku.hooks`; the check tolerates a package that does not exist yet
- [X] T002 [P] Add the error codes `COMPUTE_FAULT` and `IMPURE_COMPUTE_FAILED` to `ports/py/src/aduku/errors.py` (with a unit test in `ports/py/tests/unit/test_errors.py`) and the exception `ComputeFault(AdukuError)` carrying the macro name and the reason (never a value)
- [X] T003 [P] Extend `conformance/README.md` with the fixture behaviors `acme.label@1`, `fx.echo@1`, `fx.fail@1`, `fx.flaky@1` and the steps `hook_calls` and `recompute` exactly as in `contracts/conformance-vectors.md`

---

## Phase 2: Foundational (blocks all stories)

### Tests first (confirm they fail)

- [X] T004 [P] Write `ports/py/tests/unit/test_compute_declaration.py`: parsing of `computed` per `contracts/schema-format.md` (macro name with optional `@version`, `args` as paths or `lit::` literals, `on_missing` values and default, the impure form with every required key, `recompute_by` vocabulary and `UNKNOWN_TIER`, `IMPURE_DECLARATION_INCOMPLETE` for each missing key, platform bounds for `timeout`, `retry.max`, `deadline`, `failure_mode` and `impure_failure_mode` values)
- [X] T005 [P] Write `ports/py/tests/unit/test_dependency_graph.py`: topological order is the same for any declaration order of the schema, ties break by path in code-point order, a path beneath a derived field depends on that field, an impure field's `depends` are edges, cycles of length 1, 2 and 3 are found and the loop is listed
- [X] T006 [P] Write `ports/py/tests/unit/test_limits.py`: platform defaults and maximum from `contracts/expression-language.md`; a per-macro limit may be lower, never higher (`SchemaError`); unspecified limits fall back to the defaults

### Implementation

- [X] T007 Add `ComputedSpec` (macro name and version, ordered `args` of path or literal, `on_missing`) and `ImpureSpec` (hook, depends, reason, timeout, retry, deadline, recompute_by, failure_mode) to `ports/py/src/aduku/schema/model.py` and give `FieldSpec` the typed `computed` and `impure` attributes alongside the existing `derived` fields
- [X] T008 Parse and check the declarations in `ports/py/src/aduku/schema/loader.py` (T004): fill `ComputedSpec`/`ImpureSpec`, report `IMPURE_DECLARATION_INCOMPLETE`, the platform bounds and `UNKNOWN_TIER` for `recompute_by`, keep the existing derived-field checks (no `merge`/`default`, not both computed and deferred)
- [X] T009 Implement `ports/py/src/aduku/engine/graph.py` (pure): build the dependency graph of computed fields from `ComputedSpec`/`ImpureSpec`, return the evaluation order, and find cycles with their loops (T005); the loader's existing cycle check calls it instead of its own copy
- [X] T010 [P] Implement `ports/py/src/aduku/expr/limits.py` (pure): `Limits` with the defaults and maximum, `lowered(...)` that refuses higher values (T006)
- [X] T011 Extend `ports/py/src/aduku/behaviors/registry.py`: behavior kinds `macro` and `hook`, `register_hook(name, version, call)`, a reserved set of preset names that `register_*` refuses (`RESERVED_MACRO_NAME`)
- [X] T012 [P] Extend `ports/py/tests/fixtures.py` with the counting hooks `fx.echo@1`, `fx.fail@1` and `fx.flaky@1` (each exposes its call count) registered by `register_fixtures`

**Checkpoint**: T004 to T006 pass; `uv run pytest tests/unit` is green.

---

## Phase 3: User Story 2 - A computed value can never be overridden (P1)

**Goal**: every write to a computed path, or beneath one, is rejected whole with `FIELD_NOT_WRITABLE`, whatever the flag, tier or authority.

**Independent Test**: submit the "HACKED" request of SPEC 16.1 with auditing on and off; document, ledger and sequence numbers are unchanged.

### Tests first (confirm they fail or already pass because of Feature 001)

- [X] T013 [P] [US2] Write `ports/py/tests/integration/test_computed_not_writable.py`: scenarios 1 to 7 of US2 on both backends: set and delete on a computed path, beneath it, on an impure path, with the non-rejection flag, in a request that also has a valid entry, audit on (one record) and off (none), no seq consumed, and entries kept but shadowed when a later schema version makes a field computed
- [X] T014 [US2] Make T013 pass: check and, where it fails, fix `ports/py/src/aduku/engine/admission.py` so that a field declared with `computed.impure` is derived for the P0 rule (the existing rule covers `computed.macro`) and that nothing bypasses it
- [X] T015 [P] [US2] Start `conformance/computed-fields.ion` with the `FIELD_NOT_WRITABLE` cases (pure path, impure path, child path, delete, mixed request); `ports/py/tests/golden/test_conformance_vectors.py` runs it

**Checkpoint**: US2 complete when T013 and T015 pass. Rejection of computed paths is demonstrable before any value is computed.

---

## Phase 4: User Story 1 - See computed values in the document (P1)

**Goal**: preset macros over declared dependencies appear in the document with the `computed::` annotation, in dependency order, identical on every derivation.

**Independent Test**: `displayName = upper(name)` over the 16.1 ledger gives `computed::"ALICE S."`; changing `name` changes it on the next read.

### Tests first (confirm they fail)

- [X] T016 [P] [US1] Write `ports/py/tests/unit/test_presets.py`: every preset (`upper lower trim concat coalesce len sum min max round format_ts hash join if eq`) on valid inputs and on the wrong types (a fault, never a coerced value), the `accepts_missing` flag of each (only `coalesce`, `if`, `eq`, `join`), the reserved names, and the result type each fixes
- [X] T017 [P] [US1] Write `ports/py/tests/unit/test_compute_engine.py`: P4 over a merged document: values and the `computed::` annotation, a chain of computed fields in dependency order, the macro is not run and the field is `null` when an input is missing and the macro does not accept missing inputs, a result that does not fit the declared type is a `ComputeFault` naming the macro
- [X] T018 [P] [US1] Update `conformance/customer.schema.ion` (add `/label` with the custom macro `acme.label@1` over `/name` and `/address/city`) and `conformance/worked-example-16-1.ion` (expected document gains `displayName: computed::"ALICE S."` and `label: computed::"Alice S. (Paris)"`, `explain` of both) and `ports/py/tests/golden/test_worked_example.py` as two tests, `test_computed_display_name` (green after T025) and `test_computed_label` (red until T036), with the vector's `get` steps split the same way (SC-001)
- [X] T019 [P] [US1] Write `ports/py/tests/property/test_compute_properties.py`: for random computed-field schemas over fixed inputs, shuffling the declaration order never changes the document; deriving from scratch equals deriving through the cache equals `rebuild`; a computed value changes only when one of its dependencies changes (SC-003, SC-004)
- [X] T020 [P] [US1] Write `ports/py/tests/integration/test_computed_reads.py`: scenarios 2, 3, 4 and 6 of US1 on both backends: a changed input updates dependent fields on the next read and no others, chained fields, byte-identical text across backends, cold and warm reads, and no entry in `list_entries` ever holds a computed value
- [X] T021 [P] [US1] Write `ports/py/tests/unit/test_explain_computed.py`: `explain` on a computed path names `computed`, the macro and version, and the dependency values (scenario 5, FR-026); entries made unusable by the field becoming computed are listed as shadowed (FR-009); one call per field (SC-011)

### Implementation

- [X] T022 [US1] Implement `ports/py/src/aduku/behaviors/compute.py` (pure): the preset functions with signatures, result types and `accepts_missing` flags, and the reserved names used by T011 (T016)
- [X] T023 [US1] Implement `ports/py/src/aduku/engine/compute.py` (pure): P4 evaluates computed fields in the order of `engine/graph.py`, passes each macro only its declared dependencies, wraps results as `Annotated(("computed",), value)`, applies the default `on_missing: null`, and raises `ComputeFault` for an error or a result of the wrong type (T017)
- [X] T024 [US1] Compose P4 into `ports/py/src/aduku/engine/model_errors.py` (`_evaluate`: P1 to P3, then P4, then P6) and let `ports/py/src/aduku/engine/validate.py` read the plain value beneath a `computed::` annotation so validators depend on computed values (FR-015)
- [X] T025 [US1] Update `ports/py/src/aduku/service.py` and `engine/project.py`: annotated values flow through the view, canonical text and `get`; a `ComputeFault` in the admission dry run is `RejectedError(COMPUTE_FAULT)` with a reason naming the macro, and on a read it propagates as `ComputeFault`
- [X] T026 [US1] Extend `ports/py/src/aduku/engine/explain.py`, `ports/py/src/aduku/model.py` (`Explanation.dependencies`, `effect_seq`) and `Aduku.explain` for computed paths (T021); `shadowed` still lists earlier entries
- [X] T027 [US1] Migrate Feature 001 expectations that read documents under a schema with `displayName`: `ports/py/tests/golden/`, `ports/py/tests/integration/test_cli.py`, `specs/001-core-ledger-merge-engine/quickstart.md` and the vectors other than 16.1 (`displayName` appears once `name` is set); keep the `label` expectations for T036

**Checkpoint**: US1 complete when T016, T017, T019 to T021 and `test_computed_display_name` of T018 pass (`test_computed_label` waits for T036).

---

## Phase 5: User Story 3 - Register custom macros that run safely (P2)

**Goal**: custom macros are expressions in a sandboxed language; they see only their declared dependencies and are stopped at their limits.

**Independent Test**: register one macro that reads an undeclared name and one that exceeds its step budget; the first faults, the second is stopped, and neither affects other fields.

### Tests first (confirm they fail)

- [X] T028 [P] [US3] Write `ports/py/tests/unit/test_expr_parser_evaluator.py`: the grammar of `contracts/expression-language.md` (literals, operators, precedence, ternary, lists, maps, indexing, method calls), lazy `&&`, `||` and `?:`, the function library equals the presets, integer overflow and division by zero are faults, mixed-type comparison is a fault
- [X] T029 [P] [US3] Write `ports/py/tests/unit/test_expr_limits.py`: steps, expression length, nesting depth, built-value size and input size each stop the evaluation with a fault that names the limit and never a value; a macro registered with lower limits stops earlier; a registration above the maximum is refused (scenarios 4, 7 to 9)
- [X] T030 [P] [US3] Write `ports/py/tests/unit/test_macro_sandbox.py`: an expression cannot read a name that is not a parameter, call `open`, `__import__`, `time` or any name outside the library, or reach the document; preset names cannot be registered; the macro result depends only on its inputs (scenarios 2, 3, 6, SC-006)
- [X] T031 [P] [US3] Write `ports/py/tests/integration/test_custom_macros.py`: a registered macro in a schema gives the same result as a preset would, an unregistered macro or version is refused at load (`UNKNOWN_BEHAVIOR`), a macro fault rejects the request with `COMPUTE_FAULT` and raises `ComputeFault` on a read, never null (scenarios 1, 4, 5)

### Implementation

- [X] T032 [US3] Implement `ports/py/src/aduku/expr/parser.py` (pure): tokenizer and parser producing an immutable tree, with the length and depth limits checked while parsing
- [X] T033 [US3] Implement `ports/py/src/aduku/expr/evaluator.py` (pure): tree walk with a step counter, value-size accounting, lazy operators, the function library from `behaviors/compute.py`, and faults naming the limit (T028, T029)
- [X] T034 [US3] Add `register_macro(name, version, params, expression, accepts_missing=False, limits=None)` to `ports/py/src/aduku/behaviors/registry.py` (parses the expression at registration, refuses preset names and limits above the maximum) and make `engine/compute.py` run a registered macro through the evaluator with its limits (T030, T031)
- [X] T035 [US3] Extend the loader (`ports/py/src/aduku/schema/loader.py`): `BAD_MACRO_ARGS` when the arguments do not fit a macro's parameters or a preset's signature, `MACRO_INPUT_UNSUPPORTED` when a custom macro's dependency has type `float` or `timestamp`
- [X] T036 [US3] Register `acme.label@1` in `ports/py/tests/fixtures.py` and the conformance runner (`ports/py/tests/conformance_runner.py`) and make T018 pass completely (`label` in the 16.1 document)

**Checkpoint**: US3 complete when T028 to T031 pass and T018 is green.

---

## Phase 6: User Story 4 - Refuse impossible dependencies when the schema loads (P2)

**Goal**: cycles, undeclared dependencies and dependencies on deferred fields are refused at load with stable codes.

**Independent Test**: load schemas with a two-field cycle, a three-field cycle, a self-reference, an undeclared dependency and a deferred dependency.

### Tests first (confirm they fail)

- [X] T037 [P] [US4] Extend `ports/py/tests/unit/test_schema_load_failures.py` with scenarios 1 to 6 of US4: `DEPENDENCY_CYCLE` listing the loop (length 1, 2, 3), `UNKNOWN_DEPENDENCY`, `DEPENDS_ON_DEFERRED`, a derived field with `merge` or `default`, both computed and deferred, and an assertion that evaluation order does not depend on file order

### Implementation

- [X] T038 [US4] Add the `UNKNOWN_DEPENDENCY` check to `ports/py/src/aduku/schema/loader.py` (a path no field declares and that is not beneath a declared field) and make the existing cycle and deferred reports use `engine/graph.py` (T037)
- [X] T039 [US4] Add the new load-failure cases to `conformance/schema-load-failures.ion` and the codes to `specs/002-computed-fields/contracts/conformance-vectors.md` if any are missing (SC-005)

**Checkpoint**: US4 complete when T037 passes and the vector is green.

---

## Phase 7: User Story 5 - Decide what happens when an input is missing (P3)

**Goal**: `on_missing` is `null`, `absent` or `error`; macros that accept missing inputs always run; the `error` model error counts against the no-new-errors rule.

**Independent Test**: a document with no `name`, read under each setting; then a request that deletes `name` with and without the flag.

### Tests first (confirm they fail)

- [X] T040 [P] [US5] Write `ports/py/tests/integration/test_on_missing.py`: scenarios 1 to 10 of US5: the three settings, a missing computed dependency, a defaulted dependency, a deleted one with and without fallback, `coalesce` with one and two missing inputs, the model error `computed:<path>` and its attribution, an unflagged request that causes it rejected (`MODEL_ERROR_INTRODUCED` or `VALIDATION_FAILED`), a flagged one accepted

### Implementation

- [X] T041 [US5] Implement `on_missing: absent` and `error` in `ports/py/src/aduku/engine/compute.py`: `error` reports a violation with rule `computed:<path>` and scope `<path>` that enters `update_errors` like a validator violation (T040); missing includes a missing computed dependency
- [X] T042 [US5] Add the on_missing cases as `conformance/computed-on-missing.ion` (all three settings, `coalesce`, a default, the no-new-errors cases); a separate case because an `on_missing: error` field gives every empty document a model error

**Checkpoint**: US5 complete when T040 passes.

---

## Phase 8: User Story 6 - Impure computes with recorded outcomes (P2)

**Goal**: an impure field calls its hook while a request is handled, records every outcome in an append-only effect log, and later derivations, rebuilds and retries use the record and make no call.

**Independent Test**: a counting hook; submit a request that changes its inputs, read several times, rebuild, retry the request: one call.

### Tests first (confirm they fail)

- [X] T043 [P] [US6] Write `ports/py/tests/integration/test_effect_log.py` (both backends): append-only (update and delete forbidden), `effect_seq` increases, the latest record per `(doc, field, fingerprint, hook)` wins, outcomes written in a transaction that rolls back are not visible, `prune_effects` keeps the record in force and at most 5 superseded records per key and none older than 90 days, pruning never changes a derivation
- [X] T044 [P] [US6] Write `ports/py/tests/unit/test_effects_engine.py`: the fingerprint is the canonical text hash of hook name@version and the dependency values in declared order, stable across runs; selection of the outcome in force; `needed_calls` lists exactly the impure fields with no outcome for their current fingerprint
- [X] T045 [P] [US6] Write `ports/py/tests/unit/test_hook_runner.py`: per-attempt timeout, retries up to `retry.max` with the fixed backoff, stop at the deadline, attempts counted, the hook is passed only its declared dependencies and its timeout, a hook that outlives its timeout is abandoned without blocking the caller, a failure outcome after exhaustion
- [X] T046 [P] [US6] Write `ports/py/tests/integration/test_impure_computes.py`: scenarios 1 to 9 of US6: value in the response, zero calls on reads, rebuild and retry, no second call for repeated inputs, a request rejected for another reason discards its outcome, dependents and validators use the value, an incomplete declaration is refused, `schema check` lists the inventory, `explain` of an impure field names its `effect_seq`, a new hook version in a new schema version triggers a new call and the old outcome is not reused, and a lost or empty effect log makes the next derivation that needs the value call the hook again
- [X] T047 [P] [US6] Write `ports/py/tests/integration/test_impure_lock.py`: a hook that takes 2 seconds does not delay a write to another document by more than 100 milliseconds, the hook is never called while a write transaction is open (a storage double records this), and a document that changed while the hook ran is derived again with unchanged outcomes reused (SC-010)
- [X] T048 [P] [US6] Add the first half of `conformance/impure-computes.ion` (hook called once, repeated reads, rebuild with `hook_calls` unchanged, retry of an accepted request)

### Implementation

- [X] T049 [US6] Extend `ports/py/src/aduku/storage/base.py`: `EffectRecord`, the `Tx` methods `append_effects`, `latest_effects(doc, keys)`, `effects_seq(doc)`, `list_effects(doc, field)`, `prune_effects(doc, ...)`, and `ViewRow.effects_seq` (data-model.md)
- [X] T050 [US6] Implement the effect log in `ports/py/src/aduku/storage/memory.py` with undo on rollback (T043)
- [X] T051 [US6] Implement the `effects` table, append-only triggers and the `effects_seq` view column in `ports/py/src/aduku/storage/sqlite.py`, with a migration for databases created by Feature 001
- [X] T052 [US6] Implement `ports/py/src/aduku/engine/effects.py` (pure): fingerprint, outcome selection, `needed_calls` (T044)
- [X] T053 [US6] Implement `ports/py/src/aduku/hooks.py` (edge, not imported by `engine/`): `HookRunner` with the bounded worker pool, timeout, retry, backoff and deadline, emitting a `hook_call` log event with no values (T045)
- [X] T054 [US6] Finish the impure declaration checks in `ports/py/src/aduku/schema/loader.py`: hook registered (`UNKNOWN_BEHAVIOR`), `depends` not deferred and not cyclic, the hook inventory available to `schema check`
- [X] T055 [US6] Feed the effect view into derivation: `engine/compute.py` takes impure values from the outcome in force (`value`) and leaves failure handling to US7, `engine/model_errors.py` and `engine/derive.py` carry the view, and the cached view is served only if `effects_seq` matches (`service._fits`), and `explain` fills `effect_seq` for an impure field
- [X] T056 [US6] Rework admission in `ports/py/src/aduku/service.py` (research R-8): derive at the observed watermark, call hooks outside any transaction through `HookRunner`, re-check the watermark inside the write transaction, append entries and outcomes together, discard outcomes of a rejected request; a read with no outcome calls the hook and records it in its own short transaction
- [X] T057 [US6] Add `Aduku.list_effects` and `Aduku.prune_effects`, the CLI `effects list` and `effects prune` (metadata only: never a value or an error text), `--behaviors MODULE` and the `schema check` inventory in `ports/py/src/aduku/service.py` and `cli.py`

**Checkpoint**: US6 complete when T043 to T048 pass and T047 shows no lock overlap.

---

## Phase 9: User Story 7 - Choose what an impure failure does, and recover (P2)

**Goal**: a hook that cannot deliver either rejects the request or is recorded as a model error, per field or document type; `recompute` fixes it for the callers the schema permits.

**Independent Test**: a hook that always fails under each setting, then fix it and recompute.

### Tests first (confirm they fail)

- [X] T058 [P] [US7] Write `ports/py/tests/integration/test_impure_failures.py`: scenarios 1 to 8 of US7: `reject_on_failure` rejects with `IMPURE_COMPUTE_FAILED` and records nothing, a read or rebuild shows a model error and is never rejected, `model_error_on_failure` accepts and attributes `impure:<path>` to the request, that error is exempt from the no-new-errors rule while another new error still counts, dependents follow their `on_missing` and validators on the missing value add no error, field setting over document-type setting over the default, a changed input calls the hook again
- [X] T059 [P] [US7] Write `ports/py/tests/integration/test_recompute.py`: scenarios 7, 9 and 10: `recompute` supersedes the outcome and clears the model error, `recompute_by` allows by tier and by role, anyone else gets `NOT_AUTHORIZED` with no call and no record, a recompute without a field covers only permitted fields and reports skipped ones, the `recompute` log event
- [X] T060 [P] [US7] Add the second half of `conformance/impure-computes.ion` (both failure modes, recompute, `recompute_by`) and the `hook_calls` and `recompute` steps to `ports/py/tests/conformance_runner.py`

### Implementation

- [X] T061 [US7] Implement the failure policy: a failed outcome produces a violation `impure:<path>` in `engine/compute.py` (dependents follow `on_missing`, dependent validators skipped); in `service.py` a request whose own hook failed under `reject_on_failure` is `RejectedError(IMPURE_COMPUTE_FAILED)`, and errors with rule `impure:<path>` are exempt from the no-new-errors check; resolve `failure_mode` as field, then `impure_failure_mode`, then default
- [X] T062 [US7] Implement `Aduku.recompute(doc, field, caller)` and `RecomputeReport` in `ports/py/src/aduku/service.py` and `model.py`: check `recompute_by` against the caller's tiers and roles, call the hook, append a superseding outcome, emit the `recompute` event
- [X] T063 [US7] Add the CLI `recompute` command in `ports/py/src/aduku/cli.py` (exit 1 when refused or a hook failed) and wire the new runner steps to the service (T060)

**Checkpoint**: US7 complete when T058 to T060 pass.

---

## Phase 10: Polish and cross-cutting

- [X] T064 [P] Create `ports/py/examples/demo_hooks.py` (a `register(behaviors)` function with counting hooks) for the quickstart CLI walkthrough
- [X] T065 [P] Write `ports/py/tests/integration/test_compute_performance.py`: a document with 1,000 accepted entries and 5 computed fields reads uncached in under 1 second on both backends (SC-007); re-run `test_read_performance.py`
- [X] T066 [P] Update `ports/py/README.md`, `docs/SPEC.md` (5.3: add `on_missing` and `recompute_by` to the annotation table; 12.5: add `COMPUTE_FAULT`; 9.3 and 12.4 where the implementation differs) and `specs/001-core-ledger-merge-engine/contracts/library-api.md` (the read exception for impure hook calls, already worded) so the contracts and SPEC stay consistent
- [X] T067 Run `uv run ruff check . && uv run ruff format --check . && uv run mypy` in `ports/py/` and fix findings
- [X] T068 Run `uv run pytest tests/property --hypothesis-profile=ci` and confirm SC-003 and SC-004 (at least 1,000 ledgers, 0 mismatches)
- [X] T069 Execute every step of `specs/002-computed-fields/quickstart.md` and correct the guide or the code where they disagree
- [X] T070 Update `docs/notes.md` and `CLAUDE.md` (current step); `/speckit-analyze` and `/speckit-converge` are run afterwards as separate steps

---

## Requirements coverage

| Requirement | Tasks |
|---|---|
| FR-001, FR-003, FR-005 | T007, T017, T023, T024, T025 |
| FR-002 | T016, T022 |
| FR-004 | T005, T009, T017, T019 |
| FR-006 | T020, T025 |
| FR-007, FR-008 | T013, T014, T015 |
| FR-009 | T013, T021, T026 |
| FR-010, FR-011 | T006, T010, T011, T029, T030, T031, T033, T034 |
| FR-012 | T005, T009, T037, T038, T039 |
| FR-013, FR-013a, FR-013b, FR-014 | T016, T022, T040, T041, T042 |
| FR-015 | T017, T024, T046 |
| FR-016 | T004, T008, T046, T054 |
| FR-017, FR-022 | T045, T053 |
| FR-018, FR-020 | T046, T048, T056 |
| FR-019, FR-027 | T043, T044, T049, T050, T051, T052, T055, T057 |
| FR-021 | T047, T056 |
| FR-023, FR-024 | T058, T061 |
| FR-025 | T059, T062, T063 |
| FR-026 | T021, T026 |
| FR-028 | T037, T039, T057 |
| FR-029 | T045, T053, T059, T062 |
| SC-001 | T018, T036 |
| SC-002 | T013, T015 |
| SC-003, SC-004 | T019, T020, T068 |
| SC-005 | T037, T039 |
| SC-006 | T029, T030 |
| SC-007 | T065 |
| SC-008 | T046, T048 |
| SC-009 | T058, T059 |
| SC-010 | T047 |
| SC-011 | T021, T026 |

## Dependencies and execution order

- **Phase 1 → Phase 2 → stories → Polish.** Phase 2 blocks all stories.
- **US2** needs Phase 2 only (the guard is Feature 001's; T014 may be a no-op).
- **US1** needs Phase 2 (graph, declarations); its `label` expectation needs **US3** (T036).
- **US3** needs US1's engine (T023) and Phase 2's registry and limits.
- **US4** needs Phase 2's graph and US3's loader work (T035).
- **US5** needs US1.
- **US6** needs US1 (P4), Feature 001's storage, and Phase 2's hook registration.
- **US7** needs US6 and US5 (dependents follow `on_missing`).
- Within each story: tests, confirmed failing, then implementation.

### Parallel opportunities

- Phase 2 tests T004 to T006 together; T010 and T012 beside T007 to T009.
- US1 tests T016 to T021 together; US3 tests T028 to T031 together; US6 tests T043 to T048 together; US7 tests T058 to T060 together.
- After US1: US3 and US5 proceed in parallel (US5 touches `engine/compute.py`, so sequence T041 after T034 if one person does both).
- US6 storage tasks T050 and T051 are independent of each other.

### Parallel example (US6 tests)

```text
T043 effect log tests        T044 effects engine tests     T045 hook runner tests
T046 impure compute tests    T047 lock discipline tests    T048 first-half vector
```

## Implementation strategy

1. **MVP (P1 stories)**: Phases 1 and 2, then US2 and US1. A document with preset-computed fields that nobody can write.
2. **Increment**: US3 (custom macros), then US4 and US5, then US6 and US7 (impure computes).
3. **Finish**: Polish; SC-001 (T018 and T036) is the acceptance gate for the pure path and `impure-computes.ion` for the impure path.

## Out of scope for this feature (deferred, see spec Assumptions)

Deferred (stand-alone) fields and their stored results, redaction, CEL itself and WebAssembly modules, per-field authority beyond
what Feature 001 parses, snapshots, compaction, time-travel reads, and detailed schema evolution.

---

## Phase 11: Convergence

- [X] T071 Export `ComputeFault`, `RecomputeReport`, `EffectInfo` and `HookRunner` from `ports/py/src/aduku/__init__.py` (lazily, like the other names) and extend `ports/py/tests/unit/test_public_api.py` so that every type the contract names is importable from `aduku` per contracts/library-api.md (missing)

---

## Phase 12: Convergence

- [X] T072 Write `ports/py/tests/integration/test_sqlite_migration.py`: a SQLite database created by Feature 001 (a `views` table without `effects_seq`, and no `effects` table) opens, keeps its stored views readable with `effects_seq` 0, accepts an impure outcome, and a second open is a no-op; fix `ports/py/src/aduku/storage/sqlite.py` if it does not, per T051 (partial)

---

## Phase 13: Code review fixes

Found by `/code-review` after Phase 12, checked against the code, and fixed test-first; each task names its regression test. Three further findings were checked and left as they are: a failed `recompute` supersedes the earlier outcome (FR-025), a failure recorded by a read stays until `recompute` (FR-024), and the replay after a new effect is bounded.

- [X] T073 Order a computed field after the computed fields inside a struct it reads, without making a field depend on itself: `edges` in `ports/py/src/aduku/engine/graph.py`; and strip `computed::` throughout a struct dependency before a macro or hook sees it, and before it enters the fingerprint: `_plain` in `ports/py/src/aduku/engine/compute.py`. Tests: `test_dependency_graph.py` (two) and `test_computed_reads.py` per FR-004, FR-005 (partial)
- [X] T074 Walk the dependency graph without recursion in `find_cycles` so a chain of about 900 or more fields is reported or accepted, never a `RecursionError`. Test: `test_a_very_long_chain_of_fields_is_checked_without_recursion` per FR-009 (partial)
- [X] T075 Leave an out-of-range negated integer literal as a `Unary` node so the evaluator raises "integer overflow" (`ports/py/src/aduku/expr/parser.py`). Test: `test_negating_the_lowest_integer_overflows_instead_of_dropping_the_sign` per FR-013 (contradicts)
- [X] T076 Refuse an expression whose tree is more than 200 levels high (`MAX_TREE_HEIGHT`, found iteratively) so a long `a + a + ...` or `x.f().g()...` chain is an `ExprError` at compile time, and document the cap in `contracts/expression-language.md`. Test: `test_a_long_chain_of_operators_is_refused_when_compiled_not_by_the_stack` per FR-013 (partial)
- [X] T077 Run `round` and `sum` in the 38-digit decimal context shared with the evaluator (`DECIMAL_PRECISION` in `ports/py/src/aduku/behaviors/compute.py`), turn decimal exceptions into `PresetError`, and catch every decimal exception in the evaluator (`ports/py/src/aduku/expr/evaluator.py`) so none escapes as a crash. Tests: `test_presets.py` (two) and `test_a_decimal_beyond_the_range_is_a_fault_not_a_crash` per FR-013, FR-014 (partial)
- [X] T078 Keep the hook outcomes found so far when deriving fails in `_outcomes_for` (`ports/py/src/aduku/service.py`), so the transaction meets the same fault and the request is rejected with `COMPUTE_FAULT` (or `VALIDATION_FAILED`), never `SEQ_CONFLICT`, and nothing is stored. Test: `test_12_a_fault_in_what_the_hook_answered_is_a_compute_fault_not_a_sequence_conflict` per FR-014, FR-021 (contradicts)
