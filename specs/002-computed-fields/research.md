# Research: Computed Fields and Non-Writability

All unknowns from the Technical Context are resolved here. Spec clarifications (2026-10-19) are taken as given:
macros declare whether they accept missing inputs; the `on_missing: error` model error counts against the no-new-errors
rule; a pure macro fault is `COMPUTE_FAULT`; `recompute_by` is declared per impure field; macro limits have platform
defaults and a maximum that a registration may only lower.

## R-1. How custom macros are written and sandboxed

- **Decision**: custom macros are expressions in a small language whose syntax and meaning are a subset of CEL (Common
  Expression Language), implemented in this package (`expr/`), specified in `contracts/expression-language.md`. A custom
  macro is registered as `register_macro(name, version, params, expression, accepts_missing=False, limits=None)`. The
  only variables in scope are the macro's parameters (the declared dependencies, bound by position). The function
  library is the preset macros, so there is nothing that touches the clock, randomness, the network or the filesystem to
  call.
- **Spike (2026-10-19)**: `cel-python` (`celpy`) evaluated a standard expression correctly but has no `upperAscii` or other
  string-extension functions in its base environment, offers no evaluation cost limit, and cannot interrupt a running
  evaluation; adding functions would mean extending a third-party interpreter whose behavior must then match the CEL
  libraries of the other ports byte for byte.
- **Rationale**: the guarantee wanted (SC-006) is "cannot reach anything outside its inputs, and is stopped when over
  budget". An evaluator we own gives both by construction, deterministically. Keeping the syntax a CEL subset means a port
  may later use its native CEL library for the same expressions, and vectors written now stay valid.
- **Alternatives considered**: (a) CEL via `celpy`, rejected as above; (b) WebAssembly modules (`wasmtime`), rejected for
  Feature 002: a large runtime and a build toolchain for macro authors, to be reconsidered for module-style macros;
  (c) registered Python callables, rejected: Python cannot take away `open`, `socket` or `time` from in-process code, so SC-006
  could not hold; (d) Python `ast`-based whitelist, rejected: the syntax would be Python-specific and not portable.

## R-2. Resource limits are deterministic

- **Decision**: "time" is a step budget (fuel): every node evaluated and every element a built-in processes costs one
  step. "Memory" is a size budget: the total size of values the evaluation builds, and the size of its result. Platform
  defaults and maximum (Limits): steps 10,000 (max 100,000); expression length 2,000 characters (max 10,000); nesting
  depth 32 (max 64); produced-value size 64 KiB (max 1 MiB); input size 256 KiB. A registration may set lower values for
  its macro and is refused for higher ones than the maximum. Exceeding any limit is a `COMPUTE_FAULT` naming the macro and
  the limit.
- **Rationale**: a wall-clock limit would make the same ledger fault on one machine and not on another, which breaks
  constitution III; a step budget gives the same answer everywhere and needs no timer or thread. CEL has no loops, so the
  budget bounds every expression.
- **Alternatives considered**: wall-clock timeouts (non-deterministic), process isolation with OS limits
  (heavy, platform-specific, still non-deterministic).

## R-3. Arguments of a computed declaration: paths and literals

- **Decision**: `computed: { macro: "upper", args: ["/name"] }`. An argument is either a path, written as a plain string
  starting with `/`, or a literal written with the annotation `lit::` (for example `lit::" ("`, `lit::2`). `macro` is a
  name with an optional `@version` (default `@1`). The impure form is `computed: { impure: { hook, depends, ... } }` as in
  SPEC 9.3.1.
- **Rationale**: presets such as `concat`, `round`, `format_ts`, `join` and `if` take literals; without a marker a literal
  string `"/"` would be ambiguous. The annotation reads naturally in Ion and costs nothing to parse.
- **Alternatives considered**: all-strings with an escape (ambiguous), a separate `params:` map (more verbose).

## R-4. Presets: signatures, types and missing inputs

- **Decision**: presets are native pure functions in `behaviors/compute.py`, each with a signature used at load for
  arity and type checks and a flag `accepts_missing`. `upper lower trim` (string to string), `concat` (strings or
  scalars to string), `coalesce` (any, first non-null; accepts missing), `len` (string, list or struct to int), `sum`
  (numbers or a list of numbers to a number), `min max` (numbers or strings; a list or several arguments), `round` (number,
  literal digits), `format_ts` (timestamp, literal pattern to string), `hash` (any to a hex SHA-256 of its canonical text), `join` (list,
  literal separator to string; accepts missing as an empty list), `if` (bool, any, any; accepts missing in the condition
  as false), `eq` (any, any to bool; accepts missing). Only `coalesce`, `if`, `eq` and `join` accept missing inputs (spec
  clarification). Names are reserved.
