# Research: Deferred Computed Fields

All unknowns from the Technical Context are resolved here. The spec's clarifications (2026-10-19) are taken as given: a
`per_fetch` value is reused only while its inputs are unchanged and it is younger than `ttl`; `invalidate_by` is required
on every deferred field; the circuit breaker is per source; the fetch deadline covers the whole read; a notification only
marks values out of date.

## R-1. A pure planner and an edge executor

- **Decision**: the stage is two parts. `engine/deferred.py` is a pure function `plan(schema, document, records, now,
  consistency, breaker_open, read_allowed)` returning, per deferred field in dependency order, its **state** (status,
  value to show, reason it waits) and its **action** (`none`, or `fetch` with the reason: `first`, `ttl`, `inputs`,
  `invalidated`, `schedule`). `fetch.py` runs the actions that the caller's mode allows (a read runs `once` and
  `per_fetch` fetches; the scheduler runs `scheduled` and invalidated ones), calls sources, and writes records.
- **Rationale**: nearly all of the behavior (FR-005 to FR-018) is a decision table over (policy, record, inputs, time,
  upstream states). As a pure function it is exhaustively testable with hypothesis and a fake `now`, and it satisfies
  constitution III by construction (the import test lists it). The executor is then only plumbing.
- **Alternatives considered**: one `fetch_field` routine with the decisions inline (needs a real clock and IO in every
  test); a state machine object per field (stateful, harder to prove independent of history).

## R-2. The derived store

- **Decision**: a table `derived_values` with one row per (document, field): the latest result, mutable by design.
  Columns are in `data-model.md`. It is separate from the ledger, the effect log and the cached view. The cached view
  (`ViewRow`) never contains deferred values, so no cache invalidation is needed when the store changes; the overlay is
  applied after the cached P1-P6 view is obtained, on every read.
- **Rationale**: FR-023 and FR-024 require the core output to be identical whatever the store holds; keeping the store out
  of the view makes that structural rather than a thing to remember. The latest record is what SPEC 9.5 describes
  ("derived_value { doc, field, value, as_of, ... }"); history is not required (the effect log serves impure computes).
- **Alternatives considered**: an append-only log with a "latest" view like the effect log (history nobody reads, more
  rows, and a prune policy); storing deferred values inside the view (would make rebuild equality depend on them).

## R-3. The declaration, defaults and platform bounds

- **Decision**: 
  ```
  deferred: { hook: "name@version", depends: ["/path", ...],
              refresh: once | per_fetch | scheduled::{every:"1h"} | scheduled::{cron:"0 * * * *"}
                       | notification::{topic:"t"} | [ ... a list of the above ... ],
              ttl: "5m",                          // per_fetch only
              on_dependency_change: keep|recompute,   // once only; default keep
              on_failure: error|serve_stale|absent,   // default: schema's deferred_on_failure, else error
              on_missing: null|absent,                // default null; what a field shows while its inputs are missing
              timeout: "2s", retry: {max: 2, backoff: "200ms"}, deadline: "6s",
              invalidate_by: [admin] }
  ```
  `refresh` is a symbol (`once`, `per_fetch`) or an annotated struct (`scheduled::{...}`, `notification::{...}`); a
  list combines them. A schema-level `deferred_on_failure` is the document-type default (spec FR-018). Platform bounds:
  `timeout` default 2 s (max 30 s), `retry.max` default 2 (max 5), `backoff` default 200 ms (max 10 s), `deadline` default
  6 s (max 60 s), `ttl` at least 1 s. The fetch deadline of a read is a service setting, default 5 s (max 60 s).
  Circuit breaker: opens after 5 consecutive failed calls (each after its retries) to a source, cool-down 30 s, then one
  trial call. Scheduler: `interval` at least 1 s.
- **Rationale**: it follows the SPEC 9.5 example (`refresh: once, timeout: "2s"`, `ttl: "5m"`, `on_failure: serve_stale`) and
  the impure declaration of Feature 002 (`timeout`, `retry`, `deadline`). `on_missing` has no `error` value here:
  a deferred field never produces a model error (FR-019). Bounds mirror Feature 002's.
- **Alternatives considered**: a separate `policies:` block per refresh policy (verbose for the common single policy);
  `ttl` inside `per_fetch::{ttl}` (departs from the SPEC example).

