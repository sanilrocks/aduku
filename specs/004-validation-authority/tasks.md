# Tasks: Validation Rule Library and Per-Field Authority

**Input**: Design documents from `/specs/004-validation-authority/`

**Prerequisites**: plan.md, spec.md, research.md, data-model.md, contracts/, quickstart.md

**Tests**: INCLUDED and mandatory. Constitution Principle X (NON-NEGOTIABLE): tests are written and observed failing before the implementation they cover; the 16.1 and 16.2 vectors run unchanged throughout.

**Organization**: Tasks are grouped by user story. All Python paths are under `ports/py/` (plan.md, Structure Decision); shared vectors live in `conformance/` at the repo root. Features 001 to 003 are implemented and are the base.

## Format: `[ID] [P?] [Story] Description`

- **[P]**: Can run in parallel (different files, no dependency on an incomplete task)
- **[Story]**: User story from spec.md (US1 to US5). Setup, Foundational and Polish tasks have no story label.

## Story map

| Story | Priority | Subject |
|---|---|---|
| US1 | P1 | Reject bad values at the door with a library of rule types (`min_len`, `pattern`, `one_of`, `unique_items`) |
| US2 | P1 | Only the right people may write a field (authority: inheritance, delete rule, reasons, `explain`, schema check) |
| US3 | P2 | Check rules that span several fields (`all_or_none`, `at_least_one`, `at_most_one`, `ordered`) |
| US4 | P2 | Register a custom rule with declared arguments |
| US5 | P3 | Rules on computed values, never on deferred ones |

Build order follows dependencies, not numbers: Foundational first (matcher, argument declaration, loader checks, authority lookup), then US1 and US2 (the P1 pair, independent of each other), then US3, US4, US5 and Polish.

---

## Phase 1: Setup

- [X] T001 Extend `ports/py/tests/unit/test_purity_imports.py` (Principle III): assert `behaviors/pattern.py` exists and is pure (no `re`, no clock, no IO); it fails until the module exists (T006)
- [X] T002 [P] Add `ports/py/tests/rule_helpers.py`: a small service builder with a schema text and an optional `register(behaviors)`, `put`/`delete` entry helpers with tiers and roles, and a helper that asserts no reason text contains a submitted value
- [X] T003 [P] Extend `conformance/README.md` with the new cases `validation-rules`, `document-rules` and `authority`, the `explain` fields `authority` and `authority_from`, and the rule that a rejection's reasons never contain the submitted value, as in `contracts/conformance-vectors.md`
- [X] T004 [P] Add `authority` and `authority_from` to `Explanation` in `ports/py/src/aduku/model.py` (default `None`) with a unit test in `ports/py/tests/unit/test_model_authority.py` per FR-022

---

## Phase 2: Foundational (blocks all stories)

The pattern matcher, the argument declaration, the generic rule checks of the loader and the authority lookup. Every story reads them.

