# Contract: Schema state store and tables

The registry is bound to this interface by the service; the implementation lives next to the storage (not in the pure schema package).

```python
class SchemaState(Protocol):
    def generation(self) -> int                                        # one single-row read
    def read(self) -> SchemaStateSnapshot                              # generation, active version, pins, served fingerprints
    def apply(self, change: StateChange, *, expect_generation: int | None,
              check: Callable[[], None] | None = None) -> int   # one transaction; returns the new generation; StateConflict on a stale one
    def history(self, *, limit: int | None = None, doc: str | None = None) -> list[HistoryRecord]
```

`check` runs inside the write transaction before anything is written (the tier and compaction check of an activation or pin), so no entry
can arrive between the check and the write (FR-003); whatever it raises refuses the change and nothing is recorded.

`StateChange` also carries `pins`, more pins set by one change (the bootstrap adopts all of a registry's pins), and, for a migration, its
`to_version` is the target even when the document is unpinned. `StateChange` is one of: activate(version, rollback: bool, class), pin(doc, version), unpin(doc), migrate(doc, to), each with operator,
time and note, and, for a version served for the first time, its fingerprint and parts. `apply` writes the state rows, the
fingerprint rows and the history record together and bumps the generation by one.

## Tx methods (both backends)

```python
schema_generation(schema_id) -> int
schema_state(schema_id) -> (generation, active_version | None, dict[doc, version])
served_versions(schema_id) -> dict[int, (fingerprint, parts)]
apply_schema_change(change: StateChange, expect_generation) -> int      # raises ConstraintError on a stale generation
schema_history(schema_id, limit, doc) -> list[HistoryRecord]
compacted_docs() -> list[str]                                           # documents compacted at least once (activation cost does not grow with documents)
```

`tiers_in_use()` over the whole ledger is a probe of the tier index, not a pass over the entries, for the same reason.

## Rules

- `schema_history` accepts inserts only (trigger); `schema_versions` rows are written once.
- A reader holds the last generation it saw and reloads the state only when it differs.
- A registry's own choice is checked against the ledger inside the bootstrap transaction (`check`), and is ignored altogether when the store
  already holds state, so a process whose files hold a newer version that cannot serve the ledger still starts on the stored one. If two
  processes bootstrap an empty store at once, the one that loses adopts what the other stored.
- The SQLite schema script runs only on a database stamped with an older `PRAGMA user_version` than the code's, so opening a current
  database writes nothing.
- Bootstrap: with no `schema_state` row, binding adopts the registry's own active version and pins in one `apply` with action
  `bootstrap`; with a row, the stored state wins.
