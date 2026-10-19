"""Versioned, immutable schema registry (Principle IX). One registry holds the versions of one schema id."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import fields, is_dataclass
from pathlib import PurePath
from typing import Any

from aduku.behaviors.registry import BehaviorRegistry
from aduku.errors import ErrorCode, RejectedError, SchemaError
from aduku.ion_codec import FrozenStruct, to_canonical
from aduku.schema.loader import load_schema
from aduku.schema.model import Schema

__all__ = ["SchemaRegistry"]


def _same(a: Any, b: Any) -> bool:
    """Strict equality for schema content: 1, true, 1.0 and "a" versus a differ, as do 1.5 and 1.50."""
    if type(a) is not type(b):
        return False
    if is_dataclass(a) and not isinstance(a, type):
        return all(_same(getattr(a, f.name), getattr(b, f.name)) for f in fields(a))
    if isinstance(a, tuple):
        return len(a) == len(b) and all(_same(x, y) for x, y in zip(a, b, strict=True))
    if isinstance(a, FrozenStruct):
        return a.keys() == b.keys() and all(_same(a[key], b[key]) for key in a)
    try:
        return bool(to_canonical(a) == to_canonical(b))
    except TypeError:
        return bool(a == b)


class SchemaRegistry:
    def __init__(self) -> None:
        self._versions: dict[int, Schema] = {}
        self._active: int | None = None
        self._pins: dict[str, int] = {}
        self._usage: Callable[[str | None], set[str]] | None = None

    def load(self, path_or_text: str | PurePath, behaviors: BehaviorRegistry | None = None) -> Schema:
        """Parse and validate a schema from schema text or a file path, failing closed with every problem found.

        The schema is returned, not registered: `put` it to make it a version of this registry.
        """
        if isinstance(path_or_text, PurePath) or "schema::" not in path_or_text:  # a path, not schema text
            from aduku.files import read_schema_text  # the filesystem is reached outside the pure schema package

            return load_schema(read_schema_text(path_or_text), behaviors)
        return load_schema(path_or_text, behaviors)

    def put(self, schema: Schema) -> None:
        """Register a version. Re-registering identical content is a no-op; changed content is refused."""
        if self._versions and next(iter(self._versions.values())).id != schema.id:
            raise SchemaError(
                f"this registry holds schema {next(iter(self._versions.values())).id!r}, not {schema.id!r}"
            )
        existing = self._versions.get(schema.version)
        if existing is not None and not _same(existing, schema):
            raise SchemaError(
                f"schema {schema.id!r} version {schema.version} is immutable and already registered with different content"
            )
        if existing is None:  # an identical re-registration keeps the object that kept states and caches refer to
            self._versions[schema.version] = schema

    def get(self, version: int) -> Schema:
        try:
            return self._versions[version]
        except KeyError:
            raise RejectedError(
                ErrorCode.SCHEMA_INACTIVE, message=f"schema version {version} is not registered"
            ) from None

    def versions(self) -> tuple[int, ...]:
        return tuple(sorted(self._versions))

    def guard(self, tiers_in_use: Callable[[str | None], set[str]]) -> None:
        """Tell the registry where to find the tiers the ledger uses (one document, or all with None).

        A service sets this when it is built. Without it nothing is known about the ledger and nothing is checked, so
        the version that is already active, and every pin, are checked now (SchemaError if one drops a used tier).
        """
        self._usage = tiers_in_use
        if self._active is not None:
            self._check_tiers(self._versions[self._active], None)
        for doc, version in self._pins.items():
            self._check_tiers(self._versions[version], doc)

    def _check_tiers(self, schema: Schema, doc: str | None) -> None:
        if self._usage is None:
            return
        missing = sorted(self._usage(doc) - set(schema.tiers))
        if missing:
            where = f"document {doc!r}" if doc is not None else "the ledger"
            raise SchemaError(
                f"TIER_IN_USE: version {schema.version} drops or renames tiers {missing} that {where} uses"
            )

    def activate(self, version: int) -> None:
        """Make a version the active one. Refused if it drops or renames a tier used by any entry (FR-009)."""
        self._check_tiers(self.get(version), None)
        self._active = version

    def active(self) -> Schema:
        if self._active is None:
            raise RejectedError(ErrorCode.SCHEMA_INACTIVE, message="no schema version is active")
        return self._versions[self._active]

    def pin(self, doc: str, version: int | None) -> None:
        """Pin a document to a version, or unpin it with None."""
        if version is None:
            self._pins.pop(doc, None)
            return
        self._check_tiers(self.get(version), doc)
        self._pins[doc] = version

    def version_for(self, doc: str) -> Schema:
        """The schema a document is read and written under: its pinned version, else the active one."""
        pinned = self._pins.get(doc)
        return self.get(pinned) if pinned is not None else self.active()
