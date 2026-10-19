# Contract: Library API, delta to Feature 001

Python rendering; other ports expose the same operations. The rest of [Feature 001's library-api](../../001-core-ledger-merge-engine/contracts/library-api.md) is unchanged.

## Registration

```python
behaviors.register_macro(name, version, params, expression, *, accepts_missing=False, limits=None) -> Behavior
behaviors.register_hook(name, version, call)          # call(inputs: Mapping[str, Value], timeout: float) -> Value
```

`register_macro` refuses a preset name (`SchemaError`, `RESERVED_MACRO_NAME`), an expression that does not parse, and limits above the platform maximum. `call` is idempotent, receives only the declared dependencies, and must honor `timeout`.

## Reads and writes

`append` is unchanged in shape. Its behavior with computed fields:

- An entry on or beneath a computed path: `RejectedError(FIELD_NOT_WRITABLE)`, nothing recorded (Feature 001).
- A macro fault in the dry run: `RejectedError(COMPUTE_FAULT)` with a reason naming the macro.
- An impure failure under `reject_on_failure`: `RejectedError(IMPURE_COMPUTE_FAILED)`; under `model_error_on_failure` the request is accepted and `model_errors_added` holds `impure:<path>`.
- `model_errors_added` may also hold `computed:<path>` (counts toward the no-new-errors rule).

`get` returns the document with `computed::` values and the errors above. A macro fault raises `ComputeFault` (an `AdukuError`). A read that finds no recorded outcome for an impure field calls the hook, records the outcome, and returns the value or a model error; it is never rejected.

## Admin

```python
db.recompute(doc: str, field: str | None = None, *, caller: Identity) -> RecomputeReport
db.list_effects(doc: str, field: str | None = None, *, limit=None) -> list[EffectInfo]   # metadata only: never a value or an error text
db.prune_effects(doc: str | None = None) -> int
db.explain(doc, path) -> Explanation      # + dependencies, effect_seq; winner "computed" for a computed path
```

`recompute` returns `RecomputeReport(recomputed, skipped, failed, effect_seqs)` (`failed` is `(field, kind)` pairs: a hook that failed again, or `missing_input`; `effect_seqs` the new log records); `NOT_AUTHORIZED` (as `RejectedError`) if a named field is not permitted to the caller, nothing called. `prune_effects` keeps the record in force and at most 5 superseded records per key, none older than 90 days.

## Errors

New codes: `COMPUTE_FAULT`, `IMPURE_COMPUTE_FAILED`. `ComputeFault` for reads. Everything else as before.

## Guarantees

- Computed values never appear in `list_entries`.
- For one ledger, effect log and schema version, `get` and `rebuild` give byte-identical text, with no hook call.
- A hook is never called while the write lock is held.
