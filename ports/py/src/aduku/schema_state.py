"""The shared schema state over a `Storage` (feature 006, research R-1): the edge that binds a registry to the store."""

from __future__ import annotations

from collections.abc import Callable

from aduku.model import HistoryRecord, StateChange
from aduku.schema.registry import SchemaStateSnapshot, StateConflict
from aduku.storage.base import ConstraintError, Storage

__all__ = ["StorageSchemaState"]


class StorageSchemaState:
    """A `SchemaState` for one schema id: every read is a short read transaction, every change one write transaction."""

    def __init__(self, storage: Storage, schema_id: str) -> None:
        self._storage = storage
        self._id = schema_id

    def generation(self) -> int:
        with self._storage.read() as tx:
            return tx.schema_generation(self._id)

    def read(self) -> SchemaStateSnapshot:
        with self._storage.read() as tx:
            generation, active, pins = tx.schema_state(self._id)
            return SchemaStateSnapshot(generation, active, pins, tx.served_versions(self._id))

    def apply(
        self, change: StateChange, *, expect_generation: int | None, check: Callable[[], None] | None = None
    ) -> int:
        try:
            with self._storage.transaction() as tx:
                if check is not None:  # inside the write transaction: no entry can arrive between this and the write
                    check()
                return tx.apply_schema_change(change, expect_generation)
        except ConstraintError as error:
            raise StateConflict(str(error)) from None

    def history(self, *, limit: int | None = None, doc: str | None = None) -> list[HistoryRecord]:
        with self._storage.read() as tx:
            return tx.schema_history(self._id, limit, doc)
