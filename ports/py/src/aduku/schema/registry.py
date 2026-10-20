"""Versioned, immutable schema registry (Principle IX). One registry holds the versions of one schema id."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, fields, is_dataclass
from pathlib import PurePath
from typing import Any, Protocol

from aduku.behaviors.registry import BehaviorRegistry
from aduku.errors import ErrorCode, RejectedError, SchemaError
from aduku.ion_codec import FrozenStruct, to_canonical
from aduku.model import HistoryRecord, StateChange
from aduku.schema.diff import schema_changes
from aduku.schema.loader import load_schema
from aduku.schema.model import Schema, differing_parts

__all__ = ["SchemaRegistry", "SchemaState", "SchemaStateSnapshot", "StateConflict"]


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


@dataclass(frozen=True)
class SchemaStateSnapshot:
    """The shared state at one generation: the active version, the pins and the fingerprints of the versions served."""

    generation: int
    active: int | None
    pins: Mapping[str, int]
    served: Mapping[int, tuple[str, str]]  # version -> (fingerprint, parts as canonical JSON)


class StateConflict(Exception):
    """`SchemaState.apply` found the stored generation is not the expected one (another process changed the state)."""


class SchemaState(Protocol):
    """Where the active version and the pins live when several processes share a store (contracts/schema-state.md)."""

    def generation(self) -> int:
        """One single-row read: changes whenever the state does."""
        ...

    def read(self) -> SchemaStateSnapshot: ...

    def apply(
        self, change: StateChange, *, expect_generation: int | None, check: Callable[[], None] | None = None
    ) -> int:
        """Write the change and its history record together; raises StateConflict on a stale generation.

        `check` runs inside the same transaction before anything is written, so that what it verifies (that no entry uses a
        tier the version drops) cannot change between the check and the write; whatever it raises refuses the change.
        """
        ...

    def history(self, *, limit: int | None = None, doc: str | None = None) -> list[HistoryRecord]: ...


def _parts_text(schema: Schema) -> str:
    return json.dumps(schema.fingerprint_parts, sort_keys=True, separators=(",", ":"))


class SchemaRegistry:
    def __init__(self) -> None:
        self._versions: dict[int, Schema] = {}
        self._active: int | None = None
        self._pins: dict[str, int] = {}
        self._state: SchemaState | None = None
        self._now: Callable[[], Any] | None = None
        self._seen = 0  # the generation of the shared state this registry last loaded
        self._served: Mapping[int, tuple[str, str]] = {}
        self._usage: Callable[[str | None], set[str]] | None = None
        self._compaction: Callable[[Schema, str | None], str | None] | None = None

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
        self._sync()
        self._check_served(schema)
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

    def watch(
        self,
        tiers_in_use: Callable[[str | None], set[str]],
        compaction_problem: Callable[[Schema, str | None], str | None] | None = None,
    ) -> None:
        """Tell the registry where to find the tiers the ledger uses without checking anything yet (see `guard`)."""
        self._usage = tiers_in_use
        self._compaction = compaction_problem

    def guard(
        self,
        tiers_in_use: Callable[[str | None], set[str]],
        compaction_problem: Callable[[Schema, str | None], str | None] | None = None,
    ) -> None:
        """Tell the registry where to find the tiers the ledger uses (one document, or all with None).

        `compaction_problem` (feature 005) says why a version cannot serve compacted history, or None.

        A service sets this when it is built. Without it nothing is known about the ledger and nothing is checked, so
        the version that is already active, and every pin, are checked now (SchemaError if one drops a used tier).
        """
        self.watch(tiers_in_use, compaction_problem)
        self._sync()
        if self._active is not None and self._active in self._versions:
            self._check_tiers(self._versions[self._active], None)
        for doc, version in self._pins.items():
            if version in self._versions:  # a version this process lacks is refused on use, naming it
                self._check_tiers(self._versions[version], doc)

    def _check_tiers(self, schema: Schema, doc: str | None) -> None:
        if self._usage is None:
            return
        problem = self._compaction(schema, doc) if self._compaction is not None else None
        if problem is not None:
            raise SchemaError(problem)
        missing = sorted(self._usage(doc) - set(schema.tiers))
        if missing:
            where = f"document {doc!r}" if doc is not None else "the ledger"
            raise SchemaError(
                f"TIER_IN_USE: version {schema.version} drops or renames tiers {missing} that {where} uses"
            )

    def bind(self, state: SchemaState, now: Callable[[], Any]) -> None:
        """Share the active version and the pins through `state` (feature 006, research R-1).

        With no stored state the registry's own active version and pins are adopted into it; otherwise the stored state wins.
        A version served before must have the content it was served with (SCHEMA_VERSION_CHANGED).
        """
        own_active, own_pins = self._active, dict(self._pins)
        self._state = state
        self._now = now
        self._seen = -1
        self._sync()
        if self._seen == 0:  # nothing stored yet: this registry's own choices are adopted below
            self._active, self._pins = own_active, own_pins
        for schema in self._versions.values():
            self._check_served(schema)
        if self._seen == 0 and (self._active is not None or self._pins):
            used = sorted({*([self._active] if self._active is not None else []), *self._pins.values()})
            adopted = (self._active, dict(self._pins))

            def check() -> None:  # inside the transaction: the registry's own choice must be one the ledger can serve
                if adopted[0] is not None:
                    self._check_tiers(self._versions[adopted[0]], None)
                for doc, version in adopted[1].items():
                    self._check_tiers(self._versions[version], doc)

            try:
                self._write(
                    StateChange(
                        schema_id=self._schema_id(),
                        action="bootstrap",
                        operator="bootstrap",
                        at=now(),
                        doc=None,
                        from_version=None,
                        to_version=self._active,
                        change_class=None,
                        note="",
                        active=self._active,
                        pin_doc=None,
                        pin_version=None,
                        served=self._unserved(used),
                        pins=tuple(sorted(self._pins.items())),
                    ),
                    pins=dict(self._pins),
                    expect=0,
                    check=check,
                )
            except SchemaError as refusal:
                if "SCHEMA_STATE_CHANGED" not in refusal.codes:
                    raise
                # another process started on the empty store first: what it stored wins
                self._seen = -1
                self._sync()
                for schema in self._versions.values():
                    self._check_served(schema)

    def activate(
        self,
        version: int,
        *,
        operator: str = "unspecified",
        rollback: bool = False,
        change_class: str | None = None,
        note: str = "",
        expect_generation: int | None = None,
    ) -> None:
        """Make a version the active one. Refused if it drops or renames a tier used by any entry (FR-009)."""
        self._sync()
        self._check_tiers(self.get(version), None)
        if self._state is None:
            self._active = version
            return
        if change_class is None and self._active is not None and self._active in self._versions:
            change_class = schema_changes(self._versions[self._active], self._versions[version]).overall
        change = StateChange(
            schema_id=self._schema_id(),
            action="rollback" if rollback else "activate",
            operator=operator,
            at=self._clock(),
            doc=None,
            from_version=self._active,
            to_version=version,
            change_class=change_class,
            note=note,
            active=version,
            pin_doc=None,
            pin_version=None,
            served=self._unserved([version]),
        )
        target = self.get(version)
        self._write(
            change,
            pins=dict(self._pins),
            expect=self._seen if expect_generation is None else expect_generation,
            check=lambda: self._check_tiers(target, None),
        )

    def active(self) -> Schema:
        self._sync()
        return self._active_schema()

    def active_version(self) -> int | None:
        """The number of the active version, even when this process does not hold it (None when none is active)."""
        self._sync()
        return self._active

    def _active_schema(self) -> Schema:
        if self._active is None:
            raise RejectedError(ErrorCode.SCHEMA_INACTIVE, message="no schema version is active")
        if self._active not in self._versions:
            raise RejectedError(
                ErrorCode.SCHEMA_INACTIVE,
                message=f"the active schema version {self._active} is not registered in this process",
            )
        return self._versions[self._active]

    def pin(
        self,
        doc: str,
        version: int | None,
        *,
        operator: str = "unspecified",
        note: str = "",
        action: str | None = None,
        change_class: str | None = None,
        to_version: int | None = None,
    ) -> None:
        """Pin a document to a version, or unpin it with None. Pinning where it already is changes and records nothing.

        `action` names the act in the history (`pin` or `unpin` unless a migration says `migrate`); `to_version` is the version
        the history says the document moved to when that is not the pin itself (a migration to the active version unpins).
        """
        self._sync()
        if self._pins.get(doc) == version:
            return
        if version is not None:
            self._check_tiers(self.get(version), doc)
        if self._state is None:
            if version is None:
                self._pins.pop(doc, None)
            else:
                self._pins[doc] = version
            return
        previous = self._pins.get(doc)
        change = StateChange(
            schema_id=self._schema_id(),
            action=action or ("unpin" if version is None else "pin"),
            operator=operator,
            at=self._clock(),
            doc=doc,
            from_version=previous,
            to_version=to_version if to_version is not None else version,
            change_class=change_class,
            note=note,
            active=None,
            pin_doc=doc,
            pin_version=version,
            served=self._unserved([] if version is None else [version]),
        )
        pins = {key: value for key, value in self._pins.items() if key != doc}
        if version is not None:
            pins[doc] = version
        chosen = self.get(version) if version is not None else None
        self._write(
            change,
            pins=pins,
            expect=self._seen,
            check=(lambda: self._check_tiers(chosen, doc)) if chosen is not None else None,
        )

    def served_fingerprints(self) -> dict[int, str]:
        """The full fingerprint of every version served so far, by number (empty when the registry is not bound)."""
        self._sync()
        return {version: fingerprint for version, (fingerprint, _) in sorted(self._served.items())}

    def pins(self) -> dict[str, int]:
        """The documents pinned to a version and which (a copy)."""
        self._sync()
        return dict(self._pins)

    def version_for(self, doc: str) -> Schema:
        """The schema a document is read and written under: its pinned version, else the active one."""
        self._sync()
        pinned = self._pins.get(doc)
        return self.get(pinned) if pinned is not None else self._active_schema()

    def history(self, *, limit: int | None = None, doc: str | None = None) -> list[HistoryRecord]:
        """The recorded changes of the shared state, oldest first (empty when the registry is not bound)."""
        return [] if self._state is None else self._state.history(limit=limit, doc=doc)

    def generation(self) -> int:
        """The generation of the shared state this registry last loaded (0 when unbound or nothing was ever written)."""
        return max(self._seen, 0)

    @property
    def schema_id(self) -> str | None:
        """The id of the schema this registry holds (None while it holds no version)."""
        return next(iter(self._versions.values())).id if self._versions else None

    def _schema_id(self) -> str:
        return next(iter(self._versions.values())).id if self._versions else ""

    def _clock(self) -> Any:
        assert self._now is not None
        return self._now()

    def _sync(self) -> None:
        """Reload the active version and the pins if the shared state changed since they were last read."""
        state = self._state
        if state is None or state.generation() == self._seen:
            return
        snapshot = state.read()
        self._seen = snapshot.generation
        self._active = snapshot.active
        self._pins = dict(snapshot.pins)
        self._served = snapshot.served

    def _unserved(self, versions: list[int]) -> tuple[tuple[int, str, str], ...]:
        """(version, fingerprint, parts) of the versions that were never served before: their first use records them."""
        return tuple(
            (v, self._versions[v].content_fingerprint, _parts_text(self._versions[v]))
            for v in versions
            if v not in self._served
        )

    def _check_served(self, schema: Schema) -> None:
        recorded = self._served.get(schema.version)
        if recorded is None or recorded[0] == schema.content_fingerprint:
            return
        names = differing_parts(json.loads(recorded[1]), schema.fingerprint_parts)
        raise SchemaError(
            f"SCHEMA_VERSION_CHANGED: version {schema.version} was served with other content; differs in: "
            + (", ".join(names) or "(unknown)")
        )

    def _write(
        self, change: StateChange, *, pins: dict[str, int], expect: int, check: Callable[[], None] | None = None
    ) -> None:
        state = self._state
        assert state is not None
        try:
            generation = state.apply(change, expect_generation=expect, check=check)
        except StateConflict as error:
            self._seen = -1  # reload on the next call
            raise SchemaError(f"SCHEMA_STATE_CHANGED: another process changed the schema state ({error})") from None
        self._seen = generation
        if change.active is not None:
            self._active = change.active
        self._pins = pins
        self._served = {**self._served, **{v: (fp, parts) for v, fp, parts in change.served}}
