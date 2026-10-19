# Contract: Conformance Vectors, delta to Feature 001

Case format and canonical text are unchanged. Additions:

## Fixture behaviors

| Name | Semantics |
|---|---|
| `acme.label@1` (macro) | params `name`, `city`; expression `name + " (" + city + ")"`; does not accept missing inputs |
| `fx.echo@1` (hook) | returns the string `"tax-" + country` for input `{ "/country": c }` |
| `fx.fail@1` (hook) | always fails |
| `fx.flaky@1` (hook) | fails the first call for an input and succeeds on later calls |

The harness counts the calls of each hook. A case lists the behaviors it needs in `fixtures:`.

## New steps

- `{ hook_calls: "fx.echo@1", expect: { count: 1 } }` — the number of calls made so far.
- `{ recompute: { doc: "d1", field: "/taxId", contributor: {...} }, expect: { recomputed: ["/taxId"] } }` — or `{ rejected: { code: NOT_AUTHORIZED } }`.
- `get` expectations may contain `computed::` values in `document`, and `model_errors` entries such as `{rule: "impure:/taxId", path: "/taxId"}`.
- `load_schema` problem codes added: `UNKNOWN_DEPENDENCY`, `BAD_MACRO_ARGS`, `MACRO_INPUT_UNSUPPORTED`, `IMPURE_DECLARATION_INCOMPLETE`.

## New and changed cases

`worked-example-16-1` (document gains `displayName` and `label`), `computed-fields` (presets, missing inputs under each
setting, cycles refused, `FIELD_NOT_WRITABLE` on pure, impure and child paths), `impure-computes` (a hook called once,
rebuild with zero calls, both failure modes, recompute, `recompute_by`).