- **Rationale**: natively implemented presets are fastest, need no budget, and are specified by their signature in the
  contract so other ports match them; the expression language reuses the same functions.
- **Alternatives considered**: implementing presets as expressions (slower, and `hash` and `format_ts` need native code).

## R-5. Dependency graph and evaluation order

- **Decision**: build the graph at load from each computed field's path arguments (impure: `depends`). A path that lies
  under another derived field depends on that field. A dependency on a path no field declares is refused
  (`UNKNOWN_DEPENDENCY`); cycles are refused (`DEPENDENCY_CYCLE`, already in Feature 001) and reported with the loop;
  a dependency on a deferred field is refused (`DEPENDS_ON_DEFERRED`, already present). Evaluation order is a
  topological order with ties broken by field path in code-point order, so it does not depend on schema file order.
  Dependencies on struct children use the assembled document.
- **Rationale**: a fixed total order makes results and tests reproducible; reusing Feature 001's cycle and deferred checks
  avoids two implementations.
- **Alternatives considered**: lazy memoized evaluation (order still needs the graph, and error attribution is harder).

## R-6. Missing inputs and the three `on_missing` outcomes

- **Decision**: an input is missing when the path has no value in the merged document (never set and no default, deleted
  without a default fallback, or itself a missing computed field) or its value is null: null means no value, for a
  document value and for the null result of another computed field alike. For a macro that does not accept missing inputs the
  macro is not run and `on_missing` decides: `null` puts an explicit null at the path (annotated `computed::null`),
  `absent` leaves the path out, `error` leaves it out and adds a model error with rule `computed:<path>` and scope
  `<path>`. A macro that accepts missing inputs always runs; its result is the value, and a null result is the field's value
  (shown as `computed::null`) that downstream macros see as a missing input.
- **Rationale**: matches the clarified spec and gives every combination one defined result.
- **Alternatives considered**: running every macro with null for missing (breaks the contract of `upper`, `sum`).

## R-7. Where P4 sits in admission and derivation

- **Decision**: `engine/model_errors.py::_evaluate` becomes P1-P3 (existing), then P4 (`engine/compute.py`) over the
  built document with the effect view, then P6. The dry run of a request (`extend`) therefore sees computed values and
  their model errors, and validators may depend on computed values (V3). `on_missing: error` and impure-failure model
  errors enter `update_errors` like validator violations and are attributed the same way, including at schema-version
  boundaries. A `COMPUTE_FAULT` aborts the dry run (like a validator fault in Feature 001): admission rejects with
  `COMPUTE_FAULT`, a read raises `ComputeFault`. The no-new-errors rule exempts only errors with rule `impure:<path>`,
  and only those arise from `model_error_on_failure`, because a `reject_on_failure` failure rejects the request first.
- **Rationale**: one place computes the document and its errors; no second derivation path.
- **Alternatives considered**: a separate P4 pass after admission (would let a request be accepted on a document the
  validators never saw).

## R-8. Calling impure hooks without holding the write lock

- **Decision**: `service.append` becomes: (1) outside any transaction, derive the document the request would produce at
  the observed watermark, find the impure fields whose fingerprint has no recorded outcome (`engine/effects.py`), and call
  them through `HookRunner` (`hooks.py`), collecting outcomes in memory; (2) in the write transaction, re-check the
  watermark; if the document changed, leave the transaction and repeat step 1 (outcomes whose fingerprint is unchanged are
  reused, so no repeat call); otherwise dry-run with the collected outcomes, validate, and append the entries and the
  outcomes together. The existing `_Cold` retry loop is the template. `HookRunner` runs each attempt on its
  own daemon thread and waits up to the per-attempt timeout (the hook is passed its timeout and must honor it; a thread
  that outlives its timeout is abandoned, which is safe because hooks are idempotent and a daemon thread cannot hold the
  process open), retries up to `retry.max` with the fixed `backoff` between attempts, and stops at `deadline`. A read that
  finds no outcome does the same, then records the outcome in a short write transaction (the only case in which a read
  writes). Callers that need the same outcome at the same moment share one call (single-flight per document, field,
  fingerprint and hook), and an outcome is stored once: whoever stores second finds it already there. `recompute` is an
  explicit new call and never shares one. A request rejected for another reason after its hook was called (for example on
  `expected_seq`) has called the hook; its outcome is discarded.
- **Rationale**: satisfies FR-021 and SC-010 with the structure Feature 001 already has; outcomes of a rejected request
  are simply never written.
- **Alternatives considered**: calling hooks inside the transaction (blocks all writers), a second process for hooks
  (out of proportion).

## R-9. The effect log

