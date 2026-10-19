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
