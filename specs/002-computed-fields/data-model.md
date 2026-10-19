# Data Model: Computed Fields and Non-Writability

Builds on [Feature 001's data model](../001-core-ledger-merge-engine/data-model.md). Only what is new or changed.

## Schema additions

### ComputedSpec (on a field whose `derived` is `computed`, pure form)

| Field | Rule |
|---|---|
| `macro` | name and version, `upper` or `acme.label@1`; the version defaults to 1; must resolve to a preset or a registered macro |
| `args` | ordered list; each is a path (string starting with `/`) or a literal (`lit::<scalar>`); the number and types must fit the macro's signature |
| `type` | the field's declared type; the macro's result must conform (presets: checked at load where the signature fixes the type) |
| `on_missing` | `null` (default), `absent` or `error`; applies only when a macro that does not accept missing inputs would receive one |

A field with `computed` MUST NOT carry `merge`, `default` or `deferred` (Feature 001 check, unchanged).

### ImpureSpec (on a field whose `derived` is `computed`, impure form)

| Field | Rule |
|---|---|
| `hook` | name and version of a registered hook |
| `depends` | non-empty ordered list of paths; no deferred field; no cycle |
| `reason` | non-empty text |
| `timeout` | duration above zero, at most the platform bound (30s) |
| `retry` | `{ max: 0..5, backoff: duration >= 0 }` |
| `deadline` | duration at least `timeout`, at most the platform bound (60s) |
| `recompute_by` | non-empty list of tiers or roles (same vocabulary and checks as `authority`) |
| `failure_mode` | `reject_on_failure` or `model_error_on_failure`; absent means the schema's `impure_failure_mode`, else `reject_on_failure` |
| `on_missing` | as above, used when the hook's value is missing (model_error_on_failure) |

### Behaviors

- **Macro**: `name`, `version`, `kind="macro"`, `params` (names), `expression`, `accepts_missing` (default false), `limits`
  (steps, expression length, depth, value size; each at most the platform maximum). Presets are macros with native code.
- **Hook**: `name`, `version`, `kind="hook"`, `call(inputs, timeout)` where `inputs` maps each declared dependency path to
  its value. Idempotent by contract.

## New runtime entities

### ComputedResult

`path`, `status` (`value` | `null` | `absent` | `error` | `fault`), `value`, `dependencies` (ordered path/value pairs),
`source` (`macro name@version` or `hook name@version`), `effect_seq` (impure only). The document holds `value` and `null`
results annotated `computed::`.

### EffectRecord (effect log row)

Defined in `model.py` (so the pure engine can read it) and re-exported by `storage/base.py`.

| Field | Meaning |
|---|---|
| `effect_seq` | integer, increasing across the whole log; assigned on append |
| `doc`, `field` | what it was computed for |
| `fingerprint` | SHA-256 over the canonical text of the hook name@version and the dependency values in declared order |
| `hook` | `name@version` |
| `outcome` | `value` or `failure` |
| `value` | canonical Ion text of the value when `outcome=value` |
| `error` | failure kind and message (no field values) when `outcome=failure` |
| `attempts` | calls made, at least 1 |
| `recorded_at` | informational time; never used in derivation |

Rules: append-only; the latest record for `(doc, field, fingerprint, hook)` (greatest `effect_seq`) is the outcome in
force; a record belongs to no request and is written in the same transaction as the entries it accompanies, or alone for
a read-time call or a recompute; pruning never removes the record in force.

### EffectInfo

What `list_effects` returns for one record: `effect_seq`, `doc`, `field`, `hook`, `outcome` (`value` or `failure`),
`attempts`, `recorded_at`. It never carries the value or the error text (FR-027).

### ViewRow (changed)

Adds `effects_seq`: the greatest `effect_seq` for the document when the view was derived. A cached view serves a read
only if its watermark, schema version, rules fingerprint and `effects_seq` all match.

### Explanation (changed)

Adds `dependencies` (path/value pairs) and `effect_seq`. For a computed path: `winner="computed"`, `rule` is the
macro or hook as `name@version`, `inputs` and `shadowed` hold only entries shadowed by the field becoming computed.

### RecomputeReport

`recomputed` (fields), `skipped` (fields the caller may not recompute), `failed` (field and kind), and for each recomputed
field the new `effect_seq`.

### Model errors (additions)

| Rule id | Scope | Raised when | Counts toward no-new-errors |
|---|---|---|---|
| `computed:<path>` | `<path>` | a dependency is missing under `on_missing: error` | yes |
| `impure:<path>` | `<path>` | a hook failed under `model_error_on_failure`, or any failure met during a read | no (exempt) |

Attribution follows Feature 001: the request that introduced the error, or the schema version at a version boundary.

## Error codes (additions)

`COMPUTE_FAULT` (a pure macro errored, hit a limit or returned the wrong type), `IMPURE_COMPUTE_FAILED` (a hook ran out of
retries or time under `reject_on_failure`). Existing: `FIELD_NOT_WRITABLE`, `NOT_AUTHORIZED`, `MODEL_ERROR_INTRODUCED`,
`VALIDATION_FAILED`. Schema load codes (additions): `UNKNOWN_DEPENDENCY`, `RESERVED_MACRO_NAME`, `BAD_MACRO_ARGS`,
`MACRO_INPUT_UNSUPPORTED`, `IMPURE_DECLARATION_INCOMPLETE`; existing `DEPENDENCY_CYCLE`, `DEPENDS_ON_DEFERRED`,
`UNKNOWN_BEHAVIOR`, `UNKNOWN_TIER`.

## State transitions

An impure field's outcome for one input: `(none)` → hook called → `value` | `failure`. A changed input starts a new
fingerprint (new key). `recompute` appends a new record for the same key, which supersedes the earlier one. Nothing is
ever updated or deleted except by pruning superseded records.

## Evaluation order inside one derivation

```text
P1 squash → P2 overlay → P3 tombstones → assemble document
  → P4: computed fields in dependency order (pure macros; impure values from the effect view)
  → P6: validators on the document including computed values (V3)
  → P7: projection (computed values keep the computed:: annotation)
```
