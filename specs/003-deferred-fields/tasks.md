# Tasks: Deferred Computed Fields

**Input**: Design documents from `/specs/003-deferred-fields/`

**Prerequisites**: plan.md, spec.md, research.md, data-model.md, contracts/, quickstart.md

**Tests**: INCLUDED and mandatory. Constitution Principle X (NON-NEGOTIABLE): tests are written and observed failing before the implementation they cover; the SPEC 16.1 vector with the deferred values (T024) is written first.

**Organization**: Tasks are grouped by user story. All Python paths are under `ports/py/` (plan.md, Structure Decision); shared vectors live in `conformance/` at the repo root. Features 001 and 002 are implemented and are the base.

## Format: `[ID] [P?] [Story] Description`

- **[P]**: Can run in parallel (different files, no dependency on an incomplete task)
- **[Story]**: User story from spec.md (US1 to US8). Setup, Foundational, CLI and Polish tasks have no story label.

## Story map

| Story | Priority | Subject |
|---|---|---|
| US1 | P1 | A value fetched once and kept (`once`, the stored record, the overlay) |
| US2 | P1 | A value fetched on every read, within a time limit (`per_fetch`, `ttl`, one deadline) |
| US3 | P1 | A deferred field that needs another deferred field (promises, levels, single call) |
| US4 | P2 | Values refreshed in batches across documents (`scheduled`, scheduler) |
| US5 | P2 | Values recomputed when an event arrives (`notification`, `notify`) |
| US6 | P2 | A failing source never harms the document (`on_failure`, retries, breaker) |
| US7 | P2 | Deferred values can never leak into the core (isolation, non-writability, explain) |
| US8 | P3 | Seeing what is pending, and why (`include: pending`, `invalidate`) |

Build order follows dependencies, not numbers: Foundational first (declaration and load checks, schedule, call policy, breaker, derived store, runner), then US1, US2 and US3 (the P1 core: T024 stays red until US2 supplies `fxRate`), then US4, US5, US6, US7, US8, then the CLI and Polish.

---

## Phase 1: Setup

