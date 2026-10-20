"""Builders for the tests of validation rules and authority (feature 004)."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from aduku.behaviors.registry import BehaviorRegistry
from aduku.errors import RejectedError
from aduku.model import EntryInput, Identity
from aduku.schema.loader import load_schema
from aduku.schema.registry import SchemaRegistry
from aduku.service import Aduku
from aduku.storage.base import FakeClock, SequentialIds, Storage
from tests.fixtures import register_fixtures

TIERS = "tiers: [backfill, user, admin], roles: [finance, ops], lenient: {allowed: [backfill]},"
USER = Identity("alice", ("user",))
ADMIN = Identity("root", ("user", "admin"))
FINANCE = Identity("fin", ("user",), ("finance",))
JOB = Identity("job", ("backfill",))


def schema_of(fields: str, document_validate: str = "", top: str = "") -> str:
    rules = f"document_validate: [{document_validate}]," if document_validate else ""
    return f'schema::{{id: "t", version: 1, {TIERS} {top} {rules} fields: {{{fields}}}}}'


def build(
    storage: Storage,
    fields: str,
    document_validate: str = "",
    *,
    register: Callable[[BehaviorRegistry], None] | None = None,
    top: str = "",
    prefix: str = "r",
) -> Aduku:
    behaviors = BehaviorRegistry.with_builtins()
    register_fixtures(behaviors)
    if register is not None:
        register(behaviors)
    schemas = SchemaRegistry()
    schemas.put(load_schema(schema_of(fields, document_validate, top), behaviors))
    schemas.activate(1)
    return Aduku(storage, schemas, clock=FakeClock(step_seconds=1), ids=SequentialIds(prefix), behaviors=behaviors)


def set_(path: str, value: object, tier: str = "user") -> EntryInput:
    return EntryInput(tier=tier, path=path, op="set", value=value)


def remove(path: str, tier: str = "user") -> EntryInput:
    return EntryInput(tier=tier, path=path, op="delete")


def rejection(service: Aduku, entries: list[EntryInput], who: Identity = USER, **kwargs: Any) -> RejectedError:
    """Submit a request that must be rejected, and return the rejection."""
    try:
        service.append("d1", entries, contributor=who, **kwargs)
    except RejectedError as error:
        return error
    raise AssertionError("the request was accepted")


def never_shows(error: RejectedError, *values: object) -> None:
    """No reason of a rejection repeats a submitted value (FR-008)."""
    text = " ".join(f"{r.message} {r.rule} {r.path}" for r in error.reasons) + str(error)
    for value in values:
        assert str(value) not in text, f"{value!r} appears in {text!r}"
