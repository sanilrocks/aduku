# Contract: Library API, delta to Features 001 to 005

Python rendering; other ports expose the same operations. Everything not mentioned is unchanged.

## Changes between versions (no store)

```python
schema_changes(old: Schema, new: Schema) -> ChangeList          # also db.schema_changes(old_version, new_version)
```

## Operator operations

```python
db.plan_schema(target: int, *, docs: Sequence[str] | None = None, limit: int | None = None) -> DryRunReport
db.activate_schema(version: int, *, operator: str, acknowledge_breaking: bool = False, rebuild: bool = False,
                   expect_generation: int | None = None) -> ActivationReport
db.pin_document(doc: str, version: int, *, operator: str) -> None
db.unpin_document(doc: str, *, operator: str) -> None
db.plan_migration(doc: str, to: int | None = None) -> DocumentChange
db.migrate(docs: Sequence[str] | None = None, *, to: int | None = None, operator: str, batch: int = 100,
           progress: Callable[[MigrationProgress], None] | None = None) -> MigrationReport
db.schema_history(*, limit: int | None = None, doc: str | None = None) -> list[HistoryRecord]
```

- `operator` is required and non-empty (`ValueError` otherwise); it is recorded in the history. The existing `registry.activate`,
  `registry.pin` and `registry.version_for` keep their signatures; they are durable and shared once the registry is bound to a
  service, record the operator as `"unspecified"`, and take no acknowledgement (the acknowledgement is a rule of the operations above).
- `plan_schema` examines every document unless `docs` or `limit` is given; the report says `examined` and `left_out`. Pinned documents
  are left out and counted (an activation would not touch them). It writes nothing, calls no hook and no source.
- `activate_schema` refuses: a breaking target without `acknowledge_breaking` (`BREAKING_CHANGE_NOT_ACKNOWLEDGED`, naming the first
  breaking changes); a target that drops a used tier (`TIER_IN_USE`) or folds differently from expired compacted history
  (`HISTORY_COMPACTED`); an unregistered version (`SCHEMA_INACTIVE`); a stale `expect_generation` (`SCHEMA_STATE_CHANGED`). Activating
  the active version is a no-op that records nothing.
- A lower version than the one it replaces is a rollback (action `rollback`); it changes no entry and needs no acknowledgement (its
  class is recorded). With `rebuild=True` the report also names, in `changed`, the documents whose answer changes (the dry run's list).
- `migrate` over several documents (or all pinned ones) returns per document `moved`, `already_moved`, `not_pinned` or
  `failed(code)`. Asked about one named unpinned document, `plan_migration(doc)` and `migrate((doc,))` raise
  `RejectedError(NOT_PINNED)` (FR-017: the request is refused).

## Reads

`DocumentResult.shadowed` and `Explanation.shadowed_by_schema` as in `data-model.md` (`Explanation.shadowed` keeps its Feature 001 meaning). The reasons: `tier_not_in_schema`, `now_derived`,
`no_longer_conforms`, `cannot_be_merged`. Reading under the version that wrote the entries skips none.

## Binding and immutability

`Aduku(storage, registry)` binds the registry to the store. At bind and at `put`, a version that was served before (first activation or
pin) whose content differs from the remembered one raises `SchemaError` with code `SCHEMA_VERSION_CHANGED` naming up to five fields.
A service built on a registry that holds no version yet is not bound (there is no schema id to share); register the versions first.
If the stored active version is not in the registry, reads and writes raise `RejectedError(SCHEMA_INACTIVE)` naming it.

## Guarantees

- An activation, pin, unpin or migration is one transaction with its history record; a failure leaves both unchanged.
- A request is admitted wholly under the version `version_for` returned when it started.
- A dry run and `plan_schema` write nothing and call nothing; a document read after the activation equals the report.
- Logs record the operations (operator, versions, class, counts), never a value.