- [X] T005 [P] Write failing tests `ports/py/tests/unit/test_pattern.py`: literals and escapes, `.` (not a newline), classes with ranges and negation, groups, alternation, `*` `+` `?` `{m}` `{m,}` `{m,n}`, whole-value matching, an empty pattern, nested groups; refusals (bad grammar, unbalanced brackets, an empty class, `{n,m}` with n above m, a repeat above 255, a pattern over 500 characters, more than 5,000 matcher states); a 10,000-character value against `(a|aa)*b`, `(a*)*b` and `(a|a)*b` finishes in under 100 ms (FR-004, SC-008)
- [X] T006 Implement `ports/py/src/aduku/behaviors/pattern.py` (pure): parse the subset of `contracts/rules.md` to a tree, build the matcher, `compile_pattern(text) -> Pattern` (cached) and `Pattern.matches(value) -> bool` by simulating the set of matcher states; raise `PatternError` for every refusal above; make T005 pass
- [X] T007 [P] Write failing tests `ports/py/tests/unit/test_rule_registration.py`: `ArgSpec` kinds (`int`, `number`, `text`, `bool`, `path`, `paths`, `values`, `pattern`) accept and refuse values; unknown, missing, wrong-kind, out-of-range and out-of-choices arguments are each reported as a problem; `paths` below its minimum; `applies` field, document and either; a registration with both `args` and `check_args` runs both; a Feature 1 registration with neither is unchanged (FR-009, FR-013)
- [X] T008 Add `ArgSpec`, `Behavior.args` and `Behavior.applies` and the extended `register_validate(..., args=None, applies="either")` to `ports/py/src/aduku/behaviors/registry.py`, with the problem list built from the declaration and `pattern` arguments checked through `compile_pattern`; make T007 pass
- [X] T009 Re-declare `non_empty`, `range` and `max_len` with `ArgSpec` in `ports/py/src/aduku/behaviors/validate.py` (names, arguments, messages and codes unchanged; they are field rules, `applies="field"`); a test shows every schema of the existing vectors and tests still loads; the existing validator and vector tests must still pass unchanged
- [X] T010 [P] Write failing tests `ports/py/tests/unit/test_rule_declaration.py` for the loader: `RULE_WRONG_PLACE` (a document rule on a field and the reverse), `BAD_BEHAVIOR_ARGS` for each argument problem, `UNKNOWN_RULE_PATH`, `DEPENDS_ON_DEFERRED` through a `path`/`paths` argument (and still through the old `depends` form), `DUPLICATE_RULE_SCOPE`, `EMPTY_AUTHORITY`, `UNKNOWN_TIER`; every problem is reported together; registered custom rules with `args` are checked the same way (FR-009, FR-021)
- [X] T011 Implement the generic rule checks in `ports/py/src/aduku/schema/loader.py`: read each use against its registration (`applies`, `args`, path arguments found by kind, joined to the deferred refusal and to the declared-path check), the duplicate-scope check for document rules, and `EMPTY_AUTHORITY`; make T010 pass
- [X] T012 [P] Write failing tests `ports/py/tests/unit/test_authority_for.py`: the field's own list; a child without a list takes its parent's; a child's own list replaces it; two levels up; a wildcard pattern with a list; an exact declaration beats a wildcard; no list anywhere is `None`; the declaring pattern is reported (FR-017)
- [X] T013 Add `Schema.authority_for(path)` to `ports/py/src/aduku/schema/model.py` (pure, memoized like `field_for`) returning the list and the declaring pattern; make T012 pass

---

## Phase 3: User Story 1 - Reject bad values at the door with a library of rule types (Priority: P1)

**Goal**: `min_len`, `pattern`, `one_of` and `unique_items` work at admission and as model errors. **Independent test**: US1 acceptance scenarios 1 to 8.

- [X] T014 [P] [US1] Write failing tests `ports/py/tests/unit/test_field_rules.py`: for each of `min_len`, `pattern`, `one_of` and `unique_items` good and bad values (boundaries, an empty list, `1` against `1.0` in `one_of` and `unique_items`, two equal structs), a kind of value the rule does not apply to reports nothing, and no message contains the value (FR-003 to FR-006, FR-008)
- [X] T015 [US1] Implement `min_len`, `pattern`, `one_of` and `unique_items` in `ports/py/src/aduku/behaviors/validate.py` with their `ArgSpec` declarations and the messages of `contracts/rules.md`, and register them in `register_validators`; make T014 pass (FR-001, FR-005)
- [X] T016 [P] [US1] Write failing integration tests `ports/py/tests/integration/test_field_rules_service.py` (both backends): a good value is accepted, a bad one is rejected with the field and the rule named and the value absent from every reason; two bad values on two fields are listed together and nothing is recorded; a lenient write by a permitted tier is recorded with one model error per violation; the same bad value from a tier not permitted to use the flag is `LENIENT_NOT_PERMITTED`; each rule works on a computed field (FR-010, FR-011, FR-016, SC-003)
- [X] T017 [P] [US1] Write the differential property test `ports/py/tests/property/test_pattern_properties.py`: for random patterns generated within the subset and random values, `Pattern.matches` equals `re.fullmatch` of the same pattern (SC-001, FR-004)
- [X] T018 [US1] Write the vector `conformance/validation-rules.ion`: every field rule with good and bad values, the lenient write and the two-reasons case, per `contracts/conformance-vectors.md`; green when T015 and T016 are done (FR-025)

---

