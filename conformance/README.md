# aduku conformance vectors

Language-neutral test cases that every port (`aduku-py`, `aduku-java`, `aduku-rs`, `aduku-js`) must pass with
byte-identical output (docs/SPEC.md sections 1.4 and 16). A change that alters merge results changes the vectors in
the same commit.

- **Case format**: `specs/001-core-ledger-merge-engine/contracts/conformance-vectors.md` (a `case::{ schema, steps }`
  Ion document; steps are `append`, `get`, `explain`, `activate_schema`, `rebuild`).
- **Canonical Ion text**: the normative output form is defined in the same contract, section "Canonical Ion text".
  Expected documents and model errors are compared as that text.
- **Clock**: the harness supplies `tx_time`; it is excluded from comparisons. A pair of cases differing only in clock
  values must give identical results.
- **Fixture behaviors**: cases may name behaviors a port must provide under the same name. Each fixture is a pure
  behavior (a validator or a merge rule):

  | Name | Semantics |
  |---|---|
  | `acme.max_spouses_by_country@1` | Document rule. If `/country` is `"US"` and `/spouses` has more than 1 item, report one violation with rule id `max_spouses_by_country` and path scope `/spouses`. No violation otherwise. |
  | `acme.subtract@1` | Merge rule: `combine(old, new)` is `old - new` (the first value when `old` is absent). Declared NOT associative, so a schema using it is refused. |
  | `acme.longest@1` | Merge rule: the longer string wins; equal lengths go to the greater string. Declared associative, commutative and idempotent. |

- **Files**: `customer.schema.ion` is the schema used by the worked example and the quickstart; `*.ion` cases are
  added as each user story lands (`atomicity`, `retries`, `schema-load-failures`, `defaults-on-delete`, `tombstones`,
  `model-errors-16-2`, `worked-example-16-1`, `explain`, and the clock pair `clock-early` and `clock-late`, which differ only
  in the harness clock and must give identical results).

## Feature 002: computed fields

Fixture behaviors added for computed fields and impure computes. Each port provides them under the same names:

| Name | Kind | Semantics |
|---|---|---|
| `acme.label@1` | custom macro | parameters `name` and `city`; expression `name + " (" + city + ")"`; does not accept missing inputs |
| `fx.echo@1` | hook | returns the string `"tax-" + country` for the input `{ "/country": c }` |
| `fx.fail@1` | hook | always fails |
| `fx.flaky@1` | hook | fails the first call for a given input and succeeds on later calls |

The harness counts the calls of each hook. Steps added to the case format:

- `{ hook_calls: "fx.echo@1", expect: { count: N } }`: the number of calls made so far.
- `{ recompute: { doc, field?, contributor }, expect: { recomputed: [...] } }`, or `expect: { rejected: { code: NOT_AUTHORIZED } }`.

Cases: `computed-fields` (`FIELD_NOT_WRITABLE` on computed paths), `computed-on-missing` (`on_missing` and its model error) and
`impure-computes` (hook outcomes, failure modes, `recompute`, a rebuild with zero calls).

## Feature 003: deferred fields

Fixture sources added for deferred fields. Each port provides them under the same names; the harness counts calls:

| Name | Semantics |
|---|---|
| `fx.score@1` | answers the integer `712` |
| `fx.rate@1` | answers the decimal `0.92` |
| `fx.rate2@1` | answers `0.92` the first time for an input and `0.93` on later calls (to see a refetch) |
| `fx.limit@1` | answers `score * 10` for the input `{ "/creditScore": score }` (a deferred-to-deferred chain) |
| `fx.batch@1` | a batch source (`batch_max` 50): answers `"band-" + country` per item; records the size of each batch |
| `fx.pair@1` | the same as `fx.batch@1` with a batch maximum of 2, for cases that stay short |
| `fx.paced@1` | a batch source with a batch maximum of 1 and a rate limit of one call a second, for cases that meet the limit |
| `fx.down@1` | always fails |
| `fx.flaky@1` | fails the first call for an input and succeeds after (as in feature 002) |
| `fx.slow@1` | does not answer in time; cases that use it give the stage a deadline of milliseconds |

The harness uses one manual clock for wall time and monotonic time. Steps added to the case format:

- A case may carry `options: { fetch_deadline: "100ms", scheduler_budget: "1500ms" }`: the fetch deadline of a read and the time a scheduler run may take.
- `{ clock: { advance: "10m" } }` or `{ clock: { set: "2026-01-01T00:00:00Z" } }`.
- `get` may carry `consistency: fresh` and `include: [pending]`; its expectation may carry `deferred: { "/creditScore": { status: ok, value: 712 } }` (and `waiting_for`, `reason`), and `document` may contain `deferred::` values.
- `{ source_calls: "fx.score@1", expect: { count: N, batches: [..] } }`: calls made so far and batch sizes.
- `{ invalidate: { doc, field, contributor }, expect: { marked: N } }`, or `expect: { rejected: { code: NOT_AUTHORIZED } }`.
- `{ notify: { topic, docs: [..] }, expect: { marked: N } }`.
- `{ scheduler_run: { hook, contributor }, expect: { attempted: N, succeeded: N, calls: N } }`.

Cases: `deferred-policies` (`once`, `per_fetch`, `scheduled`), `deferred-notifications` (`notification`, a field with combined policies, a rate limit and the run's time budget; the case option `scheduler_budget` sets the budget), `deferred-failures` (the three
`on_failure` modes, retries, the circuit breaker, a write never rejected), `deferred-promises` (chains, pending, stale accepted),
and the extended `worked-example-16-1` (`creditScore` and `fxRate`) and `schema-load-failures`.

## Feature 004: validation rules and authority

Cases added: `validation-rules` (each field rule with good and bad values, a lenient write recorded as model errors, two reasons listed
together), `document-rules` (the four document rules, null as no value, incomparable kinds, the no-new-errors rule, a schema version that
adds a rule), `authority` (tier, role, inheritance, override, wildcard, a delete across declared children, several entries, the lenient flag,
a derived path) and the extended `schema-load-failures`.

New `explain` expectations: `authority` (a list of tiers and roles, or `null` for any permitted tier) and `authority_from` (the field
pattern that declared the list, or `null`). A rejected `append` may list `reasons: [{ path, rule }]`; a port also checks that no reason
text contains a value the request submitted. The 16.1 and 16.2 vectors are unchanged.
