# Implementation Plan: Deferred Computed Fields

**Branch**: `003-deferred-fields` | **Date**: 2026-10-19 | **Spec**: [spec.md](spec.md)

**Input**: Feature specification from `/specs/003-deferred-fields/spec.md`

## Summary

Add the deferred stage (P5) to `aduku-py`: fields whose values come from registered IO hooks ("sources"), kept in a
separate **derived store** and overlaid on the document at projection with the `deferred::` annotation and a status
(`ok`, `stale`, `pending`, `error`). Four refresh policies (`once`, `per_fetch`, `scheduled`, `notification`) that can be
combined; deferred-to-deferred dependencies resolved in topological order ("promises"); per-field failure policy
(`error`, `serve_stale`, `absent`) after bounded retries and a per-source circuit breaker; a failure is only a status,
never a rejection or a model error. Operations: `get(..., consistency, include)`, `invalidate`, `scheduler_run`, `notify`.
Nothing in admission, merge, computed fields, validation or the cached view reads a deferred value, so the pure chain and
its replay guarantees are unchanged.

Technical approach: split the stage into a **pure planner** (`engine/deferred.py`: given the P1-P4 document, the stored
records and a `now` passed in as a value, decide each field's status and which calls are due) and an **edge executor**
(`fetch.py`: calls sources with timeout, retry, backoff and the read's shared deadline, applies the circuit breaker and
rate limit, writes records). The service composes them after the cached P1-P6 view is obtained, so a read with
nothing due costs one store lookup. The derived store is two new tables (`derived_values`, `scheduler_runs`) next to the
ledger; unlike the ledger and the effect log it is mutable by design (latest record per document and field) and is not
part of rebuild equality.

## Technical Context

**Language/Version**: Python 3.12 (as Features 001 and 002)

**Primary Dependencies**: none new at runtime. Cron expressions use a small own parser (research R-8): the standard
5-field subset is about 80 lines and a dependency would be the only one in the package. Dev only: unchanged (`pytest`,
`hypothesis`, `ruff`, `mypy`).

**Storage**: SQLite (WAL) and the in-memory backend behind the existing storage interface, extended with `derived_values`
(latest record per document and field), `scheduler_runs` (one row per run) and `scheduler_leases` (one row per source
being run, with an expiry).

**Testing**: `pytest` with `hypothesis`; test-first (constitution X). New: unit tests for the planner (every status and
reuse rule), the cron and interval schedule, the breaker and rate limiter, and the loader checks; property tests for
planner order-independence and for "the core output never depends on the derived store"; integration tests with
counting sources, a controllable clock and slow/failing sources; golden test for the 16.1 deferred values; shared
conformance vectors (new fixture sources, a clock step).

**Target Platform**: Linux/macOS/Windows, same as Feature 001

**Project Type**: library with a thin CLI (extends `ports/py/`)

**Performance Goals**: a read of a document with 5 deferred fields whose values are all fresh adds under 5 ms to a cached
read and makes no call (SC-004); a read never exceeds the fetch deadline by more than 100 ms (SC-003); one scheduler
run plans 10,000 due documents in under 2 seconds excluding source calls (SC-006).

**Constraints**: `engine/`, `expr/`, `schema/` and `behaviors/` stay free of IO and clocks (`now` is a parameter of the
planner); no source is called while the write lock is held, and the write path makes no deferred call at all (FR-019);
a source is called only with its declared dependencies; values and error text never appear in logs (FR-028).

**Scale/Scope**: tens of deferred fields per document type; one derived record per document and deferred field; a
scheduler run covers up to hundreds of thousands of documents per source, processed in pages.

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

| Principle | Status | How the plan complies |
|---|---|---|
| I. Ledger Is Truth | Pass | Deferred values are never entries and never in the cached view. The derived store holds results only, is written by reads and the scheduler, never by admission, and a rejected request touches nothing. |
| II. Schema Is Source of Truth | Pass | Source, dependencies, policies, `ttl`, `on_failure`, `on_dependency_change`, `on_missing` and `invalidate_by` are declared in the schema and bound to registered hooks; the engine has no per-field special cases. |
| III. Determinism and Purity | Pass | The planner is a pure function of (document, records, now, schema); `now` is an argument. P0-P4, P6 and P7's core are unchanged and import-tested: `engine/deferred.py` joins the purity list, `fetch.py` and `hooks.py` stay outside it. |
| IV. IO Is Confined | Pass | The only IO is in `fetch.py` through `HookRunner` (timeout, bounded retry, deadline) behind a per-source circuit breaker and rate limit, with structured audit events. Deferred fields are never inputs to computed fields, validators or defaults (load-time check, already in place) and never cause rejection or a model error. |
| V. Algebraic Laws (NON-NEGOTIABLE) | Pass | No merge rule is added; deferred fields carry none. Laws and property tests of Feature 001 are untouched. New property: the P1-P4, P6 output is identical for any derived-store content. |
| VI. Derived Fields Never Writable | Pass | P0 already rejects entries on deferred paths; new tests cover reads, mixed requests and every policy. |
| VII. Explainability | Pass | `explain` on a deferred path names the source and version, the inputs, `as_of` and status, from the store, without calling. |
| VIII. Sandboxed Behaviors | Pass | Sources are the declared, audited exception outside the pure sandbox (SPEC 13 item 3). They receive a projection of only the declared dependencies, by construction. |
| IX. Immutable Schema Versions | Pass | Unchanged. A new version that changes a field's source version, dependencies or type makes its stored value not reusable (digest check); otherwise values carry over. |
| X. Test-First and Conformance (NON-NEGOTIABLE) | Pass | Tasks order failing tests first, including the 16.1 deferred values vector, written before the stage is built. |
| XI. Simplicity | Pass | Cross-document derivation, multi-process breaker state, a built-in timer and cron library stay out. See Complexity Tracking for the three additions that need a reason. |

Re-check after Phase 1 design (research.md, data-model.md, contracts/): every row still passes. The design adds no
merge rule, no write-path IO and no import from the pure packages to the edge modules; the one new rule for existing
fixtures (`invalidate_by` is required) is migrated in the same change (research R-13).

Scope and technology constraints: stable key ordering and canonical text apply to the `deferred::` annotation; the
declaration syntax, store record and vectors are specified in `contracts/` so other ports can implement them. The engine
phases P0-P4, P6 never read wall-clock time. No violations.

## Project Structure

### Documentation (this feature)

```text
specs/003-deferred-fields/
├── plan.md              # This file
├── research.md          # Phase 0 output
├── data-model.md        # Phase 1 output
├── quickstart.md        # Phase 1 output
├── contracts/
│   ├── schema-format.md         # delta: the deferred declaration, refresh syntax, defaults and bounds, load checks
│   ├── library-api.md           # delta: get(consistency, include), invalidate, scheduler_run, notify, registration, results
│   ├── cli.md                   # delta: get --fresh/--include, invalidate, scheduler run, notify, deferred list
│   └── conformance-vectors.md   # delta: fixture sources, clock, fetch and scheduler steps
├── checklists/
└── tasks.md             # Phase 2 output (/speckit-tasks; not created here)
```

### Source Code (repository root)

```text
conformance/
├── worked-example-16-1.ion      # expected document gains creditScore and fxRate (deferred::), with the fixture sources
├── schema-load-failures.ion     # extended: deferred declaration errors; existing deferred cases gain invalidate_by
├── deferred-policies.ion        # new: once, per_fetch (ttl, fingerprint), scheduled, notification
├── deferred-failures.ion        # new: on_failure modes, breaker, never rejects, recompute clears
├── deferred-promises.ion        # new: chains, pending, stale accepted
└── README.md                    # fixture sources, the clock step and the new step kinds documented

ports/py/src/aduku/
├── engine/
│   ├── deferred.py              # NEW, pure: the planner (statuses, reuse rules, due fields, levels); no IO, `now` is a parameter
│   ├── schedule.py              # NEW, pure: interval and 5-field cron, next fire time after a time
│   ├── graph.py                 # + deferred_levels (levels of deferred fields, ties by path)
│   └── explain.py               # + deferred paths from the store
├── schema/
│   ├── model.py                 # + DeferredSpec, RefreshPolicy on FieldSpec
│   └── loader.py                # + parse and check deferred declarations, invalidate_by, option/policy match
├── behaviors/
│   └── registry.py              # + register_hook(..., batch_max=None, rate_limit=None) metadata
├── hooks.py                     # CallPolicy shared by impure and deferred calls; run(call, policy, inputs, deadline_at)
├── fetch.py                     # NEW, edge: DeferredStage (fetch a level, single-flight, shared deadline, store writes)
├── breaker.py                   # NEW, edge: per-source circuit breaker and rate limiter (injected clock)
├── scheduler.py                 # NEW, edge: due selection, batching, rate limit, lease, run record
├── storage/
│   ├── base.py                  # + DerivedRecord, SchedulerRun in model.py; Tx derived and scheduler methods
│   ├── memory.py                # + derived store, runs, leases
│   └── sqlite.py                # + derived_values, scheduler_runs, scheduler_leases tables
├── model.py                     # + DerivedRecord, DeferredStatus, SchedulerReport, DocumentResult.deferred
├── errors.py                    # (unchanged codes; NOT_AUTHORIZED reused)
├── service.py                   # get(consistency, include), overlay, invalidate, scheduler_run, notify, explain, append view overlay
└── cli.py                       # + get --fresh/--include, invalidate, scheduler run, notify, deferred list, schema check inventory

ports/py/examples/
└── demo_sources.py              # NEW: register(behaviors) with counting sources, for the quickstart CLI walkthrough

ports/py/tests/
├── unit/                        # planner, schedule, breaker, loader checks, purity import test extended
├── property/                    # core output independent of the store; planner order independence
├── integration/                 # reads, ttl, chains, scheduler, notifications, failures, deadline, lock discipline
└── golden/                      # 16.1 with deferred values, new vectors
```

**Structure Decision**: extend the single library under `ports/py/`. The pure planner and schedule live in `engine/`
where the import test guards them; everything that does IO or reads a clock is in three edge modules (`fetch.py`,
`breaker.py`, `scheduler.py`) plus `hooks.py`, none of which `engine/`, `expr/`, `schema/` or `behaviors/` may import.
`service.py` only composes; the stage's mutable state (breakers, flights) lives in `fetch.py` so the service does not
grow further.

## Complexity Tracking

| Choice | Why Needed | Simpler Alternative Rejected Because |
|---|---|---|
| A pure planner separate from the executor (research R-1) | Statuses, reuse rules and due times are the bulk of the behavior and must be testable without threads, sleeps or a database | One mixed fetch routine would need real time and IO in every test, and could not be proven independent of the core |
| A small own cron parser (research R-8) | Spec asks for "interval or cron" and the package has no runtime dependencies | A third-party cron library adds the only runtime dependency, and for five fields and four operators |
| Concurrent calls within one dependency level (research R-6) | The read has one deadline for all fields (clarified); sequential calls would let early slow fields starve the rest | Sequential fetching makes the deadline a lottery for documents with several independent deferred fields |
| A scheduler lease table (research R-9) | "An overlapping run skips" must hold across processes sharing one database | An in-process lock only protects one process; a second runner would double-call a source |