## Phase 4: User Story 2 - Only the right people may write a field (Priority: P1)

**Note**: the `explain` authority tests (T021) were written together with their code (T022), and the document rules (T028) with their unit tests (T027), so those were not observed failing first; the service-level tests of T029 and the vectors were.
**Goal**: authority with inheritance, the delete rule, reasons without the list, and `explain` and the schema check showing the effective list. **Independent test**: US2 acceptance scenarios 1 to 10.

- [X] T019 [P] [US2] Write failing integration tests `ports/py/tests/integration/test_authority.py` (both backends) for US2 scenarios 1 to 8: tier, role, a child inheriting, a child overriding, a wildcard, a delete across declared children (the reason names the child), several entries listed together with nothing recorded, the lenient flag never passing authority, `LENIENT_NOT_PERMITTED`, a derived path under every authority shape; every reason has the field and `rule == "authority"` and never lists the allowed tiers and roles (FR-018, FR-019, FR-020, FR-024)
- [X] T020 [US2] Use the effective authority in `ports/py/src/aduku/engine/admission.py`: `_may_write` takes the list from `authority_for`, a delete checks the path and every declared field beneath it by effective list, and the reasons carry `rule="authority"`, the field and the new wording; make T019 pass
- [X] T021 [P] [US2] Write failing tests `ports/py/tests/unit/test_explain_authority.py`: `explain` reports `authority` and `authority_from` for a field with its own list, one that inherits, one with none (`None`, `None`), and for a path beneath a wildcard (FR-022)
- [X] T022 [US2] Fill `authority` and `authority_from` in `ports/py/src/aduku/engine/explain.py` from `Schema.authority_for`; make T021 pass
- [X] T023 [P] [US2] Write failing CLI tests in `ports/py/tests/integration/test_cli.py`: `schema check` lists per field `authority`, `authority_from` and `rules`, and `document_rules`; `explain` prints the authority; an unauthorized `append` exits 1 and prints the reason without the list; the new load codes exit 2 ; over `conformance/customer.schema.ion` (the SPEC 16.1 schema) `schema check` and `explain` report the effective authority of every field (`/plan` admin, the rest none) (FR-023, SC-010)
- [X] T024 [US2] Add the authority and rule inventory to `ports/py/src/aduku/cli.py` (`_inventory` and the `explain` output); make T023 pass
- [X] T025 [P] [US2] Write the property tests `ports/py/tests/property/test_authority_properties.py` (Memory and SQLite): for random schemas with authority lists and random requests, a rejected request leaves the ledger, views and model errors unchanged, and a request flagged lenient is never accepted where the same request unflagged fails the authority check (SC-004, SC-005)
- [X] T026 [US2] Write the vector `conformance/authority.ion`: tier, role, inheritance, override, wildcard, delete across children, several entries, the lenient flag, a derived path, and `explain` with `authority` and `authority_from`; green when T020 and T022 are done (FR-025)

---

## Phase 5: User Story 3 - Check rules that span several fields (Priority: P2)

**Goal**: `all_or_none`, `at_least_one`, `at_most_one` and `ordered` on the merged document. **Independent test**: US3 acceptance scenarios 1 to 8.

- [X] T027 [P] [US3] Write failing tests `ports/py/tests/unit/test_document_rules.py`: each document rule satisfied and broken; null counts as no value; `ordered` with equal values, a number against a text, a timestamp against a number, and either path without a value; the scope is the first path; no message contains a value (FR-002, FR-007, FR-008)
- [X] T028 [US3] Implement `all_or_none`, `at_least_one`, `at_most_one` and `ordered` in `ports/py/src/aduku/behaviors/validate.py` with a shared `has_value` and `compare` (numbers by value, text by code point, timestamps by instant) and their `ArgSpec` declarations; make T027 pass
- [X] T029 [P] [US3] Write failing integration tests `ports/py/tests/integration/test_document_rules_service.py` (both backends): a request whose entries satisfy a rule only together is accepted; a breaking request is rejected; a permitted lenient request is recorded as a model error; on a document that already has an error an unflagged request is accepted only if it adds none (`MODEL_ERROR_INTRODUCED` otherwise); activating a schema version that adds a rule shows the model error attributed to the version while the ledger is unchanged, equal to a rebuild; a rule over an undeclared or deferred path is refused (FR-010, FR-011, FR-012, SC-002, SC-003, SC-009)
- [X] T030 [US3] Write the vector `conformance/document-rules.ion`: the four rules satisfied, broken and satisfied together, null as no value, incomparable kinds, no-new-errors, and a schema version that adds a rule; green when T028 is done (FR-025)

