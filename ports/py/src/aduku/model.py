"""Core value types (data-model.md). All frozen."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

from aduku.errors import Reason
from aduku.ion_codec import Value

__all__ = [
    "ActivationReport",
    "AppendResult",
    "ArchiveReport",
    "AsOf",
    "AsOfInfo",
    "Change",
    "ChangeClass",
    "ChangeList",
    "CompactionPlan",
    "CompactionRecord",
    "CompactionReport",
    "SourceRow",
    "ExpiryReport",
    "PurgeApplication",
    "PurgeRecord",
    "PurgeReport",
    "PurgeScope",
    "PurgedMark",
    "StoredOp",
    "DeferredInfo",
    "DeferredStatus",
    "DerivedRecord",
    "DocumentChange",
    "DocumentResult",
    "DryRunReport",
    "EffectInfo",
    "EffectRecord",
    "Entry",
    "EntryInput",
    "Explanation",
    "HistoryRecord",
    "Identity",
    "MigrationProgress",
    "MigrationReport",
    "ModelError",
    "RebuildReport",
    "RecomputeReport",
    "SchedulerReport",
    "SchedulerRun",
    "Shadowed",
    "StateChange",
    "Violation",
]

Op = Literal["set", "delete"]
StoredOp = Literal["set", "delete", "layer"]  # `layer` is only ever written by compaction (feature 005)


@dataclass(frozen=True)
class Entry:
    """One immutable ledger record; only accepted contributions become entries."""

    id: str
    doc: str
    seq: int
    request_id: str
    tier: str
    path: str
    op: StoredOp
    value: Value
    contributor: str
    tx_time: datetime  # informational: never used for ordering or merging
    schema_version: int
    idem_key: str | None = None
    request_key: str | None = None
    request_hash: str | None = None
    lenient: bool = False
    meta: Value = None
    # Feature 005: when the fact became true in the world (read only by `as_of` valid-time reads); the compaction id of a
    # synthetic entry; True for a placeholder whose value, meta, valid_time and request hash were erased by a purge.
    valid_time: datetime | None = None
    synthetic: str | None = None
    purged: bool = False


@dataclass(frozen=True)
class EntryInput:
    """One entry of a request, before admission."""

    tier: str
    path: str
    op: Op = "set"
    value: Value = None
    idem_key: str | None = None
    meta: Value = None
    valid_time: datetime | None = None  # feature 005: a timezone-aware moment; never affects ordering or a normal read


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
    # The status of every deferred field (feature 003), in path order; the stored values are in `document`.
    deferred: tuple[DeferredStatus, ...] = ()
    # Feature 005: set only by an `as_of` read.
    as_of: AsOfInfo | None = None
    # Feature 006: why each skipped entry is left out (worked out when read, never stored); same seqs as `skipped`.
    shadowed: tuple[Shadowed, ...] = ()


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
    # Feature 005: saved per-tier layers, as (document, tier). `layers_differing` were current but not what the ledger gives
    # (repaired); `layers_refreshed` were missing or made under another version (derived afresh and saved).
    layers_differing: tuple[tuple[str, str], ...] = ()
    layers_refreshed: tuple[tuple[str, str], ...] = ()

    @property
    def identical(self) -> bool:
        return not self.differing and not self.failed and not self.layers_differing


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
    # For a deferred path (feature 003): its status in the derived store and when the value shown is from.
    status: str | None = None
    as_of: datetime | None = None
    # Who may write the path (tiers and roles; None: any permitted tier) and the field pattern that declared the list.
    authority: tuple[str, ...] | None = None
    authority_from: str | None = None
    # Feature 005: purged entries that touched the path; whether the originals behind a synthetic entry are still kept
    # (None when no synthetic entry took part); the source ids a synthetic winner or input stands for.
    purged: tuple[PurgedMark, ...] = ()
    originals_kept: bool | None = None
    sources: tuple[str, ...] = ()
    # Feature 006: entries this version leaves out that touch the path, with their reasons. (`shadowed` above keeps its
    # Feature 001 meaning: entries whose contribution makes no difference to the value.)
    shadowed_by_schema: tuple[Shadowed, ...] = ()


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


DeferredState = Literal["ok", "stale", "pending", "error", "unavailable"]  # unavailable: an `as_of` read (feature 005)


@dataclass(frozen=True)
class DerivedRecord:
    """The latest result of a deferred field for a document, as kept in the derived store (data-model.md).

    Mutable by design (one row per document and field, replaced by the next fetch) and never part of rebuild equality.
    `status` is what the last fetch left (`ok`, `stale` or `error`); `pending` is worked out on every read.
    """

    doc: str
    field: str
    value: str | None  # canonical Ion text of the last good value
    has_value: bool
    as_of: datetime | None
    input_fingerprint: str
    hook: str  # name@version
    shape: str  # digest of hook, depends and type
    schema_version: int
    status: Literal["ok", "stale", "error"]
    error: str | None = None  # kind of the last failure; never a message or a value
    attempts: int = 1
    last_attempt: datetime | None = None
    invalidated: bool = False


@dataclass(frozen=True)
class DeferredStatus:
    """What a read says about one deferred field."""

    path: str
    status: DeferredState
    as_of: datetime | None = None
    error: str | None = None
    attempts: int | None = None
    waiting_for: tuple[str, ...] = ()  # filled with include=("pending",)
    reason: str | None = None  # inputs_missing, upstream, not_fetched (with include=("pending",))


@dataclass(frozen=True)
class DeferredInfo:
    """Metadata of a stored deferred result (never the value)."""

    field: str
    status: str
    as_of: datetime | None
    attempts: int
    invalidated: bool


@dataclass(frozen=True)
class SchedulerRun:
    """The record of one scheduler run."""

    run_id: str
    hook: str
    started: datetime
    finished: datetime
    attempted: int = 0
    succeeded: int = 0
    failed: int = 0
    skipped: int = 0
    calls: int = 0
    reason: str | None = None  # why the run stopped early: lease held, breaker open, time limit


@dataclass(frozen=True)
class SchedulerReport:
    """The answer to `scheduler_run`: the run record and, per (document, field), what happened."""

    run_id: str
    hook: str
    started: datetime
    finished: datetime
    attempted: int = 0
    succeeded: int = 0
    failed: int = 0
    skipped: int = 0
    calls: int = 0
    reason: str | None = None
    items: tuple[tuple[str, str, str], ...] = ()  # (doc, field, ok|failed|skipped)


# -- feature 005: time travel, compaction and purge ------------------------------------------------------------------


@dataclass(frozen=True)
class AsOf:
    """A point in a document's history: exactly one of a seq, a recording time or a moment in the world."""

    seq: int | None = None
    tx_time: datetime | None = None
    valid_time: datetime | None = None


