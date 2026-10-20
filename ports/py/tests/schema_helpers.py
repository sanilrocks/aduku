"""Helpers for the schema evolution tests (feature 006): several versions, several services on one storage, restarts."""

from __future__ import annotations

from typing import Any

from aduku.behaviors.registry import BehaviorRegistry
from aduku.schema.loader import load_schema
from aduku.schema.model import Schema
from aduku.schema.registry import SchemaRegistry
from aduku.service import Aduku
from aduku.storage.base import FakeClock, SequentialIds, Storage
from tests.fixtures import register_fixtures
from tests.service_helpers import schema_text

__all__ = ["Env", "field_text", "versions_text"]

BASE = '"/name": {type: string, merge: lww}, "/age": {type: int, merge: max}'


def field_text(extra: str = "", base: str = BASE) -> str:
    """The `fields` body: the base fields plus `extra` (a comma-led list of more declarations)."""
    return base + extra


def versions_text(*bodies: str, tiers: str = "backfill, user, admin") -> list[str]:
    """One schema text per body, with versions 1, 2, ... in order."""
    texts = []
    for number, body in enumerate(bodies, start=1):
        text = schema_text(fields=body, version=number)
        texts.append(text.replace("tiers: [backfill, user, admin]", f"tiers: [{tiers}]"))
    return texts


class Env:
    """Schema texts, a storage and the behaviors: build services (each with a registry of its own) as separate processes would."""

    def __init__(self, storage: Storage, *texts: str) -> None:
        self.storage = storage
        self.texts = list(texts)
        self.behaviors = BehaviorRegistry.with_builtins()
        register_fixtures(self.behaviors)
        self.count = 0

    def schemas(self) -> list[Schema]:
        return [load_schema(text, self.behaviors) for text in self.texts]

    def registry(self, active: int | None = None, only: list[int] | None = None) -> SchemaRegistry:
        registry = SchemaRegistry()
        for schema in self.schemas():
            if only is None or schema.version in only:
                registry.put(schema)
        if active is not None:
            registry.activate(active)
        return registry

    def service(self, active: int | None = None, only: list[int] | None = None, **kwargs: Any) -> Aduku:
        """A new service on the same storage, with its own registry (a restart, or another process)."""
        self.count += 1
        return Aduku(
            self.storage,
            self.registry(active, only),
            clock=kwargs.pop("clock", FakeClock(step_seconds=1)),
            ids=kwargs.pop("ids", SequentialIds(f"p{self.count}")),
            behaviors=self.behaviors,
            **kwargs,
        )