---

## Phase 6: User Story 4 - Register a custom rule with declared arguments (Priority: P2)

**Goal**: a custom field or document rule with declared arguments is checked at load, pinned by version, and a crash is a fault. **Independent test**: US4 acceptance scenarios 1 to 5.

- [X] T031 [P] [US4] Write failing integration tests `ports/py/tests/integration/test_custom_rules.py` (both backends): a custom field rule and a custom document rule with `ArgSpec` arguments run at the same stages as library rules; each way of breaking the declaration is refused with the stable code; a rule that raises, and one that returns something other than a list of violations, rejects a request with a rule fault and makes a read fail, never reading as no violations; the rule sees no deferred value; replacing a registered version does not change documents of a schema that names the older one (FR-013, FR-014, FR-015, SC-007)
- [X] T032 [US4] Fix whatever T031 exposes in `ports/py/src/aduku/engine/validate.py`, `ports/py/src/aduku/engine/model_errors.py` or the loader (a fault reported as a rejection with its message, the version resolved from the schema), without weakening the tests
- [X] T033 [P] [US4] Add `ports/py/examples/demo_rules.py` (registers `acme.tax_id@1` with arguments) and `ports/py/examples/rules.schema.ion` (every rule and a parent/child authority) as described in `quickstart.md`

---

## Phase 7: User Story 5 - Rules on computed values, never on deferred ones (Priority: P3)

**Goal**: every rule type honors computed values and refuses deferred ones. **Independent test**: US5 acceptance scenarios 1 to 4.

- [X] T034 [P] [US5] Write failing tests `ports/py/tests/integration/test_rules_derived.py` (both backends): each new field rule on a computed field and a document rule over a computed and a plain field reject or record as stated; a computed field missing its inputs reports nothing and adds no error; a rule on or over a deferred field is refused at load (including through `paths`, `first` and `second`); a deferred field is never validated (FR-015, FR-016)
- [X] T035 [US5] Fix whatever T034 exposes in `ports/py/src/aduku/engine/model_errors.py` or the loader, without weakening the tests

---

## Phase 8: Polish and cross-cutting

- [X] T036 [P] Extend the vector `conformance/schema-load-failures.ion` and `ports/py/tests/unit/test_schema_load_failures.py` with the cases of `contracts/conformance-vectors.md`: `RULE_WRONG_PLACE`, each `BAD_BEHAVIOR_ARGS` cause, a bad pattern, `UNKNOWN_RULE_PATH`, `DEPENDS_ON_DEFERRED` through a path argument, `DUPLICATE_RULE_SCOPE`, `EMPTY_AUTHORITY` (SC-006)
- [X] T037 [P] Write and pass `ports/py/tests/integration/test_rules_performance.py`: a worst-shape pattern against a 10,000-character value in under 100 ms; 50 rule declarations on a 1,000-entry document add under 50 ms to a derivation (SC-008)
- [X] T038 [P] Update `docs/SPEC.md` where the build differs from the draft (10.6, 11, the change-class table in 14 for an authority change and for a rule added in a new version) and add the changelog line; update `docs/notes.md`, `ports/py/README.md`, `README.md` and `specs/001-core-ledger-merge-engine/contracts/library-api.md` for `register_validate(args=, applies=)` and the new reasons
- [X] T039 [P] Export `ArgSpec` from `ports/py/src/aduku/__init__.py` (lazily) and extend `ports/py/tests/unit/test_public_api.py`; update `CLAUDE.md` (current step) and `docs/prompts/specify-features.md` (mark Feature 4 done)
- [X] T040 Run the whole suite with the CI hypothesis profile, `uv run ruff check . && uv run ruff format --check . && uv run mypy`, the purity import test, the 16.1 and 16.2 vectors and `quickstart.md` end to end (fix discrepancies in the quickstart or the code); run the full suite five times to catch timing-dependent tests