@dataclass(frozen=True)
class AsOfInfo:
    """What an `as_of` read stood at: `resolved_seq` (for a valid-time read, the highest seq that took part), how many
    entries took part, and whether the seq falls between two entries of one request."""

    kind: Literal["seq", "tx_time", "valid_time"]
    point: object
    resolved_seq: int
    included: int
    inside_request: bool = False


@dataclass(frozen=True)
class PurgedMark:
    entry_id: str
    seq: int
    purged_at: datetime
    reason: str


@dataclass(frozen=True)
class CompactionReport:
    doc: str
    compaction_id: str | None
    watermark_seq: int
    replaced: int
    synthetic: int
    checked_paths: int
    noop: bool = False


@dataclass(frozen=True)
class ArchiveReport:
    doc: str
    checked: bool
    differences: tuple[str, ...] = ()


@dataclass(frozen=True)
class ExpiryReport:
    doc: str
    through_seq: int
    expired: int


@dataclass(frozen=True)
class PurgeScope:
    """What to erase: every contribution at or below `path` (optionally of one `tier`), or the listed entry ids."""

    path: str | None = None
    tier: str | None = None
    entries: tuple[str, ...] = ()


@dataclass(frozen=True)
class PurgeReport:
    purge_id: str
    doc: str
    entries_erased: int
    entries_rewritten: int
    archive_erased: int
    effects_erased: int
    derived_erased: int
    audit_scrubbed: int
    expired_sources: bool = False


@dataclass(frozen=True)
class PurgeRecord:
    """One entry of the purge log: who erased what, when and why. Never a value."""

    id: str
    doc: str
    scope: str
    reason: str
    actor: str
    at: datetime
    counts: str


@dataclass(frozen=True)
class PurgeApplication:
    """What the storage changes in one purge: replacement entries (by id) for ledger and archive rows, the requests whose
    stored hash goes, and the derived fields whose effect-log records and derived-store rows go."""

    doc: str
    live: tuple[Entry, ...]
    archive: tuple[Entry, ...]
    requests: tuple[str, ...]
    fields: tuple[str, ...]


@dataclass(frozen=True)
class CompactionRecord:
    """One compaction (data-model.md): what range it replaced, under which rules, and what a replay of requests would have
    computed on the way (the attributed model errors and the skipped seqs at the watermark)."""

    id: str
    doc: str
    watermark_seq: int
    schema_version: int
    fold_fingerprint: str
    created: datetime
    policy: str
    base_errors: str  # canonical text of the model errors at the watermark
    base_skipped: tuple[int, ...]
    archived: bool
    first_tx_time: datetime | None
    last_tx_time: datetime | None
    has_valid_time: bool


@dataclass(frozen=True)
class SourceRow:
    """An entry a compaction replaced: kept after the archive is expired."""

    compaction_id: str
    source_id: str
    seq: int
    tier: str
    idem_key: str | None = None