- [X] T001 Update `ports/py/tests/unit/test_purity_imports.py` (Principles III, IV): add `engine/deferred.py` and `engine/schedule.py` to the pure modules (no `aduku.storage`, `aduku.audit`, `aduku.log`, `aduku.service`, `aduku.hooks`, `aduku.fetch`, `aduku.breaker`, `aduku.scheduler`, `sqlite3`, `time`, `datetime`, `threading`, `random`) and assert that no pure package imports `aduku.fetch`, `aduku.breaker` or `aduku.scheduler`; the test fails until the modules exist (T006, T022, T026, T028, T042)
- [X] T002 [P] Extend `ports/py/tests/fixtures.py` with `ManualClock` (one object that serves as the service `Clock` and as the stage's monotonic clock, with `advance(seconds)`), and the fixture sources of `contracts/conformance-vectors.md`: `fx.score@1`, `fx.rate@1`, `fx.rate2@1`, `fx.limit@1`, `fx.batch@1` (batch_max 50, records batch sizes), `fx.down@1`, `fx.flaky@1`, `fx.slow@1` (advances the manual clock past the deadline instead of sleeping); each counts its calls
- [X] T003 [P] Extend `conformance/README.md` with the fixture sources, the manual clock and the steps `clock`, `source_calls`, `invalidate`, `notify`, `scheduler_run` and the new `get` options exactly as in `contracts/conformance-vectors.md`
- [X] T004 [P] Add the data types to `ports/py/src/aduku/model.py` (frozen dataclasses, as `data-model.md`): `DerivedRecord`, `DeferredStatus`, `DeferredInfo`, `SchedulerRun`, `SchedulerReport`, and `DocumentResult.deferred: tuple[DeferredStatus, ...] = ()`; unit test in `ports/py/tests/unit/test_model_deferred.py` (construction, defaults, equality) per FR-004

---

## Phase 2: Foundational (blocks all stories)

Declaration, schedule, graph levels, call policy, breaker, derived store and the conformance runner. The schema parsing and every load check of FR-021 land here, because all stories read a parsed `DeferredSpec`.

- [X] T005 [P] Write failing tests `ports/py/tests/unit/test_schedule.py`: an interval fires `every` after the last time; 5-field cron with `*`, lists, ranges and steps, day-of-month and day-of-week, month ends and leap days, UTC; `next_fire` is strictly after its argument; bad expressions, an `every` under 1 s and an empty field are refused (FR-009, research R-8)
- [X] T006 Implement `ports/py/src/aduku/engine/schedule.py` (pure): `parse_schedule(spec) -> Schedule` and `next_fire(schedule, after) -> datetime`; no IO, no clock; make T005 pass per FR-009
- [X] T007 [P] Write failing tests `ports/py/tests/unit/test_deferred_declaration.py` for every row of `contracts/schema-format.md`: a full declaration parses with defaults (timeout 2 s, retry 2 / 200 ms, deadline 6 s, `on_failure` error, `on_dependency_change` keep, `on_missing` null); one policy and a list; `deferred_on_failure` at schema level; each of `DEFERRED_DECLARATION_INCOMPLETE`, `BAD_REFRESH_POLICY`, `BAD_DEFERRED_OPTION` (`ttl` rules, `on_dependency_change` without `once`, bounds 30 s / 5 / 10 s / 60 s, bad duration, unknown values, `invalidate_by` naming an unknown tier or role), `UNKNOWN_BEHAVIOR` for an unregistered source, `UNKNOWN_DEPENDENCY`, `DEPENDENCY_CYCLE` through deferred fields, `DERIVED_HAS_MERGE_OR_DEFAULT`; every problem is reported together (FR-021)
- [X] T008 [P] Write failing tests `ports/py/tests/unit/test_hook_registration.py`: `register_hook(..., batch_max, rate_limit, with_context)` stores the metadata on the `Behavior`; `batch_max` below 1 or `rate_limit` not above 0 is a `SchemaError`; a Feature 002 registration without options is unchanged (FR-031, research R-7)
- [X] T009 Extend `register_hook` in `ports/py/src/aduku/behaviors/registry.py` with `batch_max`, `rate_limit` and `with_context` (fields on `Behavior`); make T008 pass per FR-031
- [X] T010 Add `DeferredSpec` and `RefreshPolicy` to `ports/py/src/aduku/schema/model.py` (`FieldSpec.deferred`, `Schema.deferred_on_failure`, the shape digest of `data-model.md`), include the declaration in `rules_fingerprint`, and export through `schema/__init__` if the package does; keep `derived == "deferred"` and `derived_spec` as they are for Features 001 and 002
- [X] T011 Parse and check the deferred declaration in `ports/py/src/aduku/schema/loader.py` (codes and bounds of `contracts/schema-format.md`, durations reuse the Feature 002 parser, the hook is resolved against `BehaviorRegistry` as a hook, `invalidate_by` against the schema's tiers and roles); make T007 pass per FR-021, FR-022
- [X] T012 [P] Extend `ports/py/tests/unit/test_dependency_graph.py` with failing tests for `deferred_levels`: levels of deferred fields (a field is one level above the deepest deferred field it depends on), ties by path in code-point order, the same for any declaration order, a deferred field depending on computed and impure fields is level 0 (FR-013, FR-014)
- [X] T013 Add `deferred_levels(schema) -> tuple[tuple[FieldSpec, ...], ...]` to `ports/py/src/aduku/engine/graph.py` (pure, iterative); make T012 pass per FR-014
- [X] T014 Migrate the existing deferred fixtures in `conformance/schema-load-failures.ion` and `ports/py/tests/unit/test_schema_load_failures.py` (and `ports/py/tests/unit/test_schema_loader.py` if it declares one): every `deferred:` gains `refresh` and `invalidate_by`; add cases for `DEFERRED_DECLARATION_INCOMPLETE`, `BAD_REFRESH_POLICY` and `BAD_DEFERRED_OPTION` to the vector file, per `contracts/conformance-vectors.md` (research R-13)
- [X] T015 [P] Write failing tests `ports/py/tests/integration/test_derived_store.py`, run on both backends via the `backend` fixture: put and get a `DerivedRecord` (latest per document and field), list by field across documents, mark invalidated, replace on a new write, scheduler runs and leases (take, renew, expire, release, refused while live), and an existing SQLite database from Feature 002 gains the tables without losing views or effects (FR-004, research R-2, R-9)
- [X] T016 Extend the `Tx` interface in `ports/py/src/aduku/storage/base.py` with `get_derived(doc)`, `put_derived(records)`, `list_derived(fields, docs=None)`, `mark_invalidated(fields, docs=None)`, `append_run(run)`, `list_runs(hook, limit=None)` and `take_lease(hook, owner, expires, now)` / `renew_lease` / `release_lease`; no existing signature changes
- [X] T017 [P] Implement the derived store, runs and leases in `ports/py/src/aduku/storage/memory.py`; part of making T015 pass
- [X] T018 [P] Implement `derived_values`, `scheduler_runs` and `scheduler_leases` in `ports/py/src/aduku/storage/sqlite.py` (created by `CREATE TABLE IF NOT EXISTS`, so an old database opens unchanged; a lease is taken with one atomic statement); finish T015
- [X] T019 [P] Write failing tests for the call policy in `ports/py/tests/unit/test_hook_runner.py`: `HookRunner.run` takes a `CallPolicy(timeout, retry_max, backoff, deadline)` and an optional `deadline_at` (monotonic time) that caps the call; Feature 002 `ImpureSpec` calls behave as before; a batch call returns one result per item with per-item failures; `with_context` passes `ctx` (FR-002, FR-008, FR-017)
- [X] T020 Refactor `ports/py/src/aduku/hooks.py`: add `CallPolicy` (with `ImpureSpec.policy` and `DeferredSpec.policy`), `run(call, policy, inputs, *, deadline_at=None, ctx=None)` and `run_batch(call, policy, items, *, deadline_at=None)`; keep a thin form for Feature 002 callers; make T019 and the existing runner tests pass
- [X] T021 [P] Write failing tests `ports/py/tests/unit/test_breaker.py`: per source key; opens after 5 consecutive failed calls; refuses while open; after 30 s exactly one trial call is allowed and concurrent callers are refused until it ends; success closes, failure re-opens; a refused call and a `rate_limited` refusal do not count toward the threshold; state changes are reported to a callback; token bucket honors `rate_limit` and is exact with a manual clock (FR-017, SC-008, research R-10)
- [X] T022 Implement `ports/py/src/aduku/breaker.py` (edge): `Breaker`, `BreakerBank` (by `name@version`, thread-safe, injected monotonic clock and thresholds) and `RateLimiter`; make T021 pass per FR-017
- [X] T023 Extend the conformance runner `ports/py/tests/conformance_runner.py` with the manual clock, the fixture sources and the steps `clock`, `source_calls`, `invalidate`, `notify`, `scheduler_run` and the `get` options `consistency`, `include` and an expected `deferred` map, as `contracts/conformance-vectors.md`; the runner reports an unknown step clearly (FR-032)

---

## Phase 3: User Story 1 - A value fetched once and kept (Priority: P1)

**Goal**: a `once` field is fetched on first read, stored, shown as `deferred::`, and kept. **Independent test**: US1 acceptance scenarios 1 to 6 with `fx.score@1`. The 16.1 vector is written first and stays red until US2 supplies `fxRate`.

- [X] T024 [US1] Write the failing golden vector first: extend `conformance/worked-example-16-1.ion` and `ports/py/tests/golden/test_worked_example.py` with `creditScore: deferred::712` (once, `fx.score@1`) and `fxRate: deferred::0.92` (per_fetch, `fx.rate@1`) and the rest of the document unchanged, `deferred` statuses `ok`, per SPEC 16.1 (SC-010, FR-032)
- [X] T025 [P] [US1] Write failing planner tests `ports/py/tests/unit/test_deferred_planner.py` (once): no record and inputs ready gives a fetch with reason `first`; a current record gives none; changed inputs give none without opt-in and a fetch with `on_dependency_change: recompute`; a shape change (source version, depends, type) makes the record not reusable; missing or null inputs give `pending` with `on_missing` display and no fetch; `invalidated` gives a fetch (FR-005, FR-006, FR-016, FR-030)
- [X] T026 [US1] Implement the planner core in `ports/py/src/aduku/engine/deferred.py` (pure): `plan(schema, document, records, now, consistency, mode)` returning `Plan`/`FieldPlan` per `data-model.md`; the `once` rules, shape digest check, `pending` and `on_missing` display, `show` values; make T025 pass
- [X] T027 [P] [US1] Write failing integration tests `ports/py/tests/integration/test_deferred_once.py` (both backends): first read calls once and stores; second read calls nothing; a name change keeps the value; the opt-in refetches once; inputs missing means pending and no call; a rejected or accepted `append` makes no call; deleting the derived records and rebuilding leaves the core document, computed fields and model errors identical and makes no call; an answer of the wrong type, or `None`, is a failure and is not stored or shown; an empty document (no entries) calls nothing; with two schema versions, one that changes the source version or `depends` fetches again and one that leaves the declaration alone keeps the stored `once` value (FR-003, FR-005, FR-006, FR-019, FR-024, FR-030, SC-004)
- [X] T028 [US1] Implement `ports/py/src/aduku/fetch.py` (edge) `DeferredStage`: take records from the store, run `plan`, call due `once`/`per_fetch` sources through `HookRunner` and the breaker, check the answer against the field type, write the record (value, `as_of`, fingerprint, hook, shape, status, attempts) in a short write transaction after the calls, never under the ledger write lock; unit seam for a fake runner
- [X] T029 [US1] Overlay deferred values in `ports/py/src/aduku/service.py`: `Aduku(..., fetch_deadline=5.0, monotonic=None)`, `get` applies the stage after the cached P1-P6 view and returns `Annotated(("deferred",), value)` plus `DocumentResult.deferred`; `AppendResult.view` shows stored values with no call; `ViewRow` and the cached view stay free of deferred values; make T027 and the first part of T024 pass per FR-001, FR-019, FR-023

---

## Phase 4: User Story 2 - A value fetched on every read, within a time limit (Priority: P1)

**Goal**: `per_fetch` with `ttl`, the fingerprint rule, `consistency: fresh`, one deadline for the whole read, stale fallback. **Independent test**: US2 acceptance scenarios 1 to 6 and 5a.

- [X] T030 [P] [US2] Extend `ports/py/tests/unit/test_deferred_planner.py` with failing `per_fetch` cases: fetch when no record, invalidated, inputs changed (even inside `ttl`), `now - as_of >= ttl`, or `fresh`; no fetch when inputs are unchanged and the record is younger than `ttl` (FR-007, clarification 1)
- [X] T031 [US2] Add the `per_fetch` rules and the `consistency` argument to `plan` in `ports/py/src/aduku/engine/deferred.py`; make T030 pass
- [X] T032 [P] [US2] Write failing integration tests `ports/py/tests/integration/test_deferred_per_fetch.py` (both backends, manual clock): reuse inside `ttl`, refetch after it, refetch on a changed input inside `ttl`, `fresh` ignores `ttl`; `fx.slow@1` read returns by the fetch deadline with the field following `on_failure`; several independent slow fields share one deadline and unfinished fields follow their policy (the chain case is in T036); a late valid answer is stored for the next read; with `serve_stale` the last good value shows as `stale`; with a source `rate_limit` of 1 per second, reads beyond the limit make no call and follow `on_failure` with kind `rate_limited` without delaying the read (FR-007, FR-008, FR-018, SC-003)
- [X] T033 [US2] In `ports/py/src/aduku/fetch.py` process the plan level by level, run the calls of one level concurrently, cap every call with the read's `deadline_at`, abandon calls that outlive it, store a conforming late answer, apply `on_failure` for the read, and take a rate-limiter token before each call (a read that finds none makes no call and records the kind `rate_limited`, which the breaker does not count); in `ports/py/src/aduku/service.py` add `get(doc, *, consistency="cached", include=())`; make T032 and T024 pass per FR-008

---

## Phase 5: User Story 3 - A deferred field that needs another deferred field (Priority: P1)

**Goal**: promises: a source runs only when every deferred input has a value, in a deterministic order. **Independent test**: US3 acceptance scenarios 1 to 5.

- [X] T034 [P] [US3] Extend `ports/py/tests/unit/test_deferred_planner.py` with failing promise cases: a dependent of a `pending` upstream is `pending` (reason `upstream`) and not fetched; a dependent of an `ok` or `stale` upstream is fetched with that value; an upstream in `error` with nothing stale holds dependents back; chains are ordered by level then path; the plan is the same for any field declaration order (FR-013, FR-014, FR-015)
- [X] T035 [US3] Add the promise rules, levels and `waiting_for` / `reason` to `plan` in `ports/py/src/aduku/engine/deferred.py`; make T034 pass
- [X] T036 [P] [US3] Write failing integration tests `ports/py/tests/integration/test_deferred_promises.py`: `creditLimit` depends on `creditScore` (deferred) and `/country` (plain): with the score source down the limit is pending and its source is never called; with it up they are fetched in order; a source receives only its declared dependencies and the `ctx` it asked for (`with_context`); a `once` dependent keeps its value when the upstream refreshes unless it opted in; a deferred field whose `depends` include a computed field and an impure field reads their current and recorded values (FR-002, FR-013, SC-005)
- [X] T037 [P] [US3] Write failing test `ports/py/tests/integration/test_deferred_single_flight.py`: two threads reading the same document at the same moment cause one source call and both receive the value; a `scheduler_run` and a read needing the same key share one call too (FR-029, SC-011)
- [X] T038 [US3] Implement the single-flight map for deferred calls in `ports/py/src/aduku/fetch.py` (key: document, field, fingerprint, source), reusing the Feature 002 pattern, and make T036 and T037 pass per FR-029

---

## Phase 6: User Story 4 - Values refreshed in batches across documents (Priority: P2)

**Goal**: `scheduled` fields refreshed by `scheduler_run` in batches, within rate limits, idempotently, with a run record. **Independent test**: US4 acceptance scenarios 1 to 6.

- [X] T039 [P] [US4] Extend `ports/py/tests/unit/test_deferred_planner.py` with failing `scheduled` cases: due when `next_fire(as_of) <= now`, not due before it, first fetch (no record, inputs ready) is the scheduler's, a read never fetches a scheduled-only field and shows it `pending` with reason `not_fetched`, combined `once` plus `scheduled` fetches once on read and then by schedule (FR-009, FR-012)
- [X] T040 [US4] Add the `scheduled` rules and the `mode` (read or scheduler) to `plan` in `ports/py/src/aduku/engine/deferred.py`; make T039 pass
- [X] T041 [P] [US4] Write failing integration tests `ports/py/tests/integration/test_scheduler.py` (both backends, manual clock): 120 documents with a batch source of `batch_max` 50 give three calls (50, 50, 20); a second run immediately does nothing; a rate limit leaves the rest due; an interrupted run (source raises on batch 2) resumed refreshes each document once for that due time; a live lease makes a second run skip with `reason="lease held"` and no call; a read never calls the source even when overdue; the run record has source, counts and times; a caller outside `invalidate_by` gets `NOT_AUTHORIZED` with no call, and a partly permitted caller runs only the fields it may (FR-009, FR-010, FR-025, SC-006)
- [X] T042 [US4] Implement `ports/py/src/aduku/scheduler.py` (edge): pages of due items by document id and field path, grouping by source, batches of `batch_max`, rate limiter, per-batch writes, lease renewal, the run record and structured log event; use the breaker and single-flight
- [X] T043 [US4] Add `scheduler_run(hook, *, caller)` to `ports/py/src/aduku/service.py` (permission against `invalidate_by`, report); make T041 pass per FR-010, FR-025
- [X] T044 [US4] Write the vector `conformance/deferred-policies.ion` (once keeps and opt-in, per_fetch ttl / changed input / fresh, scheduled batches 50-50-20 and idempotence, rate limit, combined policies) using the runner steps; green when T043 is done (FR-032)

---

## Phase 7: User Story 5 - Values recomputed when an event arrives (Priority: P2)

**Note**: the planner tests of T045 passed when first run, because T040 had already written the `notification` rule for the scheduler; the integration tests of T047 failed first, as required.
**Goal**: `notify` marks values out of date without a call; the next scheduler run recomputes them. **Independent test**: US5 acceptance scenarios 1 to 5.

- [X] T045 [P] [US5] Extend `ports/py/tests/unit/test_deferred_planner.py` with failing `notification` cases: an invalidated record is due for the scheduler and not for a read of a `notification`-only field; the flag clears only after a successful fetch (FR-011, FR-020)
- [X] T046 [US5] Add the `notification` and `invalidated` rules to `plan` in `ports/py/src/aduku/engine/deferred.py`; make T045 pass
- [X] T047 [P] [US5] Write failing integration tests `ports/py/tests/integration/test_deferred_notifications.py`: a notification naming one of two documents marks only that one and makes no call; with no documents it marks all that hold a record; another topic changes nothing; a read between notification and run shows the stored value and calls nothing; delivering it twice changes nothing more; a missing document is ignored; the next `scheduler_run` recomputes exactly the marked records (FR-011, SC-007)
- [X] T048 [US5] Add `notify(topic, docs=None)` to `ports/py/src/aduku/service.py` (one short write transaction marking the fields linked to the topic, per document schema) and the structured `notification` log event; make T047 pass per FR-011, FR-028

---

## Phase 8: User Story 6 - A failing source never harms the document (Priority: P2)

**Goal**: `on_failure` modes, retries, the per-source breaker, failure as status only, recovery. **Independent test**: US6 acceptance scenarios 1 to 7.

- [X] T049 [P] [US6] Extend `ports/py/tests/unit/test_deferred_planner.py` with failing failure cases: `error` shows nothing and status `error`; `serve_stale` shows the last good value as `stale` (or `error` with none); `absent` leaves the field out with status `error`; the schema's `deferred_on_failure` applies when the field has none; a stored failure is retried by the next due fetch, not forgotten (FR-018)
- [X] T050 [US6] Add the failure display rules to `plan` in `ports/py/src/aduku/engine/deferred.py`; make T049 pass
- [X] T051 [P] [US6] Write failing integration tests `ports/py/tests/integration/test_deferred_failures.py` (both backends, manual clock): a source failing every call: a write is accepted or rejected exactly as without the field and adds or removes no model error (compare with a schema without the field); each `on_failure` mode reads as specified; retries and backoff counts; after the threshold the breaker stops calls for any field or document using the source and one trial call follows the cool-down; a non-conforming answer is a failure and is not stored; `bad_value`, `timeout`, `deadline`, `breaker_open` kinds; a later success clears the status; `rebuild` is unaffected (FR-017, FR-018, FR-019, FR-020, SC-002, SC-008)
- [X] T052 [US6] Wire failure handling in `ports/py/src/aduku/fetch.py`: record failures (keeping the last good value), apply the breaker and its callback, and emit the structured events `deferred_call` and `breaker` without values or error text; make T051 and T053 pass per FR-017 to FR-020, FR-028
- [X] T053 [P] [US6] Write failing test `ports/py/tests/integration/test_deferred_logging.py`: the events `deferred_call`, `breaker`, `scheduler_run` and `notification` carry document, field, source, counts and result, and no value or error text, even when a source fails with a message containing a secret (FR-028)
- [X] T054 [US6] Write the vector `conformance/deferred-failures.ion` (the three `on_failure` modes, retries, breaker open and trial, write never rejected and no model error, recovery by a later successful read or run) with `fx.down@1`, `fx.flaky@1`, `fx.slow@1`; green when T052 is done (FR-032)

---

## Phase 9: User Story 7 - Deferred values can never leak into the core (Priority: P2)

**Goal**: prove isolation and non-writability for working deferred fields. **Independent test**: US7 acceptance scenarios 1 to 5.

- [X] T055 [P] [US7] Write failing property tests `ports/py/tests/property/test_deferred_properties.py` (Memory and SQLite, hypothesis, CI profile 1,000 examples): for random ledgers and any random content of the derived store (empty, full, stale, invalidated, errors) the document without deferred values, the computed fields, the model errors and the `rebuild` output are byte-identical, and `rebuild` calls no source; the plan is independent of field declaration order (FR-001, FR-024, SC-001)
- [X] T056 [P] [US7] Write failing tests `ports/py/tests/integration/test_deferred_not_writable.py`: an entry on a deferred path, its children, a delete, and a mixed request is rejected with `FIELD_NOT_WRITABLE`, not bypassable by the lenient flag, for each refresh policy; nothing is recorded; a computed field, a field validator, a document rule and a default depending on a deferred field are refused at load with `DEPENDS_ON_DEFERRED` (FR-021, FR-022, SC-009)
- [X] T057 [P] [US7] Write failing tests `ports/py/tests/unit/test_explain_deferred.py` and extend `ports/py/src/aduku/engine/explain.py`: `explain` on a deferred path gives `winner="deferred"`, the source and version, the dependency values, `as_of` and status from the store, never calls a source, and a path with no record says so (FR-027)
- [X] T058 [US7] Implement the deferred branch of `explain` in `ports/py/src/aduku/engine/explain.py` and pass the stored record from `ports/py/src/aduku/service.py`; make T057 pass per FR-027
- [X] T059 [US7] Make T055 and T056 pass: fix whatever the property and non-writability tests expose in `ports/py/src/aduku/service.py` or the engine, without weakening the tests (FR-001, FR-024)

---

## Phase 10: User Story 8 - Seeing what is pending, and why (Priority: P3)

**Goal**: `include: pending`, `invalidate`, `list_deferred`. **Independent test**: US8 acceptance scenarios 1 to 5.

- [X] T060 [P] [US8] Write failing integration tests `ports/py/tests/integration/test_deferred_pending.py`: `include=("pending",)` fills `waiting_for` and `reason` (`inputs_missing`, `upstream`, `not_fetched`) and without it they are empty; an upstream in `error` with nothing stale holds the dependent `pending`; a stale upstream does not; `list_deferred` returns metadata and never a value (FR-015, FR-026)
- [X] T061 [P] [US8] Write failing integration tests `ports/py/tests/integration/test_deferred_invalidate.py`: `invalidate` by a caller whose tier or role is in `invalidate_by` marks the record and the next fetch recomputes; a caller outside the list gets `NOT_AUTHORIZED` with no call and no change; `doc=None` marks every record; an unknown field is `UNKNOWN_PATH`; invalidating a failed field and a successful call clears it; the `invalidate` log event carries caller, document and field and no value (FR-020, FR-025, FR-028)
- [X] T062 [US8] Implement `invalidate` (with its structured log event), `list_deferred` and the `include=("pending",)` detail in `ports/py/src/aduku/service.py`; make T060 and T061 pass per FR-025, FR-026
- [X] T063 [US8] Write the vector `conformance/deferred-promises.ion` (chain order, pending reasons, stale accepted, upstream error holds back, invalidate and recovery); green when T062 is done (FR-032)

---

## Phase 11: CLI, example and public API

- [X] T064 [P] Write failing CLI tests in `ports/py/tests/integration/test_cli.py`: `get --fresh --include pending` prints `deferred::` values and the `deferred` section; `invalidate` (exit 1 on `NOT_AUTHORIZED`); `scheduler run`; `notify`; `deferred list` (no value); `schema check` prints the deferred inventory; per `contracts/cli.md`
- [X] T065 [P] Add `ports/py/examples/demo_sources.py` (sources `fx.score`, `fx.rate`, `fx.limit`, `fx.batch` and a notification source, call counter, `DEMO_DOWN=1`) and `ports/py/examples/deferred.schema.ion` as described in `quickstart.md`
- [X] T066 Implement the CLI commands and options of `contracts/cli.md` in `ports/py/src/aduku/cli.py`; make T064 pass
- [X] T067 [P] Export `DeferredStatus`, `DeferredInfo`, `SchedulerReport` from `ports/py/src/aduku/__init__.py` (lazily, as the other names) and extend `ports/py/tests/unit/test_public_api.py` so every type `contracts/library-api.md` names is importable from `aduku`

---

## Phase 12: Polish and cross-cutting

- [X] T068 [P] Write and pass performance tests `ports/py/tests/integration/test_deferred_performance.py`: a read of a document with 5 fresh deferred fields adds under 5 ms to a cached read and makes no call; a read with a never-answering source ends within 100 ms of the fetch deadline; planning 10,000 due documents takes under 2 seconds excluding calls (Performance Goals, SC-003)
- [X] T069 [P] Update `docs/SPEC.md` where the build differs from the draft (9.5 table, 12.2 note that a read may call deferred sources within the fetch deadline, 12.4, 16 item 11) and add the changelog line; update `docs/notes.md` with the planning decisions (process-local breaker, own cron parser, `notify` not gated, `with_context`) and `ports/py/README.md`, `README.md` and `conformance/README.md`; keep `specs/001-core-ledger-merge-engine/contracts/library-api.md` consistent
- [X] T070 [P] Update `CLAUDE.md` (current step) and `docs/prompts/specify-features.md` (mark Feature 3 done)
- [X] T071 Run the whole suite with the CI hypothesis profile, `uv run ruff check . && uv run ruff format --check . && uv run mypy`, the purity import test and `quickstart.md` end to end (fix discrepancies in the quickstart or the code); run the full suite five times to catch timing-dependent tests; record the result in the Completion report

---

## Dependencies and execution order

- **Phase 1** has no dependencies; T002 to T004 can run in parallel with T001.
- **Phase 2** needs Phase 1. Within it: T006 after T005; T009 after T008; T010 after T007 (the model); T011 (the loader) after T010 and T009 (it resolves hooks); T013 after T012 and T010; T016 before T017 and T018; T020 after T019; T022 after T021; T023 after T002 and the schema pieces (T011). T014 needs T011.
- **US1 (Phase 3)** needs Phase 2. T026 after T025; T028 after T026, T016 to T018 and T020; T029 after T028.
- **US2 (Phase 4)** needs US1; **US3 (Phase 5)** needs US2 (the level processing of T033).
- **US4 (Phase 6)** needs US1 and the breaker; **US5 (Phase 7)** needs US4 (the scheduler recomputes); **US6 (Phase 8)** needs US1 and may overlap with US4 and US5; **US7 (Phase 9)** needs US1 to US3 and benefits from the others; **US8 (Phase 10)** needs US3.
- **Phase 11 (CLI)** needs US1 to US8; **Phase 12** is last.

## Parallel examples

- Phase 2 tests first, together: T005, T007, T010, T012, T015, T019, T021 (different test files).
- Phase 2 implementations that touch different files: T017 and T018; then T006, T011, T013, T020, T022 in parallel.
- Within a story, the planner test and the integration test (for example T025 and T027) are written in parallel; the implementation tasks follow in order.

## Implementation strategy

1. **MVP**: Phases 1 and 2, then US1: a `once` field fetched, stored, shown and kept, with the core unchanged.
2. Add US2 and US3 for the full P1 set (the 16.1 example turns green at T032).
3. Add the batch and event paths (US4, US5), the failure handling (US6), the proof of isolation (US7) and observability (US8).
4. Finish with the CLI, the example and Polish. After each story, run its independent test from spec.md and keep the whole suite green.
5. After Phase 12 run `/speckit-analyze` and `/speckit-converge`.

---

## Phase 13: Code review fixes

Found by `/code-review` after Phase 12, checked against the code, and fixed test-first; each task names its regression test. Checked and left as they are: a `once` field whose first call failed waits for `invalidate` or a changed input (a deliberate choice, now stated in SPEC 9.5), `notify` and `scheduler_run` consider only the active schema (stated in the spec's Assumptions), and one successful item in a mixed batch resets the breaker. The reported unbounded thread joins could not be made to happen (the calls honor the deadline themselves), but the joins now use the time left.

- [X] T072 Do not count a refusal (an open breaker, a rate limit, a wait for another request's call) as an attempt: `record_of` in `ports/py/src/aduku/fetch.py` leaves `last_attempt` and `attempts` as they were, and the planner treats a record that was never attempted as due. Tests: `test_a_refusal_is_not_an_attempt_so_the_next_read_after_the_cool_down_asks_without_waiting_for_the_ttl` and `test_a_rate_limit_refusal_is_asked_again_as_soon_as_a_call_is_allowed` in `test_deferred_failures.py` per FR-017, FR-018 (contradicts)
- [X] T073 Keep a late answer only for the inputs still being asked about, and keep the `invalidated` mark: `_keep_late` in `ports/py/src/aduku/fetch.py`. Tests: `test_a_late_answer_for_old_inputs_does_not_replace_a_value_for_the_current_ones` and `test_a_late_answer_keeps_a_mark_made_after_the_call_started` in `test_deferred_per_fetch.py` per FR-007, FR-020 (partial)
- [X] T074 Keep a mark made while a source call is running on a clean value: `DeferredStage.store` in `ports/py/src/aduku/fetch.py`. Test: `test_an_invalidate_that_arrives_while_a_fetch_is_in_flight_is_not_lost` in `test_deferred_invalidate.py` per FR-020 (partial)
- [X] T075 Stop a scheduler run whose lease was lost (`reason="lease lost"`, the rest skipped): `ports/py/src/aduku/scheduler.py`. Test: `test_a_run_that_loses_its_lease_stops_instead_of_working_beside_another_runner` in `test_scheduler.py` per FR-010 (partial)
- [X] T076 Read cron day fields that start with `*` (`*/2`) as no restriction, as cron does: `ports/py/src/aduku/engine/schedule.py`. Test: `test_a_day_field_that_starts_with_a_star_is_not_restricted_so_both_day_fields_must_match` in `test_schedule.py` per FR-009 (contradicts)
- [X] T077 Use one call path for reads and the scheduler (`run_items` takes `late` and `read_token`, `_call` and `_invoke` are gone), bound the thread joins by the time left, count read rounds from the schema, and put the derived-store tables under their own heading in `ports/py/src/aduku/storage/sqlite.py` per plan: code duplication (unrequested)
- [X] T078 State in SPEC 9.5 when a failed call is asked again (`docs/SPEC.md`) per FR-020 (partial)
