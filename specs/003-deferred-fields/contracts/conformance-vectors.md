# Contract: Conformance Vectors, delta to Features 001 and 002

Case format and canonical text are unchanged. Additions:

## Fixture sources

| Name | Semantics |
|---|---|
| `fx.score@1` | answers `712`; the harness counts calls |
| `fx.rate@1` | answers `0.92` (decimal); counts calls |
| `fx.rate2@1` | answers `0.92` the first time per input and `0.93` after (to see a refetch) |
| `fx.limit@1` | answers `score * 10` for input `{ "/creditScore": score }` (a deferred-to-deferred chain) |
| `fx.batch@1` | `batch_max` 50; answers `"band-" + country` per item; counts calls and the size of each batch |
| `fx.down@1` | always fails |
| `fx.flaky@1` | fails the first call for an input and succeeds after |
| `fx.pair@1` | the same as `fx.batch@1` with a batch maximum of 2, for cases that stay short |
| `fx.slow@1` | does not answer in time; cases that use it give the stage a deadline of milliseconds |

The harness uses a manual clock (both wall time and monotonic time). A case lists what it needs in `fixtures:`.

## New steps

- `{ clock: { advance: "10m" } }` or `{ clock: { set: "2026-01-01T00:00:00Z" } }`.
- `get` may carry `consistency: fresh` and `include: [pending]`; its expectation may carry `deferred: { "/creditScore":
  { status: ok, value: 712 } }` (and `waiting_for`, `reason`), and `document` may contain `deferred::` values.
- `{ source_calls: "fx.score@1", expect: { count: 1, batches: [50, 50, 20] } }` — calls made so far and batch sizes.
- `{ invalidate: { doc, field, contributor } , expect: { marked: 1 } }` or `{ rejected: { code: NOT_AUTHORIZED } }`.
- `{ notify: { topic, docs: [..] }, expect: { marked: 2 } }`.
- `{ scheduler_run: { hook, contributor }, expect: { attempted: 120, succeeded: 120, calls: 3 } }`.
- `load_schema` problem codes added: `DEFERRED_DECLARATION_INCOMPLETE`, `BAD_REFRESH_POLICY`, `BAD_DEFERRED_OPTION`.

## New and changed cases

`worked-example-16-1` (document gains `creditScore: deferred::712` and `fxRate: deferred::0.92`), `deferred-policies`
(`once` keeps and opt-in recompute, `per_fetch` ttl, changed input and `fresh`, `scheduled` batches, rate limit and
idempotence, `notification` mark then run, combined policies), `deferred-failures` (the three `on_failure` modes,
retries, breaker open and trial, write never rejected and no model error, recovery by `invalidate`), `deferred-promises`
(chain order, pending with reasons, stale accepted, upstream error holds back), `schema-load-failures` (extended), and a
`rebuild` step in each showing zero source calls and an unchanged core document.