---

## Dependencies and execution order

- **Phase 1** has no dependencies; its four tasks can run together.
- **Phase 2** needs Phase 1. The pattern matcher (pattern tests, then matcher) must precede the argument declaration, because a `pattern` argument is checked by compiling it; the argument declaration precedes the conversion of the three existing rules and the loader checks; the authority lookup is independent of the others.
- **US1** needs Phase 2. **US2** needs the authority lookup and may run in parallel with US1. **US3** needs the argument declaration and the loader checks. **US4** needs Phase 2. **US5** needs US1 and US3.
- **Phase 8** is last.

## Parallel examples

- Phase 2 tests first, together: the pattern tests, the registration tests, the loader declaration tests and the authority lookup tests.
- US1 and US2 can be built side by side: they touch `behaviors/validate.py` and `engine/admission.py`.
- Within a story, the unit tests and the integration tests are written in parallel; the implementation follows.

## Implementation strategy

1. **MVP**: Phases 1 and 2, then US1 and US2: the four field rules and a complete authority, with the 16.1 and 16.2 vectors unchanged.
2. Add US3 (document rules), US4 (custom rules) and US5 (computed and deferred values).
3. Finish with the load-failure vector, performance, documentation and the final run. After each story, run its independent test from spec.md and keep the whole suite green.
4. After Phase 8 run `/speckit-analyze` and `/speckit-converge`.

---

## Phase 9: Analysis fixes

Found by `/speckit-analyze` after Phase 8 and fixed test-first where the behavior changed.

- [X] T041 Make a rule fault name only the kind of error the rule raised (`it raised RuntimeError`), in `ports/py/src/aduku/engine/validate.py`; replace the vacuous assertion and the either-or check in `ports/py/tests/integration/test_custom_rules.py` with exact ones per FR-008, FR-014 (partial)
- [X] T042 Report no authority for a derived path in `explain` (`ports/py/src/aduku/service.py`) and say so in `contracts/library-api.md`; test in `ports/py/tests/unit/test_explain_authority.py` per FR-022, FR-024 (partial)
- [X] T043 Test a rule on a recorded impure value in `ports/py/tests/integration/test_rules_derived.py` per FR-016 (partial)
- [X] T044 Correct the class grammar of `contracts/rules.md` to what the matcher accepts per FR-004 (partial)

---

## Phase 10: Code review fixes

Found by `/code-review` after Phase 9, checked against the code, and fixed test-first. Checked and left as they are: two uses of a document rule that start at the same path are refused on purpose (they would be one model error); a null is no value for a field rule (now stated in the spec); `one_of` and `unique_items` compare by canonical form (as stated); `non_empty` and `unique_items` refusing an argument is the fail-closed rule (now stated in `schema-format.md`); `authority_of` and `authority_for` agreed on every case tried, and a test now keeps them so.

- [X] T045 Report a malformed `path`/`paths` argument as `BAD_BEHAVIOR_ARGS` instead of crashing the loader: `_rule_paths` in `ports/py/src/aduku/schema/loader.py`. Test: `test_a_malformed_path_argument_is_a_reported_problem_not_a_crash` in `test_rule_declaration.py` per FR-009 (partial)
- [X] T046 Read a repeat count as ASCII digits only: `ports/py/src/aduku/behaviors/pattern.py`. Test: `test_a_repeat_count_is_ascii_digits_only` in `test_pattern.py` per FR-004 (contradicts)
- [X] T047 Refuse a `paths` list that names a path twice (`ports/py/src/aduku/behaviors/registry.py`). Test: `test_a_paths_list_may_not_name_a_path_twice` per FR-009 (partial)
- [X] T048 Say "these fields cannot be compared" for incomparable values in `ordered` (`ports/py/src/aduku/behaviors/validate.py`); `contracts/rules.md` updated. Test: `test_ordered` per FR-007 (partial)
- [X] T049 Work out the effective authority of every declared field once per schema (`Schema._authority_table`), keep `authority_of` and `authority_for` in agreement with a test (`test_authority_for.py`), and state the null and argument-less rule behavior in the spec and contracts per FR-017, FR-009 (partial)
