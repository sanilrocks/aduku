"""The optional, capped, audit-only rejection log (SPEC 4.5, FR-027, FR-028).

Nothing here is ever read by a merge, validation, derivation or document read, and nothing in `engine/` imports it.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from aduku.errors import RejectedError, SchemaError
from aduku.ion_codec import FrozenStruct, or_placeholder, parse, to_canonical
from aduku.model import EntryInput, Identity
from aduku.schema.model import Schema
from aduku.storage.base import AuditBackend, AuditRow, Clock

__all__ = ["AuditRecord", "RejectionAudit"]

SETTINGS = ("enabled", "disabled", "inherit")


@dataclass(frozen=True)
class AuditRecord:
    id: int | None
    doc: str
    contributor: str
    tiers: tuple[str, ...]
    lenient: bool
    attempted: tuple[dict[str, Any], ...]
    reasons: tuple[dict[str, Any], ...]
    schema_version: int | None
    recorded_at: datetime


class RejectionAudit:
    def __init__(self, backend: Callable[[], AuditBackend], clock: Clock) -> None:
        self._backend = backend
        self._clock = clock

    # -- settings ------------------------------------------------------------------------------------------------

    def enabled(self, schema: Schema, doc: str) -> bool:
        override = self._backend().get_override(doc)
        if override == "enabled":
            # an override never switches auditing on without a cap, whatever version of the schema is in force
            settings = schema.rejection_audit
            return settings.max_entries is not None or settings.max_age_seconds is not None
        if override == "disabled":
            return False
        return schema.rejection_audit.enabled

    def set_override(self, schema: Schema, doc: str, setting: str) -> None:
        if setting not in SETTINGS:
            raise ValueError(f"setting must be one of {SETTINGS}")
        settings = schema.rejection_audit
        if setting == "enabled" and settings.max_entries is None and settings.max_age_seconds is None:
            raise SchemaError("rejection auditing cannot be enabled without a retention limit (max_entries or max_age)")
        self._backend().set_override(doc, None if setting == "inherit" else setting)

    # -- writing -------------------------------------------------------------------------------------------------

    def record(
        self,
        schema: Schema,
        doc: str,
        contributor: Identity,
        entries: Sequence[EntryInput],
        lenient: bool,
        rejection: RejectedError,
    ) -> None:
        """Record one rejected request if auditing is on for the document, then prune. Raises on a storage failure."""
        if not self.enabled(schema, doc):
            return
        full = schema.rejection_audit.mode == "full"
        attempted = tuple(_attempt(entry, full) for entry in entries)
        reasons = tuple(
            FrozenStruct(
                {
                    key: value
                    for key, value in {
                        "code": reason.code.value,
                        "message": reason.message,
                        "entry_index": reason.entry_index,
                        "path": reason.path,
                        "rule": reason.rule,
                    }.items()
                    if value is not None
                }
            )
            for reason in rejection.reasons
        )
        backend = self._backend()
        with backend.atomic():  # the record, the pruning and the pruned count stand or fall together
            backend.insert(
                AuditRow(
                    doc=doc,
                    contributor=contributor.id,
                    tiers=tuple(contributor.tiers),
                    lenient=lenient,
                    attempted=to_canonical(attempted),
                    reasons=to_canonical(reasons),
                    schema_version=schema.version,
                    recorded_at=self._clock.now(),
                )
            )
            self.prune(schema, doc)

    # -- reading and pruning ------------------------------------------------------------------------------------

    def list_records(self, doc: str, since: datetime | None = None, limit: int | None = None) -> list[AuditRecord]:
        """Oldest first. `since` keeps records at or after it; `limit` keeps the most recent N."""
        return [_record(row) for row in self._backend().rows(doc, since=since, limit=limit)]

    def docs(self) -> list[str]:
        return self._backend().docs()

    def summary(self, doc: str) -> dict[str, int]:
        backend = self._backend()
        return {"count": backend.count(doc), "pruned": backend.pruned(doc)}

    def prune(self, schema: Schema, doc: str | None = None) -> int:
        """Apply the retention caps (oldest first, either limit); returns how many records were removed."""
        backend = self._backend()
        settings = schema.rejection_audit
        removed = 0
        with backend.atomic():
            for name in [doc] if doc is not None else backend.docs():
                gone = 0
                if settings.max_age_seconds is not None:
                    cutoff = self._clock.now() - timedelta(seconds=settings.max_age_seconds)
                    gone += backend.delete_older_than(name, cutoff)
                if settings.max_entries is not None:
                    gone += backend.delete_oldest(name, settings.max_entries)
                if gone:
                    backend.add_pruned(name, gone)
                removed += gone
        return removed


def _attempt(entry: EntryInput, full: bool) -> FrozenStruct:
    fields: dict[str, Any] = {"tier": entry.tier, "path": entry.path, "op": entry.op}
    if full:
        if entry.op == "set":
            fields["value"] = or_placeholder(entry.value)  # a rejected value may have no Ion form
        if entry.idem_key is not None:
            fields["idem_key"] = entry.idem_key
        if entry.meta is not None:
            fields["meta"] = or_placeholder(entry.meta)
    return FrozenStruct(fields)


def _record(row: AuditRow) -> AuditRecord:
    return AuditRecord(
        id=row.id,
        doc=row.doc,
        contributor=row.contributor,
        tiers=row.tiers,
        lenient=row.lenient,
        attempted=tuple(dict(item) for item in parse(row.attempted)),
        reasons=tuple(dict(item) for item in parse(row.reasons)),
        schema_version=row.schema_version,
        recorded_at=row.recorded_at,
    )
