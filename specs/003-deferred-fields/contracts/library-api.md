# Contract: Library API, delta to Features 001 and 002

Python rendering; other ports expose the same operations. Everything not mentioned is unchanged.

## Registration

```python
behaviors.register_hook(name, version, call, *, batch_max=None, rate_limit=None, with_context=False) -> Behavior
```

- `call(inputs, timeout) -> value` for a normal source. The same hook may serve impure computes and deferred fields.
- With `batch_max=N`, `call(items, timeout) -> list` where `items` is a list of input mappings and the answer has one element
  per item: a value, or an `Exception` instance for that item. On the read path a batch source is called with one item.
- `rate_limit` is calls per second (`float`, greater than 0). `batch_max` is at least 1.
- With `with_context=True` the call is `call(inputs, timeout, ctx)` where `ctx` is `{doc, field, attempt, deadline}` (SPEC 9.5;
  for a batch source `doc` is the list of documents, aligned with `items`). Feature 002 hooks, registered without it, are
  unchanged and used by impure computes as before.
- An answer of `None` (no value, or null) is a failure of kind `bad_value`; a value stored for a deferred field is never null.
- A source is idempotent, receives only the declared dependencies, and must honor `timeout`.

Service construction gains `fetch_deadline: float = 5.0` (seconds, above 0 and at most 60), `scheduler_budget: float = 900.0`
(seconds a scheduler run may take, including the time it waits for a rate limit), and injectable `monotonic` and `sleep`
functions for the deferred stage (defaults `time.monotonic` and `time.sleep`), next to the existing `clock`.

## Reads

```python
db.get(doc, *, consistency="cached", include=()) -> DocumentResult
```

- `consistency="fresh"` ignores `ttl` for `per_fetch` fields (everything else as `cached`).
- `include` may contain `"pending"`, which fills `waiting_for` and `reason` of each `DeferredStatus`.
- `DocumentResult.deferred: tuple[DeferredStatus, ...]` lists every deferred field (path order). A shown value is in
  `document` as `Annotated(("deferred",), value)`; an `absent` field (policy `absent`, or `on_missing: absent` while
  pending) is left out; a pending field with `on_missing: null` shows `deferred::null`.
- A read may call sources: `once` and `per_fetch` fields that are due, within the one fetch deadline. It never raises
  because of a source, never writes a model error, and never writes the cached view.
- `AppendResult.view` shows stored deferred values and makes no call; `append` never calls a source (FR-019).
- `get` on a document with no entries returns an empty document and calls nothing.

`DeferredStatus(path, status, as_of, error, attempts, waiting_for, reason)` is defined in `data-model.md`.

## Admin

The spec and SPEC.md name these operations `hook.invalidate` and `scheduler.run`; the Python names are `invalidate` and
`scheduler_run`, and the CLI words are `invalidate` and `scheduler run`.

```python
db.invalidate(doc: str | None, field: str, *, caller: Identity) -> int       # records marked out of date
db.scheduler_run(hook: str, *, caller: Identity) -> SchedulerReport          # hook is "name@version"
db.notify(topic: str, docs: Sequence[str] | None = None) -> int              # records marked out of date
db.list_deferred(doc: str) -> list[DeferredInfo]                             # metadata only: field, status, as_of, attempts, invalidated
db.explain(doc, path) -> Explanation                                         # + winner "deferred" for a deferred path
```

- `invalidate`: `NOT_AUTHORIZED` (as `RejectedError`) if the caller holds none of the field's `invalidate_by` tiers or roles;
  `UNKNOWN_PATH` if `field` is not a deferred field of the document's schema (or of the active schema when `doc` is
  `None`). `doc=None` covers every document that holds a record. Nothing is called.
- `scheduler_run`: `NOT_AUTHORIZED` if the caller may invalidate none of the fields the hook serves; otherwise runs only
  those it may. `SchedulerReport(run_id, hook, started, finished, attempted, succeeded, failed, skipped, calls, reason,
  items)`. A live lease held by another run returns `reason="lease held"` with no call.
- `notify`: not caller-gated (the deployment's job runner delivers events); returns the number of records marked. A document
  that does not exist or holds no record is ignored.
- `list_deferred` reads the store only.

## Explanation (extended)

For a deferred path: `winner="deferred"`, `rule` = `name@version` of the source, `dependencies` (path and value pairs used
for the stored value), `as_of`, `status`; `effect_seq` is `None`. It never calls a source.

## Errors

No new codes. `NOT_AUTHORIZED` and `UNKNOWN_PATH` are reused; load problems are listed in `schema-format.md`. A deferred
failure is never an error to the caller of `get` or `append`.

## Guarantees

- For one ledger, effect log and schema version, the document without its deferred values, its model errors and the output
  of `rebuild` are byte-identical for any content of the derived store, and `rebuild` makes no source call.
- A source is called only with its declared dependencies, and only when every deferred field it depends on is `ok` or `stale`.
- A source is never called while the write lock is held; a read's calls end by the fetch deadline plus a small margin.
- Two concurrent requests that need the same (document, field, fingerprint, source) share one call.