## R-4. Reuse rules per policy (the heart of the planner)

- **Decision**: a stored record is *current* for a field when its **shape digest** (source name@version, the depends
  list and the field type) equals the schema's. If not, it is not reusable (FR-030) and counts as no record. Given a
  current record and the field's current **input fingerprint** (the Feature 002 fingerprint over source and input values):

  | Policy | Needs a fetch when |
  |---|---|
  | `once` | no record; or `invalidated`; or inputs changed and `on_dependency_change: recompute` |
  | `per_fetch` | no record; or `invalidated`; or inputs changed; or `now - as_of >= ttl`; or `consistency: fresh` |
  | `scheduled` | no record and inputs ready (by the scheduler); or `invalidated`; or the next fire after `as_of` is `<= now` |
  | `notification` | no record and inputs ready (by the scheduler); or `invalidated` |

  A read runs the fetches for `once` and `per_fetch` (and the first fetch of a field whose other policy is only
  `scheduled` or `notification` is left to the scheduler: the read shows `pending` with the reason `not fetched yet`).
  The scheduler runs `scheduled`, `notification` and invalidated ones. With combined policies a field needs a fetch when
  any of its policies says so and the caller's mode runs that policy.
- **Rationale**: this is exactly spec FR-005 to FR-012 and the clarification on `per_fetch`. Keeping the digest separate
  from the input fingerprint is what lets `once` keep its value when only input values change while a changed source or
  dependency list still forces a re-fetch.
