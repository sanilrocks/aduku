"""Builders for service-level tests."""

from __future__ import annotations

from aduku.behaviors.registry import BehaviorRegistry
from aduku.model import EntryInput, Identity
from aduku.schema.loader import load_schema
from aduku.schema.registry import SchemaRegistry
from aduku.service import Aduku
from aduku.storage.base import Clock, FakeClock, IdSource, SequentialIds, Storage
from aduku.storage.memory import MemoryStorage
from tests.fixtures import register_fixtures

FIELDS = """
  "/name": {type: string, merge: lww},
  "/age": {type: int, merge: max},
  "/tags": {type: "list<string>", merge: union},
  "/plan": {type: string, merge: lww, authority: [admin]},
  "/pin": {type: string, merge: lww, ops: [set]},
  "/displayName": {type: string, computed: {macro: upper, args: ["/name"]}}
"""

ALICE = Identity("alice", ("user",))
BOB = Identity("bob", ("user",))
JOB = Identity("job", ("backfill",))
ROOT = Identity("root", ("user", "admin"))


def schema_text(
    audit: str = "", version: int = 1, fields: str = FIELDS, lenient: str = "lenient: {allowed: [backfill]},"
) -> str:
    return (
        f'schema::{{id: "t", version: {version}, tiers: [backfill, user, admin], {lenient} {audit}'
        f" fields: {{{fields}}}}}"
    )


def make_service(
    storage: Storage | None = None,
    audit: str = "",
    clock: Clock | None = None,
    schema: str | None = None,
    ids: IdSource | None = None,
    schema_state: bool = True,
) -> Aduku:
    behaviors = BehaviorRegistry.with_builtins()
    register_fixtures(behaviors)
    registry = SchemaRegistry()
    registry.put(load_schema(schema or schema_text(audit), behaviors))
    registry.activate(1)
    return Aduku(
        storage or MemoryStorage(),
        registry,
        clock=clock or FakeClock(step_seconds=1),
        ids=ids or SequentialIds(),
        behaviors=behaviors,
        schema_state=schema_state,
    )


def put(path: str = "/name", value: object = "Alice", tier: str = "user", **kwargs: object) -> EntryInput:
    return EntryInput(tier=tier, path=path, op="set", value=value, **kwargs)  # type: ignore[arg-type]


def delete(path: str = "/name", tier: str = "user") -> EntryInput:
    return EntryInput(tier=tier, path=path, op="delete")
