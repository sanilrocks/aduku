"""Core value types (data-model.md). All frozen."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

from aduku.errors import Reason
from aduku.ion_codec import Value

__all__ = [
    "AppendResult",
    "DocumentResult",
    "EffectInfo",
    "EffectRecord",
    "Entry",
    "EntryInput",
    "Explanation",
    "Identity",
    "ModelError",
    "RebuildReport",
    "RecomputeReport",
    "Violation",
]

Op = Literal["set", "delete"]


@dataclass(frozen=True)
class Entry:
    """One immutable ledger record; only accepted contributions become entries."""

    id: str
    doc: str
    seq: int
    request_id: str
    tier: str
    path: str
    op: Op
    value: Value
    contributor: str
    tx_time: datetime  # informational: never used for ordering or merging
    schema_version: int
    idem_key: str | None = None
    request_key: str | None = None
    request_hash: str | None = None
    lenient: bool = False
    meta: Value = None


@dataclass(frozen=True)
class EntryInput:
    """One entry of a request, before admission."""

    tier: str
    path: str
    op: Op = "set"
    value: Value = None
    idem_key: str | None = None
    meta: Value = None


@dataclass(frozen=True)
class Identity:
    """An authenticated contributor and the tiers it may use (FR-032). Authentication is out of scope."""

    id: str
    tiers: tuple[str, ...] = ()
    roles: tuple[str, ...] = ()


@dataclass(frozen=True)
class ModelError:
    """A recorded violation of a validation rule. Identity is (rule_id, path_scope)."""

    rule_id: str
    path_scope: str
    message: str = ""
    introduced_by_request: str | None = None
    introduced_by_schema_version: int | None = None

    @property
    def identity(self) -> tuple[str, str]:
        return (self.rule_id, self.path_scope)


@dataclass(frozen=True)
class Violation:
    """What a validator reports; becomes a ModelError (or a rejection reason) when the engine attributes it."""

    rule_id: str
    path_scope: str
    message: str = ""


@dataclass(frozen=True)
class DocumentResult:
    document: Value
    model_errors: tuple[ModelError, ...] = ()
    status: str = "ok"
    # Seqs of entries left out because this schema version cannot use them (unknown tier, unmergeable value).
    skipped: tuple[int, ...] = ()
    # Paths whose value was left out because it could not be merged with another tier's or had no place in the document.
    conflicts: tuple[str, ...] = ()


@dataclass(frozen=True)
class AppendResult:
    entries: tuple[tuple[str, int], ...]
    request_id: str
    duplicate: bool = False
    model_errors_added: tuple[ModelError, ...] = ()
    model_errors_resolved: tuple[ModelError, ...] = ()
    view: DocumentResult | None = None
    reasons: tuple[Reason, ...] = field(default=())


@dataclass(frozen=True)
class RebuildReport:
    """What `rebuild` found. `differing` lists documents whose current cached view was not what the ledger gives
    (they are repaired); `refreshed` lists those with no usable cached view (missing, or from another version);
    `failed` lists those that could not be derived (a validator faulted, a schema version is not registered)."""

    checked: tuple[str, ...]
    differing: tuple[str, ...] = ()
    refreshed: tuple[str, ...] = ()
    failed: tuple[tuple[str, str], ...] = ()  # (document, why) for documents that could not be derived at all

    @property
    def identical(self) -> bool:
        return not self.differing and not self.failed


@dataclass(frozen=True)
class Explanation:
    """Why a path has its value (SPEC 12.3).

    `winner` is the entry id that supplied the value, or "default", "computed" or "deferred", or None when nothing
    sets the path. `inputs` are the entries the value depends on (the winner among them), `shadowed` those whose
    contribution makes no difference to it. `rule` is the merge rule as `name@version`.
    """

    path: str
    value: Value
    winner: str | None
    winner_seq: int | None
    rule: str | None
    inputs: tuple[str, ...] = ()
    shadowed: tuple[str, ...] = ()
    model_errors: tuple[ModelError, ...] = ()
    # For a computed path: the (path, value) pairs it was computed from, and the effect-log record for an impure one.
    dependencies: tuple[tuple[str, Value], ...] = ()
    effect_seq: int | None = None


@dataclass(frozen=True)
class EffectRecord:
    """One outcome of an impure compute, as stored in the effect log (data-model.md). Holds no contributions."""

    doc: str
    field: str
    fingerprint: str
    hook: str  # "name@version"
    outcome: str  # "value" or "failure"
    value: str | None  # canonical Ion text, for a value
    error: str | None  # the kind of failure, never a field value
    attempts: int
    recorded_at: datetime  # informational: never used to derive a document
    effect_seq: int | None = None  # assigned on append


@dataclass(frozen=True)
class EffectInfo:
    """What `list_effects` shows of an effect-log record: when and what, never a value or an error text (FR-027)."""

    effect_seq: int
    doc: str
    field: str
    hook: str
    outcome: str  # "value" or "failure"
    attempts: int
    recorded_at: datetime


@dataclass(frozen=True)
class RecomputeReport:
    """What `recompute` did. `skipped` are fields the caller may not recompute (only when no field was named); `failed`
    are (field, kind) for a hook that failed again or an input that is missing; `effect_seqs` the new log records."""

    recomputed: tuple[str, ...]
    skipped: tuple[str, ...] = ()
    failed: tuple[tuple[str, str], ...] = ()
    effect_seqs: tuple[tuple[str, int], ...] = ()