- **Alternatives considered**: using the input fingerprint as the only key (a `once` value would be refetched on every
  input change, contradicting 17 item 5); also fetching `scheduled` fields on first read (a read would then call
  a source the field's author said belongs to the batch job).

## R-5. Statuses, promises and "accepted stale"

- **Decision**: per field, in dependency-level order:
  - `pending`: some input has no value (`on_missing` decides null/absent display), or a deferred upstream is `pending`, or
    an upstream is `error` with nothing stale to serve, or the field has never been fetched and its policy does not
    fetch on read. A `pending` field never calls its source. The reason is kept (`waiting for /path`, `inputs missing`,
    `not fetched yet`).
  - `ok`: current record with status ok.
  - `stale`: the last good value is shown because a fetch failed under `serve_stale`. A stale upstream **counts as having a
    value** for dependents, always (FR-013, FR-015), and its stale value is the input; the dependent's fingerprint then
    changes when the upstream recovers.
  - `error`: the last fetch failed after retries (or the breaker was open); under `error` the field is shown as `deferred::null`; under
    `absent` the field is left out of the document with status `error`.
- **Rationale**: the promise rule needs a total order on states: a dependent runs on `ok` or `stale` and waits otherwise.
- **Alternatives considered**: dependents of a stale upstream wait for recovery (the field would be stuck through any
  long outage, defeating `serve_stale`).

## R-6. The read path: one deadline, levels, single-flight

- **Decision**: `get` obtains the cached P1-P6 view as today, then overlays. It computes `deadline_at = now_monotonic +
  fetch_deadline` once. Planner levels (graph depth over deferred fields) are processed in order; within a level the due
  calls run concurrently on the existing per-attempt daemon threads, each given `deadline_at` as an upper bound for its own
  deadline. When the read deadline passes, calls still running are abandoned (as `HookRunner` already does) and their
  fields follow `on_failure` for this read; if a late answer arrives and conforms, it is stored for later reads. Concurrent
  requests needing the same (document, field, fingerprint, source) share one call through the Feature 002 single-flight
  map. A write (`append`) makes no deferred call and its `view` shows stored deferred values only.
- **Rationale**: FR-008 (one deadline), FR-019 (writes unaffected) and FR-029 (single call). Levels keep the order
  deterministic without serializing independent fields.
- **Alternatives considered**: sequential fetches in order (a slow first field starves the rest); a worker pool per
  service (keeps a stuck call's worker, as rejected in Feature 002 R-8).

## R-7. Sources, batches and registration

- **Decision**: a source is registered with `register_hook(name, version, call, *, batch_max=None, rate_limit=None)`.
  A normal source is `call(inputs, timeout) -> value` as in Feature 002 (a hook may serve impure computes and deferred
  fields alike). A batch source (`batch_max=N`) is `call(items, timeout) -> list`, where `items` is a list of input
  structs and the answer has one element per item: a value, or an `Exception` instance for that item. `rate_limit` is
  calls per second (a float). `with_context=True` adds the SPEC 9.5 `ctx` (`doc`, `field`, `attempt`, `deadline`) as a
  third argument; it is opt-in so that Feature 002 hooks keep their two-argument form. A batch source is called with a single item on the read path. An answer of `None` (no value or null) is a failure of
  kind `bad_value`, as in `HookRunner` for impure computes, so a stored value is never null.
- **Rationale**: SPEC 9.5: "Batch hooks MAY declare `batch: { max: N }`"; rate limit is "hook rate limits". Per-item
  exceptions let one bad document fail without failing the batch.
- **Alternatives considered**: batch metadata in the schema (the schema author cannot know what the source accepts);
  a separate registration function for batch sources (two ways to register one kind of thing).

## R-8. Scheduler: due times, cron, order

- **Decision**: `engine/schedule.py` provides `next_fire(spec, after)`. An interval is `after + every`; a cron
  expression is the standard five fields (minute, hour, day of month, month, day of week) with `*`, lists, ranges
  and steps, evaluated in UTC. A scheduled field is due when `next_fire(spec, as_of) <= now`. After a refresh `as_of` is
  the time of the fetch, so a repeated run finds nothing due (idempotence, FR-010). The run orders documents by id and
  fields by path so that runs are deterministic.
- **Rationale**: derived entirely from stored `as_of`, so a run that is interrupted and restarted continues where it
  stopped. UTC avoids time-zone ambiguity.
- **Alternatives considered**: a third-party cron library (the only runtime dependency, see Complexity Tracking); storing
  a `next_due` column (a second source of truth that can disagree with `as_of`).

## R-9. Scheduler run: batching, rate limit, lease, record

- **Decision**: `scheduler_run(hook, caller)` first checks `invalidate_by` for the fields served by that source (only
  those fields are run). It takes a lease row `(source, owner, expires)` valid for the run's maximum duration (default
  15 min, renewed per batch); if a live lease exists it returns a report with `skipped: lease held` and calls nothing.
  Due items are paged from the store, grouped by source, packed into batches of `batch_max`, and called through the
  breaker; between calls it waits on a token bucket built from `rate_limit`; when the bucket would make the run exceed its
  maximum duration, the remaining items stay due. Results are written per batch (so an interrupted run keeps progress)
  and one `scheduler_runs` row is written at the end: source, started, finished, attempted, succeeded, failed, skipped.
- **Rationale**: FR-010 and the edge case on overlapping runs; per-batch writes make "repeat, interrupt and start
  again" safe.
- **Alternatives considered**: an in-process lock only (does not protect two processes on one database); one transaction
  for the whole run (a crash loses all progress and holds a write lock during IO, which the engine forbids).

## R-10. Circuit breaker and rate limiter

- **Decision**: `breaker.py` holds, per source key (`name@version`), `closed | open | half_open`, a consecutive-failure
  counter, and the time it opened. State is in memory per `Aduku` instance and shared by all fields and documents (spec
  clarification). A call is refused without being made while open; after the cool-down exactly one trial call is allowed
  (concurrent callers are refused until it ends); success closes, failure re-opens. Time comes from an injected monotonic
  clock. The rate limiter is a token bucket per source from `rate_limit`, used by the scheduler (which waits for a token) and by
  the read path (which does not wait: a read that finds no token makes no call, and the field follows its failure policy
  for that read with the attempt kind `rate_limited`; the breaker does not count it).
- **Rationale**: FR-017 and SC-008 need a breaker whose threshold and cool-down are testable with a fake clock. A
  refused call is treated as a failed attempt for the field's policy but does not count toward the threshold again.
- **Alternatives considered**: persisting breaker state in the database (shared across processes, but a write on every
  failure and a stale-state problem after a crash; recorded as a possible later addition).

## R-11. Notifications and `invalidate`

- **Decision**: `notify(topic, docs=None)` sets `invalidated = 1` on the derived records of the fields linked to the topic
  (for the named documents, or for all documents that hold a record), in one short write transaction, and makes no call;
  repeating it changes nothing further. `invalidate(doc|None, field, caller)` does the same for one field after the
  `invalidate_by` check; the field must be a deferred field of the document's schema or the call raises `UNKNOWN_PATH`.
  The next fetch of that kind (a read for `once`/`per_fetch`, the scheduler for the rest) recomputes and clears the flag
  when a call succeeds; a failed call keeps the flag set.
- **Rationale**: clarification 5 and FR-020. A boolean on the record is idempotent and survives a crash.
- **Alternatives considered**: deleting the record on invalidate (loses the last good value that `serve_stale` needs).

## R-12. Load-time checks and codes

- **Decision**: the loader adds, with stable codes and the field named: `DEFERRED_DECLARATION_INCOMPLETE` (no `hook`,
  `depends`, `refresh` or `invalidate_by`), `BAD_REFRESH_POLICY` (unknown policy or malformed option),
  `BAD_DEFERRED_OPTION` (an option that does not belong to the policy, a value out of the platform bounds, a bad
  duration), `UNKNOWN_BEHAVIOR` (the source is not registered as a hook), `UNKNOWN_DEPENDENCY` and
  `DEPENDENCY_CYCLE` (existing codes, now also for deferred nodes), plus the existing `DEPENDS_ON_DEFERRED` and
  `COMPUTED_AND_DEFERRED`. A deferred field with `merge`, `default` or `validate` keeps the existing codes.
- **Rationale**: FR-021; codes are shared with other ports through `conformance/schema-load-failures.ion`.
- **Alternatives considered**: one generic `BAD_DEFERRED` code (hard to test per cause).

## R-13. Existing fixtures that change

- **Decision**: `invalidate_by` is required, so every `deferred:` declaration in `conformance/` and `ports/py/tests/`
  (found: `conformance/schema-load-failures.ion`, `tests/unit/test_schema_load_failures.py`) gains `invalidate_by` and
  the fixture `refresh`; the load-failure vector gains a case for the missing option. This is done in the same change
  (constitution workflow). The 16.1 vector is extended with the two deferred values and its fixture sources.
- **Rationale**: a change to loader acceptance must update vectors in the same change.
- **Alternatives considered**: making `invalidate_by` optional with an empty default (contradicts the clarified FR-025).

## R-14. Conformance: clock and fetch steps

- **Decision**: vectors gain a `clock` step (`set` or `advance` by a duration) and the harness uses a manual clock for both
  `Clock` and the breaker's monotonic time; `get` steps gain `consistency`, `include` and an expected `deferred` map
  (status, value); new steps `invalidate`, `notify` and `scheduler_run`, with expected `source_calls` (counts per source,
  and batch sizes) as `hook_calls` does for impure computes. Fixture sources: `fx.score@1` (answers 712, counts),
  `fx.rate@1` (answers `0.92`), `fx.batch@1` (batch_max 50), `fx.down@1` (always fails), `fx.slow@1` (sleeps past the
  deadline; the harness advances the manual clock instead of sleeping), `fx.flaky@1`.
- **Rationale**: SC-003 to SC-008 are about time; a manual clock makes them deterministic across ports.
- **Alternatives considered**: real sleeps in vectors (slow and flaky).

## R-15. Logging

- **Decision**: structured events `deferred_call` (document, field, source, attempts, result, never value or error text),
  `breaker` (source, state change), `scheduler_run` (source, counts, duration), `notification` (topic, count of
  documents marked), `invalidate` (caller, document, field). Error kinds are the Feature 002 vocabulary (`error`,
  `timeout`, `deadline`, `bad_value`) plus `breaker_open` and `rate_limited`.
- **Rationale**: FR-028, SPEC 13 item 3.
- **Alternatives considered**: none worth recording.

## R-16. Explain, append views and the CLI

- **Decision**: `explain` on a deferred path returns winner `"deferred"`, the source with version, the dependency values
  used, `as_of`, status, and `effect_seq` unused; it reads the store and never calls. `AppendResult.view` and `get` with
  `consistency: cached` and nothing due show stored values with no calls. The CLI gains `get --fresh --include pending`,
  `invalidate`, `scheduler run`, `notify` and `deferred list`; sources come from `--behaviors`/`ADUKU_BEHAVIORS` as in
  Feature 002.
- **Rationale**: FR-023, FR-026, FR-027.
- **Alternatives considered**: `explain` triggering a fetch (surprising IO from a diagnostic call).
