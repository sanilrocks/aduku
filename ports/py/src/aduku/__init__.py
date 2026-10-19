"""aduku-py: ledger of contributions merged into documents by a central annotated schema.

The names a user needs are importable from here (contracts/library-api.md). They load on first use, so importing a pure
module such as `aduku.engine.derive` does not pull in storage.
"""

from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from aduku.behaviors.registry import BehaviorRegistry
    from aduku.errors import AdukuError, RejectedError, SchemaError, StorageError
    from aduku.model import AppendResult, DocumentResult, EntryInput, Explanation, Identity, RebuildReport
    from aduku.schema.loader import load_schema
    from aduku.schema.registry import SchemaRegistry
    from aduku.service import Aduku
    from aduku.storage.base import Clock, IdSource
    from aduku.storage.memory import MemoryStorage
    from aduku.storage.sqlite import SqliteStorage

_EXPORTS = {
    "Aduku": "aduku.service",
    "AdukuError": "aduku.errors",
    "AppendResult": "aduku.model",
    "BehaviorRegistry": "aduku.behaviors.registry",
    "Clock": "aduku.storage.base",
    "DocumentResult": "aduku.model",
    "EntryInput": "aduku.model",
    "Explanation": "aduku.model",
    "IdSource": "aduku.storage.base",
    "Identity": "aduku.model",
    "MemoryStorage": "aduku.storage.memory",
    "RebuildReport": "aduku.model",
    "RejectedError": "aduku.errors",
    "SchemaError": "aduku.errors",
    "SchemaRegistry": "aduku.schema.registry",
    "SqliteStorage": "aduku.storage.sqlite",
    "StorageError": "aduku.errors",
    "load_schema": "aduku.schema.loader",
}

__all__ = [
    "Aduku",
    "AdukuError",
    "AppendResult",
    "BehaviorRegistry",
    "Clock",
    "DocumentResult",
    "EntryInput",
    "Explanation",
    "IdSource",
    "Identity",
    "MemoryStorage",
    "RebuildReport",
    "RejectedError",
    "SchemaError",
    "SchemaRegistry",
    "SqliteStorage",
    "StorageError",
    "load_schema",
]


def __getattr__(name: str) -> Any:
    module = _EXPORTS.get(name)
    if module is None:
        raise AttributeError(f"module 'aduku' has no attribute {name!r}")
    value = getattr(import_module(module), name)
    globals()[name] = value
    return value
