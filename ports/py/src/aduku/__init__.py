"""aduku-py: ledger of contributions merged into documents by a central annotated schema.

The names a user needs are importable from here (contracts/library-api.md). They load on first use, so importing a pure
module such as `aduku.engine.derive` does not pull in storage.
"""

from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from aduku.behaviors.registry import ArgSpec, BehaviorRegistry
    from aduku.errors import AdukuError, ComputeFault, RejectedError, SchemaError, StorageError
    from aduku.hooks import HookRunner
    from aduku.model import (
        AppendResult,
        ArchiveReport,
        AsOf,
        AsOfInfo,
        CompactionReport,
        DeferredInfo,
        DeferredStatus,
        DocumentResult,
        EffectInfo,
        EntryInput,
        ExpiryReport,
        Explanation,
        Identity,
        PurgedMark,
        PurgeRecord,
        PurgeReport,
        PurgeScope,
        RebuildReport,
        RecomputeReport,
        SchedulerReport,
    )
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
    "ArgSpec": "aduku.behaviors.registry",
    "BehaviorRegistry": "aduku.behaviors.registry",
    "Clock": "aduku.storage.base",
    "ComputeFault": "aduku.errors",
    "DeferredInfo": "aduku.model",
    "DeferredStatus": "aduku.model",
    "DocumentResult": "aduku.model",
    "EffectInfo": "aduku.model",
    "EntryInput": "aduku.model",
    "Explanation": "aduku.model",
    "HookRunner": "aduku.hooks",
    "IdSource": "aduku.storage.base",
    "Identity": "aduku.model",
    "MemoryStorage": "aduku.storage.memory",
    "RebuildReport": "aduku.model",
    "RecomputeReport": "aduku.model",
    "RejectedError": "aduku.errors",
    "SchedulerReport": "aduku.model",
    "SchemaError": "aduku.errors",
    "SchemaRegistry": "aduku.schema.registry",
    "SqliteStorage": "aduku.storage.sqlite",
    "StorageError": "aduku.errors",
    "ArchiveReport": "aduku.model",
    "AsOf": "aduku.model",
    "AsOfInfo": "aduku.model",
    "CompactionReport": "aduku.model",
    "ExpiryReport": "aduku.model",
    "PurgeRecord": "aduku.model",
    "PurgeReport": "aduku.model",
    "PurgeScope": "aduku.model",
    "PurgedMark": "aduku.model",
    "HistoryCompactedError": "aduku.errors",
    "load_schema": "aduku.schema.loader",
}

__all__ = [
    "Aduku",
    "AdukuError",
    "AppendResult",
    "ArgSpec",
    "BehaviorRegistry",
    "Clock",
    "ComputeFault",
    "DeferredInfo",
    "DeferredStatus",
    "DocumentResult",
    "EffectInfo",
    "EntryInput",
    "Explanation",
    "HookRunner",
    "IdSource",
    "Identity",
    "MemoryStorage",
    "RebuildReport",
    "RecomputeReport",
    "RejectedError",
    "SchedulerReport",
    "SchemaError",
    "SchemaRegistry",
    "SqliteStorage",
    "StorageError",
    "ArchiveReport",
    "AsOf",
    "AsOfInfo",
    "CompactionReport",
    "ExpiryReport",
    "PurgeRecord",
    "PurgeReport",
    "PurgeScope",
    "PurgedMark",
    "HistoryCompactedError",
    "load_schema",
]


def __getattr__(name: str) -> Any:
    module = _EXPORTS.get(name)
    if module is None:
        raise AttributeError(f"module 'aduku' has no attribute {name!r}")
    value = getattr(import_module(module), name)
    globals()[name] = value
    return value