@dataclass(frozen=True)
class CompactionPlan:
    """Everything one compaction changes, decided in advance by the pure planner."""

    record: CompactionRecord
    replaced: tuple[Entry, ...]  # the live entries to archive and remove
    synthetic: tuple[Entry, ...]  # one `layer` entry per tier, put in their place
    sources: tuple[SourceRow, ...]
    request_index: tuple[tuple[str, str, int], ...]  # (request id, entry id, seq) of the replaced contributed entries
    retired_keys: tuple[str, ...]  # idempotency keys of the replaced contributed entries
    # (request id, model errors it added, model errors it resolved) as canonical text: what a retry of it is answered with
    request_effects: tuple[tuple[str, str, str], ...] = ()


# -- feature 006: schema versioning and evolution -------------------------------------------------------------------------

ChangeClass = Literal["compatible", "visible", "breaking"]
_WORST = {"compatible": 0, "visible": 1, "breaking": 2}


@dataclass(frozen=True)
class Change:
    """One difference between two schema versions, with its class (docs/SPEC.md 14, contracts/changes.md)."""

    kind: str
    path: str  # a field pattern, a tier name, or "" for a top-level setting
    description: str  # one sentence; never a stored value
    change_class: ChangeClass
    classified: bool = True  # False: a difference the table does not name (listed as visible)
    detail: tuple[str, ...] = ()  # for a change that can shadow entries: the reasons it can give


@dataclass(frozen=True)
class ChangeList:
    old_version: int
    new_version: int
    changes: tuple[Change, ...]

    @classmethod
    def of(cls, old_version: int, new_version: int, changes: Sequence[Change]) -> ChangeList:
        """A list in the fixed order (path, kind): the same two versions always give the same list."""
        return cls(old_version, new_version, tuple(sorted(changes, key=lambda c: (c.path, c.kind))))

    @property
    def overall(self) -> ChangeClass:
        worst: ChangeClass = "compatible"
        for item in self.changes:
            if _WORST[item.change_class] > _WORST[worst]:
                worst = item.change_class
        return worst


@dataclass(frozen=True)
class Shadowed:
    """An entry a version leaves out of a document, and why (worked out each time, never stored)."""

    seq: int
    entry_id: str
    tier: str
    path: str
    reason: str  # tier_not_in_schema | now_derived | no_longer_conforms | cannot_be_merged
    detail: str = ""


@dataclass(frozen=True)
class HistoryRecord:
    id: int
    at: datetime
    operator: str
    action: str  # bootstrap | activate | rollback | pin | unpin | migrate
    doc: str | None
    from_version: int | None
    to_version: int | None
    change_class: str | None
    note: str = ""


@dataclass(frozen=True)
class StateChange:
    """One change of the shared schema state, written with its history record in a single transaction.

    `active` is the new active version (None leaves it); `pin_doc` with `pin_version` pins, with None unpins;
    `served` lists (version, fingerprint, parts) for versions served for the first time.
    """

    schema_id: str
    action: str  # bootstrap | activate | rollback | pin | unpin | migrate
    operator: str
    at: datetime
    doc: str | None
    from_version: int | None
    to_version: int | None
    change_class: str | None
    note: str
    active: int | None
    pin_doc: str | None
    pin_version: int | None
    served: tuple[tuple[int, str, str], ...] = ()
    pins: tuple[
        tuple[str, int], ...
    ] = ()  # more pins set by this change (the bootstrap adopts all of a registry's pins)


@dataclass(frozen=True)
class DocumentChange:
    """What would change in one document under another version (a dry run), or why that cannot be told."""

    doc: str
    document_changed: bool
    errors_added: tuple[str, ...]  # "rule@path" identities
    errors_resolved: tuple[str, ...]
    skipped_before: tuple[int, ...]
    skipped_after: tuple[int, ...]
    shadowed_by_reason: tuple[tuple[str, int], ...]
    would_call_hook: bool
    failure_code: str | None = None
    conflicts_changed: bool = False

    @property
    def failed(self) -> bool:
        return self.failure_code is not None

    @property
    def changed(self) -> bool:
        return (
            self.document_changed
            or bool(self.errors_added or self.errors_resolved)
            or self.skipped_before != self.skipped_after
            or self.conflicts_changed
        )


@dataclass(frozen=True)
class DryRunReport:
    change_list: ChangeList
    target: int
    generation: int  # the shared schema state it compared against
    examined: int
    left_out: int
    changed: int
    unchanged: int
    failed: int
    would_call_hook: int
    documents: tuple[DocumentChange, ...]


@dataclass(frozen=True)
class ActivationReport:
    version: int
    previous: int | None
    change_class: str | None
    rollback: bool
    rebuild: RebuildReport | None = None
    changed: tuple[str, ...] = ()  # with a rebuild: the documents whose answer changed (the dry run's list)


@dataclass(frozen=True)
class MigrationProgress:
    """Where a batch migration stands after a batch (the counts are of everything processed so far)."""

    batch: int
    processed: int
    total: int
    moved: int
    already_moved: int
    not_pinned: int
    failed: int


@dataclass(frozen=True)
class MigrationReport:
    moved: tuple[str, ...]
    already_moved: tuple[str, ...]
    not_pinned: tuple[str, ...]
    failed: tuple[tuple[str, str], ...]  # (document, code)
    batches: int