- **Decision**: a new append-only table `effects(effect_seq PRIMARY KEY AUTOINCREMENT, doc, field, fingerprint, hook,
  outcome, value, error, attempts, recorded_at)`, with triggers forbidding update and delete except through
  `prune_effects`. The fingerprint is SHA-256 over the canonical text of the hook name, version and the dependency values in
  declared order. `outcome` is `value` or `failure`; `value` holds canonical Ion text; `error` holds the failure kind and
  message with no field values. The latest outcome for `(doc, field, fingerprint, hook)` is the one with the greatest
  `effect_seq`. Pruning keeps the latest per key and at most 5 superseded per key, none older than 90 days, and never
  changes a derivation. Listing returns metadata only (no value, no error text), so the log cannot be a side door to
  document values. The cached view row stores `effects_seq` (the greatest sequence seen for the document) so a new
  outcome invalidates the cache like a new entry.
- **Rationale**: the same pattern as `entries`; deriving from `(ledger, effect log, schema version)` stays pure because
  lookups are by value, not by time.
- **Alternatives considered**: storing outcomes inside entries (would put derived data in the ledger), per-document files
  (no transactions).

## R-10. `recompute` and who may call it

- **Decision**: `recompute(doc, field=None, caller)` takes the caller's `Identity`. A field may be recomputed when the
  caller's tiers or roles intersect the field's `recompute_by` list (same vocabulary and unknown-name check as
  `authority`); otherwise `NOT_AUTHORIZED` with no call. Without a field it covers only permitted fields and returns which
  were recomputed and which skipped. It calls the hook with the current inputs, records a new outcome (a greater
  `effect_seq`), and returns the new status.
- **Rationale**: the clarified spec; reuses `Identity` from Feature 001.
- **Alternatives considered**: a deployment-level role (rejected by the clarification).

## R-11. Explain for computed paths

- **Decision**: `Explanation` gains `dependencies` (path and value pairs, in declared order) and `effect_seq`; for a
  computed path `winner` is `"computed"`, `rule` is `macro@version` or `hook@version`, `inputs` and `shadowed` are empty,
  and `shadowed` entries from before the field became computed are listed in `shadowed` as in Feature 001.
- **Rationale**: SPEC 12.3 already names this shape.
- **Alternatives considered**: a second operation (one request per field is the stated success criterion, SC-011).

## R-12. Caching and memoization

- **Decision**: no memoization of macro results in this feature: P4 is recomputed on every derivation, in time
  proportional to the number of computed fields, and the whole view is cached as before. The cache key gains
  `effects_seq`.
- **Rationale**: SC-007 is met without it; memoization keyed by fingerprint can be added later without changing results.
- **Alternatives considered**: per-field memo tables (extra invalidation rules with no measured need).

## R-13. Migrating Feature 001 tests and vectors

- **Decision**: `customer.schema.ion` gains `/label` (custom macro `acme.label@1`). The expected document of
  `worked-example-16-1.ion`, the quickstart output, the CLI tests and any test that asserts a document read under a
  schema with `displayName` change to include the computed values; the golden test for 16.1 is written first (red), then
  P4 makes it pass. `explain` on a derived path now returns the full answer instead of the placeholder.
- **Rationale**: Feature 001 deliberately left computed paths absent from documents; this feature is the intended change.
- **Alternatives considered**: a flag to hide computed values (would keep two behaviors).

## R-14. Observability

- **Decision**: structured events through `log.emit`: `hook_call` (doc, field, hook version, attempts, result kind) and
  `recompute` (doc, field, caller id, result), with no values; faults log the macro name and the limit hit.
- **Rationale**: FR-029; same logger as Feature 001.
- **Alternatives considered**: metrics (no consumer yet).

## R-15. Property-testing strategy

- **Decision**: generate random schemas of computed fields over a fixed set of inputs: (a) shuffling declaration order
  never changes the document; (b) deriving from scratch equals deriving through the cache and equals a rebuild; (c) every
  preset is deterministic and total on its declared input types; (d) a computed value changes only when one of its
  dependencies changes. Use the 1,000-example CI profile.
- **Rationale**: SC-003 and SC-004.
- **Alternatives considered**: example tests only.

## R-16. Fixture behaviors for shared vectors

- **Decision**: ports provide the custom macro `acme.label@1` (parameters `name` and `city`, expression
  `name + " (" + city + ")"`, so a port needs no code for it) and three hooks: `fx.echo@1` (returns `"tax-" + country`),
  `fx.fail@1` (always fails) and `fx.flaky@1` (fails once per input, then succeeds). The harness exposes each hook's call
  count to a `hook_calls` step. A hook that is called is never called twice for the same input within one case unless the
  case asks for a recompute.
- **Rationale**: vectors for the impure behavior need a hook whose calls can be counted without a network.
- **Alternatives considered**: mocking per port (not shared).
