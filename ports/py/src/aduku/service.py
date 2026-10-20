"""The `Aduku` facade: append, read entries, rejection audit admin (contracts/library-api.md)."""

from __future__ import annotations

import hashlib
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Sequence
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from functools import partial
from typing import Any, Literal

from aduku.audit import AuditRecord, RejectionAudit
from aduku.behaviors.registry import BehaviorRegistry
from aduku.engine import compaction as compaction_engine
from aduku.engine import dryrun
from aduku.engine import purge as purge_engine
from aduku.engine.admission import check_request
from aduku.engine.asof import Selection, point_of, select
from aduku.engine.compute import compute_fields, failure_mode_of
from aduku.engine.derive import (
    DerivationState,
    Prepared,
    layers_fit,
    prepare,
    state_from_layers,
)
from aduku.engine.effects import EffectView, PendingCall, fingerprint, view_from_records
from aduku.engine.explain import entry_touches, paths_touch
from aduku.engine.explain import explain as explain_path
from aduku.engine.layer_codec import decode_layer, encode_layer
from aduku.engine.model_errors import (
    Base,
    Extension,
    Origin,
    Replay,
    advance,
    can_have_errors,
    catch_up,
    extend,
    replay,
)
from aduku.engine.project import project
from aduku.engine.request_hash import request_hash
from aduku.engine.shadow import shadow_reasons
from aduku.engine.squash import spread
from aduku.engine.validate import ValidatorFault
from aduku.errors import (
    ComputeFault,
    ErrorCode,
    HistoryCompactedError,
    Reason,
    RejectedError,
    SchemaError,
    StorageError,
)
from aduku.fetch import DeferredStage
from aduku.hooks import HookRunner
from aduku.ion_codec import ABSENT, TOMBSTONE, FrozenStruct, IonError, normalize, parse, to_canonical, to_utc
from aduku.log import emit
from aduku.model import (
    ActivationReport,
    AppendResult,
    ArchiveReport,
    AsOf,
    ChangeList,
    CompactionReport,
    DeferredInfo,
    DeferredStatus,
    DocumentChange,
    DocumentResult,
    DryRunReport,
    EffectInfo,
    EffectRecord,
    Entry,
    EntryInput,
    ExpiryReport,
    Explanation,
    HistoryRecord,
    Identity,
    MigrationProgress,
    MigrationReport,
    ModelError,
    PurgeApplication,
    PurgedMark,
    PurgeRecord,
    PurgeReport,
    PurgeScope,
    RebuildReport,
    RecomputeReport,
    SchedulerReport,
    Shadowed,
)
from aduku.paths import PathError, format_path, parse_path
from aduku.scheduler import DEFAULT_BUDGET, Scheduler
from aduku.schema.diff import schema_changes
from aduku.schema.model import FieldSpec, Schema, conforms
from aduku.schema.registry import SchemaRegistry
from aduku.schema_state import StorageSchemaState
from aduku.storage.base import Clock, ConstraintError, IdSource, LayerRow, RequestRecord, Storage, Tx, ViewRow

__all__ = ["Aduku"]


def _encode_errors(errors: Sequence[ModelError]) -> str:
    return to_canonical(
        tuple(
            FrozenStruct(
                {
                    "rule": e.rule_id,
                    "path": e.path_scope,
                    "message": e.message,
                    **({"request": e.introduced_by_request} if e.introduced_by_request is not None else {}),
                    **(
                        {"version": e.introduced_by_schema_version}
                        if e.introduced_by_schema_version is not None
                        else {}
                    ),
                }
            )
            for e in errors
        )
    )


def _decode_errors(text: str) -> tuple[ModelError, ...]:
    return tuple(
        ModelError(item["rule"], item["path"], item["message"], item.get("request"), item.get("version"))
        for item in parse(text)
    )


def _by_hash(doc: str) -> str:
    """The order a limited dry run takes documents in: by a hash of the name, so it is the same each time."""
    return hashlib.sha256(doc.encode()).hexdigest()


def _fits(view: ViewRow | None, last: int, schema: Schema, effects_seq: int = 0, epoch: int = 0) -> bool:
    """A cached view serves a read only if it is of the latest entry and effect, and of this very version and rules."""
    return (
        view is not None
        and view.epoch == epoch
        and view.watermark_seq == last
        and view.schema_version == schema.version
        and view.rules_fingerprint == schema.rules_fingerprint
        and view.effects_seq == effects_seq
    )


def _decode_view(view: ViewRow) -> DocumentResult:
    notes = parse(view.provenance)
    return project(
        parse(view.document), _decode_errors(view.model_errors), notes.get("skipped", ()), notes.get("conflicts", ())
    )


def _scope_text(scope: PurgeScope) -> FrozenStruct:
    fields: dict[str, Any] = {}
    if scope.path is not None:
        fields["path"] = scope.path
    if scope.tier is not None:
        fields["tier"] = scope.tier
    if scope.entries:
        fields["entries"] = tuple(scope.entries)
    return FrozenStruct(fields)


def _known_request(
    tx: Tx, doc: str, contributor: Identity, request_key: str, digest: str | None
) -> AppendResult | None:
    """The duplicate answer for a request key already used, None if it is new. A different payload is refused."""
    known = tx.find_request(doc, contributor.id, request_key)
    if known is None:
        return None
    # a request whose values were erased by a purge has no hash left to compare (a hash of a value is a copy of it)
    if known.request_hash is not None and known.request_hash != digest:
        raise RejectedError(
            ErrorCode.REQUEST_KEY_REUSED, message=f"request key {request_key!r} was used for a different request"
        )
    return AppendResult(
        entries=tuple(tx.request_entries(known.request_id)), request_id=known.request_id, duplicate=True
    )


class _Cold(Exception):
    """The kept state is too far behind the ledger to catch up inside the write lock (internal)."""


class _Flight:
    """One hook call in progress, shared by everyone who needs the same outcome at the same moment."""

    def __init__(self) -> None:
        self.done = threading.Event()
        self.record: EffectRecord | None = None


class _Pending(Exception):
    """The document changed while its hooks ran, so some inputs have no outcome yet: call again outside the lock."""


class Aduku:
    WARM_ATTEMPTS = 3
    CATCH_UP_LIMIT = 64  # entries another process may have appended since the fold, folded inside the lock

    def __init__(
        self,
        storage: Storage,
        schemas: SchemaRegistry,
        *,
        clock: Clock | None = None,
        ids: IdSource | None = None,
        behaviors: BehaviorRegistry | None = None,
        state_cache_size: int = 256,
        hook_runner: HookRunner | None = None,
        fetch_deadline: float = 5.0,
        monotonic: Callable[[], float] | None = None,
        sleep: Callable[[float], None] | None = None,
        scheduler_budget: float = DEFAULT_BUDGET,
        layers: bool = True,
        schema_state: bool = True,
    ) -> None:
        self.storage = storage
        # Feature 006: share the active version and the pins through the store. False keeps them in this process only
        # (the behavior before feature 006); no merged result depends on it.
        self.schema_state = schema_state
        # Feature 005: save and use per-tier layers. False runs the same service without them (for tests and conformance:
        # no result may depend on it).
        self.layers = layers
        self.schemas = schemas
        self.clock = clock or Clock.system()
        self.ids = ids or IdSource.system()
        self.behaviors = behaviors or BehaviorRegistry.with_builtins()
        self._audit = RejectionAudit(lambda: self.storage.audit_backend(), self.clock)
        # In-memory per-tier layers, so that a read or an append folds only the entries it has not seen. Disposable:
        # losing it only costs a derivation from the ledger.
        self._states: OrderedDict[str, DerivationState] = OrderedDict()
        self._shadow_cache: OrderedDict[tuple[Any, ...], tuple[Shadowed, ...]] = (
            OrderedDict()
        )  # reasons by ledger state
        self._views: OrderedDict[str, ViewRow] = OrderedDict()  # views derived by reads, which never write to storage
        self._hooks = hook_runner or HookRunner()
        if not 0.0 < fetch_deadline <= 60.0:
            raise ValueError("fetch_deadline is above zero and at most 60 seconds")
        self._deferred = DeferredStage(
            storage, self.behaviors, self._hooks, self.clock, monotonic or time.monotonic, fetch_deadline
        )
        self._scheduler = Scheduler(
            storage,
            self._deferred,
            self.clock,
            self.ids,
            monotonic or time.monotonic,
            sleep or time.sleep,
            scheduler_budget,
            self.doc_ids,
            self._core_of,
        )
        self._flights: dict[
            tuple[str, str, str, str], _Flight
        ] = {}  # calls in progress, by (doc, field, fingerprint, hook)
        self._retried_effects: OrderedDict[
            tuple[str, str, int, str], tuple[tuple[ModelError, ...], tuple[ModelError, ...]]
        ] = OrderedDict()
        self._bases: dict[str, Base] = {}  # compaction id -> what a replay had computed when it was made (immutable)
        self._states_lock = threading.Lock()
        self._prepared: dict[
            tuple[str, int], Prepared
        ] = {}  # per schema version: the merge context, defaults and fallbacks
        self.state_cache_size = state_cache_size  # documents kept in memory; the least recently used are dropped
        self.derivations = 0  # how many times a document was derived (not served from a cache); for tests
        if schema_state and schemas.schema_id is not None:
            # Feature 006: from here the store decides the active version and the pins; the registry's own are adopted
            # only when the store holds none (and only if the ledger can be served by them). Then what is in force is checked.
            schemas.watch(self._tiers_in_use, self._compaction_problem)
            schemas.bind(StorageSchemaState(storage, schemas.schema_id), self.clock.now)
        schemas.guard(
            self._tiers_in_use, self._compaction_problem
        )  # activating or pinning checks it against the ledger

    # -- write ---------------------------------------------------------------------------------------------------

    def append(
        self,
        doc: str,
        entries: Sequence[EntryInput],
        *,
        contributor: Identity,
        lenient: bool = False,
        expected_seq: int | None = None,
        request_key: str | None = None,
    ) -> AppendResult:
        """Submit one atomic request: every entry is accepted, or none is (FR-029). Raises RejectedError."""
        entries = list(entries)
        if not entries:
            raise ValueError("a request must contain at least one entry")
        try:
            schema = self.schemas.version_for(doc)
        except RejectedError as rejection:  # no schema to govern the request: logged, and audited if one can be found
            self._log("rejected", doc, contributor, [r.code.value for r in rejection.reasons])
            try:
                self._record_rejection(self.schemas.active(), doc, contributor, entries, lenient, rejection)
            except RejectedError:
                pass
            raise
        digest = request_hash(doc, contributor.id, lenient, entries) if request_key is not None else None
        try:
            result, folded = self._retried(doc, contributor, request_key, digest) or self._admit_warm(
                schema, doc, entries, contributor, lenient, expected_seq, request_key, digest
            )
        except RejectedError as rejection:
            self._log("rejected", doc, contributor, [r.code.value for r in rejection.reasons])
            self._record_rejection(schema, doc, contributor, entries, lenient, rejection)
            raise
        self._log("duplicate" if result.duplicate else "accepted", doc, contributor, [result.request_id])
        if folded is None:  # a retry: nothing was written, so say what the original did and serve the latest view
            return self._with_view(doc, self._with_effects(schema, doc, result))
        self._remember(doc, folded)  # only now that the request is committed
        return self._with_stored_deferred(schema, doc, result)

    def _retried(
        self, doc: str, contributor: Identity, request_key: str | None, digest: str | None
    ) -> tuple[AppendResult, None] | None:
        """The answer to a retry, found in a read: a retry needs neither the write lock nor a fold."""
        if request_key is None:
            return None
        with self.storage.read() as tx:
            found = _known_request(tx, doc, contributor, request_key, digest)
        return (found, None) if found is not None else None

    def _admit_warm(
        self,
        schema: Schema,
        doc: str,
        entries: list[EntryInput],
        contributor: Identity,
        lenient: bool,
        expected_seq: int | None,
        request_key: str | None,
        digest: str | None,
    ) -> tuple[AppendResult, DerivationState | None]:
        """Admit in a write transaction, after folding the ledger outside it.

        A long fold must not happen while the write lock is held. If the document changed since the fold (another
        process, or an evicted cache), the transaction is abandoned before anything is written, the fold is redone
        outside the lock, and only the last attempt folds inline.
        """
        for attempt in range(self.WARM_ATTEMPTS):
            self._warm(doc, schema, refresh=attempt > 0)
            fresh = self._outcomes_for(schema, doc, entries, contributor, lenient)  # hooks run here, outside every lock
            try:
                with self.storage.transaction() as tx:
                    return self._admit(
                        tx,
                        schema,
                        doc,
                        entries,
                        contributor,
                        lenient,
                        expected_seq,
                        request_key,
                        digest,
                        inline=attempt == self.WARM_ATTEMPTS - 1,
                        fresh=fresh,
                    )
            except (_Cold, _Pending):
                continue
        raise RejectedError(
            ErrorCode.SEQ_CONFLICT, message="the document kept changing while its hooks ran; submit the request again"
        )

    def _admit(
        self,
        tx: Tx,
        schema: Schema,
        doc: str,
        entries: list[EntryInput],
        contributor: Identity,
        lenient: bool,
        expected_seq: int | None,
        request_key: str | None,
        digest: str | None,
        inline: bool = True,
        fresh: Sequence[EffectRecord] = (),
    ) -> tuple[AppendResult, DerivationState | None]:
        ready = self._engine(schema)
        if request_key is not None:
            duplicate = _known_request(tx, doc, contributor, request_key, digest)
            if duplicate is not None:
                return duplicate, None
        last = tx.last_seq(doc)
        if expected_seq is not None and expected_seq != last:
            raise RejectedError(
                ErrorCode.SEQ_CONFLICT, message=f"expected sequence number {expected_seq} but the document is at {last}"
            )

        reasons = check_request(schema, entries, contributor, lenient, self.behaviors, ctx=ready.ctx)
        for index, entry in enumerate(entries):
            if entry.idem_key is not None and tx.idem_key_seen(doc, entry.idem_key):
                reasons.append(
                    Reason(
                        ErrorCode.DUPLICATE_IDEM_KEY,
                        "this idempotency key was already used for the document",
                        entry_index=index,
                        path=entry.path,
                    )
                )
        if reasons:
            end = len(entries)
            reasons.sort(key=lambda r: end if r.entry_index is None else r.entry_index)
            raise RejectedError(reasons[0].code, reasons)

        request_id = self.ids.new_id()
        now = self.clock.now()
        try:
            stored_entries = self._stored(
                entries, doc, last, request_id, now, schema, contributor, lenient, request_key, digest
            )
        except IonError as error:  # a value the codec cannot write back is not storable (checked here as a last resort)
            raise RejectedError(
                ErrorCode.TYPE_MISMATCH, message=f"a value has no canonical Ion form: {error}"
            ) from error
        # Dry run: fold the request onto the document as it stands and validate the result, once for the whole request.
        effects = self._effect_view(tx, ready, doc)
        stored_view = effects
        if effects is not None and fresh:  # outcomes of hooks called for this request, not stored until it is accepted
            effects = effects.with_outcomes(view_from_records(fresh))
        state, behind = self._load(tx, schema, doc, last, effects.seq if effects else 0)
        if not inline and (len(behind) > self.CATCH_UP_LIMIT or (state is None and behind)):
            raise _Cold  # nothing is written yet: fold outside the lock and come back
        try:
            base = self._catch_up(schema, state, behind, effects)
            ext = extend(
                schema, self.behaviors, ready, base, stored_entries, Origin(request_id=request_id), base.errors, effects
            )
        except ValidatorFault as fault:
            raise RejectedError(ErrorCode.VALIDATION_FAILED, message=str(fault)) from fault
        except ComputeFault as fault:
            raise RejectedError(ErrorCode.COMPUTE_FAULT, message=str(fault)) from fault
        if ext.pending:
            raise _Pending  # the document is not what the hooks were called for: nothing is written yet
        self.derivations += 1
        self._refuse_unusable(entries, stored_entries, base, ext)
        self._refuse_failed_hooks(schema, fresh, ext)
        # An impure failure under model_error_on_failure is recorded and does not count against the request (FR-024).
        counted = [e for e in ext.added if not e.rule_id.startswith("impure:")]
        if counted and not lenient:  # the flag, if given, was checked in check_request: only a permitted writer has it
            code = ErrorCode.MODEL_ERROR_INTRODUCED if base.errors else ErrorCode.VALIDATION_FAILED
            raise RejectedError(
                code,
                [
                    Reason(code, f"{e.rule_id} fails at {e.path_scope}: {e.message}", rule=e.rule_id, path=e.path_scope)
                    for e in counted
                ],
            )

        try:
            tx.append_request(RequestRecord(request_id, doc, contributor.id, request_key, digest), stored_entries)
        except ConstraintError as error:
            raise StorageError(f"ledger constraint violated: {error}") from error
        state_now = replace(
            ext.state, epoch=tx.epoch(doc)
        )  # the epoch it was derived in, not the one seen when remembered
        to_store = [
            r for r in fresh if stored_view is not None and stored_view.lookup(r.field, r.fingerprint, r.hook) is None
        ]
        if to_store:  # the request is accepted: its outcomes become part of the log, in the same transaction
            tx.append_effects(to_store)
            state_now = replace(state_now, effects_seq=tx.effects_seq(doc))
        document, text = self._settle(ext.document)
        tx.put_view(self._view_row(doc, last + len(stored_entries), schema, text, state_now, tx.epoch(doc)))
        self._save_layers(tx, doc, schema, state_now, {entry.tier for entry in entries})
        result = AppendResult(
            entries=tuple((e.id, e.seq) for e in stored_entries),
            request_id=request_id,
            model_errors_added=ext.added,
            model_errors_resolved=ext.resolved,
            view=project(document, ext.errors, ext.state.skipped, ext.conflicts),
        )
        return result, state_now

    def _stored(
        self,
        entries: list[EntryInput],
        doc: str,
        last: int,
        request_id: str,
        now: datetime,
        schema: Schema,
        contributor: Identity,
        lenient: bool,
        request_key: str | None,
        digest: str | None,
    ) -> list[Entry]:
        return [
            Entry(
                id=self.ids.new_id(),
                doc=doc,
                seq=last + 1 + offset,
                request_id=request_id,
                tier=entry.tier,
                path=entry.path,
                op=entry.op,
                value=TOMBSTONE if entry.op == "delete" else normalize(entry.value),  # the same bytes in every backend
                contributor=contributor.id,
                tx_time=now,
                schema_version=schema.version,
                idem_key=entry.idem_key,
                request_key=request_key,
                request_hash=digest,
                lenient=lenient,
                meta=normalize(entry.meta) if entry.meta is not None else None,
                valid_time=to_utc(entry.valid_time).replace(tzinfo=UTC) if entry.valid_time is not None else None,
            )
            for offset, entry in enumerate(entries)
        ]

    @staticmethod
    def _refuse_failed_hooks(schema: Schema, fresh: Sequence[EffectRecord], ext: Extension) -> None:
        """Under reject_on_failure, a hook that failed for this request rejects it; nothing, outcomes included, is kept."""
        modes = {spec.pattern: failure_mode_of(schema, spec) for spec in schema.fields if spec.impure is not None}
        failed = [
            record
            for record in fresh
            if record.outcome == "failure"
            and modes.get(record.field) == "reject_on_failure"
            and any(error.rule_id == f"impure:{record.field}" for error in ext.errors)
        ]
        if failed:
            raise RejectedError(
                ErrorCode.IMPURE_COMPUTE_FAILED,
                [
                    Reason(
                        ErrorCode.IMPURE_COMPUTE_FAILED,
                        f"{record.hook} failed for {record.field} ({record.error}) after {record.attempts} attempt(s)",
                        rule=f"impure:{record.field}",
                        path=record.field,
                    )
                    for record in failed
                ],
            )

    def _refuse_unusable(
        self, entries: list[EntryInput], stored: list[Entry], base: DerivationState, ext: Extension
    ) -> None:
        """An entry no rule can merge with what the document holds is a schema error (SPEC 6.5): never recorded."""
        reasons = [
            Reason(
                ErrorCode.TYPE_MISMATCH,
                "this entry cannot be merged with the values the document already holds",
                entry_index=index,
                path=entries[index].path,
            )
            for index, entry in enumerate(stored)
            if entry.seq in ext.state.skipped
        ]
        reasons += [
            Reason(
                ErrorCode.TYPE_MISMATCH,
                f"the value at {path} cannot be merged with the value another tier holds",
                path=path,
            )
            for path in sorted(set(ext.conflicts) - set(base.conflicts))
        ]
        if reasons:
            raise RejectedError(reasons[0].code, reasons)

    def _with_effects(self, schema: Schema, doc: str, result: AppendResult) -> AppendResult:
        """What an earlier request did to the model errors, found by replaying the ledger (to answer a retry)."""
        if not can_have_errors(schema):
            return result
        key = (doc, result.request_id, schema.version, schema.rules_fingerprint)
        with self._states_lock:
            memo = self._retried_effects.get(key)
        if memo is None:  # the ledger never changes, so what a request did never changes: work it out once
            with self.storage.read() as tx:
                recorded = tx.request_effects(result.request_id)  # a request a compaction replaced: kept when it was
                entries = self._entries(tx, doc, schema) if recorded is None else []
                effects = self._effect_view(tx, self._engine(schema), doc)
            if recorded is not None:
                memo = (_decode_errors(recorded[0]), _decode_errors(recorded[1]))
            else:
                replayed = self._replay(schema, entries, capture=result.request_id, effects=effects)
                before = {e.identity for e in replayed.before}
                after = {e.identity for e in replayed.after}
                memo = (
                    tuple(e for e in replayed.after if e.identity not in before),
                    tuple(e for e in replayed.before if e.identity not in after),
                )
            with self._states_lock:
                self._retried_effects[key] = memo
                while len(self._retried_effects) > self.state_cache_size:
                    self._retried_effects.popitem(last=False)
        return replace(result, model_errors_added=memo[0], model_errors_resolved=memo[1])

    def _record_rejection(
        self,
        schema: Schema,
        doc: str,
        contributor: Identity,
        entries: list[EntryInput],
        lenient: bool,
        rejection: RejectedError,
    ) -> None:
        """Best effort: a failing audit write never changes the outcome of the request (FR-027)."""
        try:
            self._audit.record(schema, doc, contributor, entries, lenient, rejection)
        except Exception as error:
            emit("audit_write_failed", doc=doc, error=str(error))

    def _with_view(self, doc: str, result: AppendResult) -> AppendResult:
        """Attach the latest view of the document. A retry wrote nothing, so a failed read is reported, not hidden."""
        schema = self.schemas.version_for(doc)
        return self._with_stored_deferred(schema, doc, replace(result, view=self._core(doc, schema)))

    def _with_stored_deferred(self, schema: Schema, doc: str, result: AppendResult) -> AppendResult:
        """Show the deferred values already in the store in a write's view. A write never calls a source."""
        if result.view is None:
            return result
        view = self._with_shadowed(doc, schema, result.view)
        if DeferredStage.has_deferred(schema):
            view = self._deferred.stored(schema, doc, view)
        return replace(result, view=view)

    def _with_shadowed(
        self, doc: str, schema: Schema, result: DocumentResult, entries: Sequence[Entry] | None = None
    ) -> DocumentResult:
        """Say why each skipped entry is left out (feature 006). Worked out here, from the same checks as the fold, and
        only when something was skipped; `entries` are the ones the result was derived from (the ledger's when None)."""
        if not result.skipped:
            return result
        if entries is not None:  # an as-of read: its own selection of entries, nothing to cache
            return replace(
                result,
                shadowed=tuple(shadow_reasons(schema, self.behaviors, self._engine(schema), entries, result.skipped)),
            )
        with self.storage.read() as tx:
            key = (
                doc,
                schema.id,
                schema.version,
                schema.rules_fingerprint,
                tx.last_seq(doc),
                tx.epoch(doc),
                len(tx.purge_log(doc)),  # a purge changes neither the last seq nor the epoch
                result.skipped,
            )
            with self._states_lock:
                known = self._shadow_cache.get(key)
                if known is not None:
                    self._shadow_cache.move_to_end(key)
            if known is None:  # worked out once per state of the ledger and version, then served from memory
                known = tuple(
                    shadow_reasons(
                        schema,
                        self.behaviors,
                        self._engine(schema),
                        self._shadow_entries(tx, doc, schema),
                        result.skipped,
                    )
                )
                with self._states_lock:
                    self._shadow_cache[key] = known
                    while len(self._shadow_cache) > self.state_cache_size:
                        self._shadow_cache.popitem(last=False)
        return replace(result, shadowed=known)

    def _shadow_entries(self, tx: Tx, doc: str, schema: Schema) -> list[Entry]:
        """The entries a derivation under `schema` folded, restored from the archive where it is still kept (never raises)."""
        entries = tx.entries(doc, 0)
        if tx.epoch(doc) == 0:
            return entries
        records = tx.compactions(doc)
        if not records or all(r.fold_fingerprint == schema.fold_fingerprint for r in records):
            return entries
        return self._restore(tx, doc, entries, strict=False)[0]

    # -- read ----------------------------------------------------------------------------------------------------

    def get(
        self,
        doc: str,
        *,
        consistency: Literal["cached", "fresh"] = "cached",
        include: Sequence[str] = (),
        as_of: AsOf | None = None,
    ) -> DocumentResult:
        """The merged document: a pure function of the ledger and the schema version the document is read under.

        `as_of` (feature 005) reads the document at an earlier point: a seq, a recording time or a moment in the world. Such
        a read writes nothing and calls no hook or source; deferred fields are listed `unavailable` and have no value.

        A read never takes the write lock: a view it has to derive is kept in memory only. The stored view is written
        by `append` and `rebuild`. Deferred fields (feature 003) are overlaid after that: sources that are due are
        called within the fetch deadline, and a failing source only changes a field's status. `consistency="fresh"`
        ignores the `ttl` of `per_fetch` fields; `include=("pending",)` says what each pending field waits for.
        """
        if consistency not in ("cached", "fresh"):
            raise ValueError("consistency is cached or fresh")
        if as_of is not None:
            return self._as_of_document(doc, as_of)
        schema = self.schemas.version_for(doc)
        core = self._core(doc, schema)
        if DeferredStage.has_deferred(schema):
            core = self._deferred.read(schema, doc, core, consistency=consistency, detail="pending" in include)
        return self._with_shadowed(doc, schema, core)

    def _core(self, doc: str, schema: Schema) -> DocumentResult:
        """P1 to P6 and the projection: everything that does not depend on the derived store."""
        ready = self._engine(schema)
        with self.storage.read() as tx:
            last = tx.last_seq(doc)
            effects = self._effect_view(tx, ready, doc)
            seen = effects.seq if effects else 0
            epoch = tx.epoch(doc)
            view = tx.get_view(doc)
            if not _fits(view, last, schema, seen, epoch):
                view = self._kept_view(doc)
            if view is not None and _fits(view, last, schema, seen, epoch):
                return _decode_view(view)
            state, entries = self._load(tx, schema, doc, last, seen)
        ext = self._rebuild(schema, state, entries, effects)  # outside the read transaction
        if ext.pending:  # impure fields with no outcome for their inputs: call the hooks, record, and derive again
            ext, effects = self._fill(
                schema, doc, ext, effects or EffectView(), lambda view: self._rebuild(schema, state, entries, view)
            )
        self.derivations += 1
        self._remember(doc, replace(ext.state, epoch=epoch))
        document, text = self._settle(ext.document)
        self._keep_view(self._view_row(doc, last, schema, text, ext.state, epoch))
        return project(document, ext.errors, ext.state.skipped, ext.conflicts)

    def _as_of_inputs(
        self, doc: str, schema: Schema, ready: Prepared, as_of: AsOf
    ) -> tuple[Selection, EffectView | None, set[str]]:
        """The entries that take part at `as_of`, the recorded outcomes of impure computes and the ids that came out of the
        archive; reads only."""
        point_of(as_of)  # one point, or a refusal, before anything is read
        as_of = AsOf(
            as_of.seq,
            None if as_of.tx_time is None else to_utc(as_of.tx_time).replace(tzinfo=UTC),
            None if as_of.valid_time is None else to_utc(as_of.valid_time).replace(tzinfo=UTC),
        )
        with self.storage.read() as tx:
            # the originals the archive still keeps stand in for the synthetic entries, so any point inside a compacted range
            # is answered exactly; what is expired is refused below (feature 005, FR-029)
            entries, archived = self._restore(tx, doc, tx.entries(doc, 0), strict=False)
            self._refuse_expired_point(tx, doc, as_of)
            effects = self._effect_view(tx, ready, doc)
            tx_seq = tx.seq_at_tx_time(doc, as_of.tx_time) if as_of.tx_time is not None else None
        return select(entries, as_of, tx_seq=tx_seq), effects, archived

    @staticmethod
    def _refuse_expired_point(tx: Tx, doc: str, as_of: AsOf) -> None:
        """Refuse a point that falls inside a compacted range whose originals were expired (never a wrong document)."""
        expired = [c for c in tx.compactions(doc) if not c.archived]
        for record in expired:
            if as_of.seq is not None:
                inside = as_of.seq < record.watermark_seq
            elif as_of.tx_time is not None:
                first, last = record.first_tx_time, record.last_tx_time
                inside = first is not None and last is not None and first <= as_of.tx_time < last
            else:
                inside = record.has_valid_time
            if inside:
                earliest = max(c.watermark_seq for c in expired)
                raise HistoryCompactedError(
                    f"{doc!r} was compacted up to seq {earliest} and the originals are no longer kept, so this point "
                    f"cannot be answered exactly; the earliest seq that can be is {earliest}",
                    earliest_seq=earliest,
                )

    def _as_of_document(self, doc: str, as_of: AsOf, schema: Schema | None = None) -> DocumentResult:
        """The document at a point of its history, under the schema version it has now (feature 005, research R-11)."""
        schema = schema or self.schemas.version_for(doc)
        ready = self._engine(schema)
        selection, effects, _ = self._as_of_inputs(doc, schema, ready, as_of)
        ext = self._replay(schema, selection.entries, effects=effects).final
        # an impure field with no recorded outcome for its inputs at that point: a model error, never a hook call
        missing = tuple(
            ModelError("NO_RECORDED_OUTCOME", call.field, "no outcome was recorded for this field's inputs")
            for call in ext.pending
        )
        errors = tuple(sorted((*ext.errors, *missing), key=lambda error: error.identity))
        document, _ = self._settle(ext.document)
        result = project(document, errors, ext.state.skipped, ext.conflicts)
        unavailable = tuple(
            DeferredStatus(spec.pattern, "unavailable", reason="history_not_kept")
            for spec in sorted(schema.fields, key=lambda spec: spec.pattern)
            if spec.deferred is not None
        )
        return self._with_shadowed(
            doc, schema, replace(result, deferred=unavailable, as_of=selection.info), selection.entries
        )

    def explain(self, doc: str, path: str, *, as_of: AsOf | None = None) -> Explanation:
        """Why `path` has its value in `doc`: the winning entry, the rule, and the entries that made no difference.

        Worked out from the ledger under the schema version the document is read under. Raises RejectedError
        (UNKNOWN_PATH) for a path the schema does not declare. `shadowed_by_schema` (feature 006) lists the entries this
        version leaves out that touch the path, with their reasons.
        """
        schema = self.schemas.version_for(
            doc
        )  # once: the explanation and the shadow reasons come from the same version
        explained = self._explain(doc, path, as_of, schema)
        result = self._core(doc, schema) if as_of is None else self._as_of_document(doc, as_of, schema)
        if as_of is None:
            result = self._with_shadowed(doc, schema, result)
        if not result.shadowed:
            return explained
        target = parse_path(path)

        def touches(path_text: str) -> bool:
            try:
                return paths_touch(parse_path(path_text), target)
            except PathError:  # a compacted layer has no single path: it may hold the one asked about
                return True

        return replace(explained, shadowed_by_schema=tuple(r for r in result.shadowed if touches(r.path)))

    def _explain(self, doc: str, path: str, as_of: AsOf | None, schema: Schema) -> Explanation:
        try:
            parsed = parse_path(path)
        except PathError as error:
            raise RejectedError(ErrorCode.UNKNOWN_PATH, message=f"invalid path: {error}") from error
        if schema.field_for(parsed) is None:
            raise RejectedError(ErrorCode.UNKNOWN_PATH, message="the schema does not declare this path")
        derived = schema.derived_field_at_or_above(parsed)
        if as_of is not None:
            return self._explain_as_of(doc, path, parsed, schema, derived, as_of)
        if derived is not None and derived.derived == "deferred":
            return self._with_authority(
                schema, parsed, self._deferred.explain(schema, doc, self._core(doc, schema), derived.pattern, path)
            )
        ready = self._engine(schema)
        if ready.has_impure:
            self.get(doc)  # makes sure every impure field has an outcome for its current inputs
        with self.storage.read() as tx:
            entries: list[Entry]
            archived: set[str]
            if tx.epoch(doc) == 0:
                entries, archived = tx.entries(doc, 0), set()
            else:  # exact while the archive keeps the originals; a synthetic entry is one entry once they are expired
                entries, archived = self._restore(tx, doc, tx.entries(doc, 0), strict=False)
            marks = self._purged_marks(tx, doc, entries, parsed)
            effects = self._effect_view(tx, ready, doc)
        errors = self._replay(schema, entries, effects=effects).final.errors  # of the same entries, so one snapshot
        return self._with_authority(
            schema,
            parsed,
            replace(
                self._with_compaction(
                    explain_path(schema, self.behaviors, ready, entries, path, errors, effects), entries, archived
                ),
                purged=marks,
            ),
        )

    @staticmethod
    def _purged_marks(tx: Tx, doc: str, entries: list[Entry], path: Any) -> tuple[PurgedMark, ...]:
        """The purged entries that touched `path`, with when and why they were erased (feature 005)."""
        placeholders = [e for e in entries if e.purged and entry_touches(e, path)]
        if not placeholders:
            return ()
        by_entry: dict[str, PurgeRecord] = {}
        for record in tx.purge_log(doc):
            for entry_id in parse(record.counts).get("ids", ()):
                by_entry.setdefault(str(entry_id), record)
        return tuple(
            PurgedMark(e.id, e.seq, by_entry[e.id].at, by_entry[e.id].reason) for e in placeholders if e.id in by_entry
        )

    def _with_compaction(self, why: Explanation, entries: list[Entry], archived: set[str]) -> Explanation:
        """Say whether a synthetic entry took part in the answer (and what it stands for) or the originals were used."""
        named = {why.winner, *why.inputs, *why.shadowed}
        synthetic = {e.id: e for e in entries if e.synthetic is not None and e.id in named}
        if synthetic:
            with self.storage.read() as tx:
                sources = tuple(row.source_id for e in synthetic.values() for row in tx.sources_of(e.synthetic or ""))
            return replace(why, originals_kept=False, sources=sources)
        if named & archived:
            return replace(why, originals_kept=True)
        return why

    def _explain_as_of(
        self, doc: str, path: str, parsed: Any, schema: Schema, derived: FieldSpec | None, as_of: AsOf
    ) -> Explanation:
        ready = self._engine(schema)
        selection, effects, archived = self._as_of_inputs(doc, schema, ready, as_of)
        if derived is not None and derived.derived == "deferred":  # no history is kept for a deferred value
            return self._with_authority(
                schema, parsed, Explanation(path, None, "deferred", None, None, status="unavailable")
            )
        errors = self._replay(schema, selection.entries, effects=effects).final.errors
        return self._with_authority(
            schema,
            parsed,
            self._with_compaction(
                explain_path(schema, self.behaviors, ready, selection.entries, path, errors, effects),
                selection.entries,
                archived,
            ),
        )

    @staticmethod
    def _with_authority(schema: Schema, parsed: Any, explanation: Explanation) -> Explanation:
        """Add who may write the path and which field said so (SPEC 11); nobody can write a derived path."""
        found = None if schema.derived_field_at_or_above(parsed) is not None else schema.authority_for(parsed)
        return replace(explanation, authority=found[0] if found else None, authority_from=found[1] if found else None)

    def _kept_view(self, doc: str) -> ViewRow | None:
        with self._states_lock:
            return self._views.get(doc)

    def _keep_view(self, row: ViewRow) -> None:
        with self._states_lock:
            kept = self._views.get(row.doc)
            if kept is None or kept.watermark_seq <= row.watermark_seq:
                self._views[row.doc] = row
            self._views.move_to_end(row.doc)
            while len(self._views) > self.state_cache_size:
                self._views.popitem(last=False)

    def _tiers_in_use(self, doc: str | None) -> set[str]:
        with self.storage.read() as tx:
            return tx.tiers_in_use(doc)

    # -- rebuild -------------------------------------------------------------------------------------------------

    def rebuild(self, doc: str | None = None) -> RebuildReport:
        """Derive from the ledger alone and compare with the cached view (all documents when `doc` is None).

        A view that is current but differs is reported and repaired; one that is missing or from another schema
        version is derived afresh and stored. The ledger is never written.
        """
        with self.storage.read() as tx:
            names = [doc] if doc is not None else tx.doc_ids()
        return self._rebuild_names(names)

    def _rebuild_names(self, names: list[str]) -> RebuildReport:
        differing: list[str] = []
        refreshed: list[str] = []
        failed: list[tuple[str, str]] = []
        layers_differing: list[tuple[str, str]] = []
        layers_refreshed: list[tuple[str, str]] = []
        for name in names:
            try:
                outcome, tiers_differ, tiers_fresh = self._rebuild_one(name)
                layers_differing.extend((name, tier) for tier in tiers_differ)
                layers_refreshed.extend((name, tier) for tier in tiers_fresh)
            except (
                ValidatorFault,
                ComputeFault,
                RejectedError,
                SchemaError,
            ) as error:  # this document only: go on with the rest
                failed.append(
                    (name, f"{getattr(getattr(error, 'code', None), 'value', type(error).__name__)}: {error}")
                )
                continue
            if outcome == "differs":
                differing.append(name)
            elif outcome == "refreshed":
                refreshed.append(name)
        report = RebuildReport(
            tuple(names),
            tuple(differing),
            tuple(refreshed),
            tuple(failed),
            tuple(layers_differing),
            tuple(layers_refreshed),
        )
        emit(
            "rebuild",
            checked=len(names),
            differing=differing,
            refreshed=refreshed,
            failed=[d for d, _ in failed],
            layers_differing=layers_differing,
            layers_refreshed=layers_refreshed,
        )
        return report

    def _rebuild_one(self, doc: str) -> tuple[str, list[str], list[str]]:
        schema = self.schemas.version_for(doc)
        ready = self._engine(schema)
        with self.storage.read() as tx:
            last = tx.last_seq(doc)
            cached = tx.get_view(doc)
            entries = self._entries(tx, doc, schema)
            effects = self._effect_view(tx, ready, doc)
            epoch = tx.epoch(doc)
            saved = {row.tier: row for row in tx.get_layers(doc)} if self.layers else {}
        if last == 0:  # no entries: nothing to derive, cache or compare
            return "same", [], []
        ext = self._replay(schema, entries, effects=effects).final  # from the ledger alone: no kept state is trusted
        if ext.pending:
            ext, effects = self._fill(
                schema,
                doc,
                ext,
                effects or EffectView(),
                lambda view: self._replay(schema, entries, effects=view).final,
            )
        self.derivations += 1
        _, text = self._settle(ext.document)
        fresh = self._view_row(doc, last, schema, text, ext.state, epoch)
        self._remember(doc, replace(ext.state, epoch=epoch))
        wanted, layers_differ, layers_fresh = self._compare_layers(doc, schema, ext.state, entries, epoch, saved)
        if cached == fresh and not layers_differ and not layers_fresh:
            return "same", [], []
        with self.storage.transaction() as tx:  # repair, unless something was appended in the meantime
            if tx.last_seq(doc) == last and tx.epoch(doc) == epoch:
                if cached != fresh:
                    tx.put_view(fresh)
                if layers_differ or layers_fresh:
                    tx.drop_layers(doc)
                    tx.put_layers(wanted)
        if layers_differ or layers_fresh:
            emit("layers_repaired", doc=doc, differing=layers_differ, refreshed=layers_fresh)
        if cached == fresh:
            return "same", layers_differ, layers_fresh
        outcome = "differs" if _fits(cached, last, schema, fresh.effects_seq, epoch) else "refreshed"
        return outcome, layers_differ, layers_fresh

    def _compare_layers(
        self,
        doc: str,
        schema: Schema,
        state: DerivationState,
        entries: Sequence[Entry],
        epoch: int,
        saved: dict[str, LayerRow],
    ) -> tuple[list[LayerRow], list[str], list[str]]:
        """The layers the ledger gives, and the tiers whose saved layer differs from them or is missing or stale.

        A saved layer made under this very version and epoch whose text or coverage is not what the ledger gives is a
        difference, unless it merely lags behind (a writer that did not get to save it): that is stale, like a missing
        layer or one from another version.
        """
        if not self.layers:
            return [], [], []
        last_by_tier: dict[str, int] = {}
        for entry in entries:
            last_by_tier[entry.tier] = max(last_by_tier.get(entry.tier, 0), entry.seq)
        wanted: list[LayerRow] = []
        differ: list[str] = []
        stale: list[str] = []
        for tier in schema.tiers:
            seq = last_by_tier.get(tier, 0)
            row = saved.get(tier)
            if seq == 0:
                if row is not None and row.tier_seq != 0:
                    differ.append(tier)
                continue
            expected = LayerRow(
                doc,
                tier,
                seq,
                schema.version,
                schema.rules_fingerprint,
                epoch,
                encode_layer(state.layers.get(tier, {})),
            )
            wanted.append(expected)
            if row is None or (row.schema_version, row.rules_fingerprint, row.epoch) != (
                expected.schema_version,
                expected.rules_fingerprint,
                expected.epoch,
            ):
                stale.append(tier)
            elif row.tier_seq < seq:
                stale.append(tier)
            elif row != expected:
                differ.append(tier)
        return wanted, differ, stale

    def _engine(self, schema: Schema) -> Prepared:
        with self._states_lock:
            ready = self._prepared.get((schema.id, schema.version))
            if ready is None or ready.ctx.schema is not schema:
                ready = self._prepared[(schema.id, schema.version)] = prepare(schema, self.behaviors)
            return ready

    def _load(
        self, tx: Tx, schema: Schema, doc: str, last: int, effects_seq: int = 0
    ) -> tuple[DerivationState | None, list[Entry]]:
        """The kept state if it fits this schema and this effect log, and the entries it has not folded in yet."""
        epoch = tx.epoch(doc)
        with self._states_lock:
            state = self._states.get(doc)
        if state is not None and (
            state.schema_ref is not schema
            or state.watermark > last
            or state.effects_seq != effects_seq
            or state.epoch != epoch  # a compaction or purge since: the entries it folded are no longer the ledger's
        ):
            state = (
                None  # built under other rules or an older log (or ahead of the ledger): start again from the ledger
            )
        if state is None:
            state = self._saved_state(tx, schema, doc, last, effects_seq)  # a cold process: the saved layers
        entries = self._entries(tx, doc, schema, state.watermark if state else 0)
        if state is not None and any(e.schema_version != schema.version for e in entries):
            state, entries = (
                None,
                self._entries(tx, doc, schema),
            )  # written under other rules: attribute as a replay of the ledger does
        return state, entries

    def _saved_state(self, tx: Tx, schema: Schema, doc: str, last: int, effects_seq: int) -> DerivationState | None:
        """The state the saved layers and the stored view stand for, if they fit the ledger (research R-3), else None.

        A layer that is missing, lagging, damaged or from other rules is simply not used; the caller replays the ledger.
        """
        if not self.layers:
            return None
        view = tx.get_view(doc)
        epoch = tx.epoch(doc)
        if (
            view is None
            or view.watermark_seq > last
            or view.schema_version != schema.version
            or view.rules_fingerprint != schema.rules_fingerprint
            or view.effects_seq != effects_seq
            or view.epoch != epoch
        ):
            return None
        rows = {row.tier: row for row in tx.get_layers(doc)}
        tier_seqs = {tier: tx.tier_last_seq(doc, tier, view.watermark_seq) for tier in schema.tiers}
        if not layers_fit(rows, tier_seqs, schema, epoch):
            return None
        try:
            notes = parse(view.provenance)
            return state_from_layers(
                schema,
                view.watermark_seq,
                {tier: row.state for tier, row in rows.items()},
                skipped=tuple(notes.get("skipped", ())),
                errors=_decode_errors(view.model_errors),
                conflicts=tuple(notes.get("conflicts", ())),
                effects_seq=view.effects_seq,
                epoch=epoch,
            )
        except (IonError, ValueError, AttributeError, KeyError, TypeError):
            return None  # damaged: ignored, and replaced by the next write

    def _layer_rows(
        self, tx: Tx, doc: str, schema: Schema, state: DerivationState, touched: set[str]
    ) -> list[LayerRow]:
        """The layers to save for `state`: those of the tiers a request touched, and any that are missing or stale."""
        epoch = tx.epoch(doc)
        existing = {row.tier: row for row in tx.get_layers(doc)}
        rows: list[LayerRow] = []
        for tier in schema.tiers:
            seq = tx.tier_last_seq(doc, tier, state.watermark)
            if seq == 0:
                continue
            saved = existing.get(tier)
            current = (
                saved is not None
                and saved.tier_seq == seq
                and saved.schema_version == schema.version
                and saved.rules_fingerprint == schema.rules_fingerprint
                and saved.epoch == epoch
            )
            if current and tier not in touched:
                continue
            text = encode_layer(state.layers.get(tier, {}))
            rows.append(LayerRow(doc, tier, seq, schema.version, schema.rules_fingerprint, epoch, text))
        return rows

    def _save_layers(self, tx: Tx, doc: str, schema: Schema, state: DerivationState, touched: set[str]) -> None:
        """Save layers in the transaction that commits the request (research R-1, R-3).

        Best effort: layers are a cache, so a failure here is logged and never fails or rejects the request.
        """
        if not self.layers:
            return
        try:
            rows = self._layer_rows(tx, doc, schema, state, touched)
            if rows:
                tx.put_layers(rows)
        except Exception as error:  # noqa: BLE001 - a cache: whatever went wrong, the request stands
            emit("layers_not_saved", doc=doc, error=type(error).__name__)

    def _replay(
        self,
        schema: Schema,
        entries: list[Entry],
        capture: str | None = None,
        effects: EffectView | None = None,
        bases: dict[str, Base] | None = None,
        trail: dict[str, tuple[tuple[ModelError, ...], tuple[ModelError, ...]]] | None = None,
    ) -> Replay:
        known = {**self._bases_for(entries), **(bases or {})}
        return replay(schema, self.schemas.get, self.behaviors, self._engine, entries, capture, effects, known, trail)

    def _bases_for(self, entries: Sequence[Entry]) -> dict[str, Base]:
        """For the compactions whose synthetic entries are among `entries`: what their replay had computed (feature 005)."""
        wanted = {e.synthetic: e.doc for e in entries if e.synthetic is not None}
        with self._states_lock:
            missing = [cid for cid in wanted if cid not in self._bases]
        if missing:
            with self.storage.read() as tx:
                for doc in sorted({wanted[cid] for cid in missing}):
                    for record in tx.compactions(doc):
                        base = Base(_decode_errors(record.base_errors), record.base_skipped)
                        with self._states_lock:
                            self._bases.setdefault(record.id, base)
        with self._states_lock:
            return {cid: self._bases[cid] for cid in wanted if cid in self._bases}

    def _entries(self, tx: Tx, doc: str, schema: Schema, since_seq: int = 0) -> list[Entry]:
        """The entries a derivation under `schema` folds: the live ledger, or, if the document was compacted under rules
        that fold differently, the originals restored from the archive (feature 005, research R-10)."""
        if tx.epoch(doc) == 0:
            return tx.entries(doc, since_seq)
        records = tx.compactions(doc)
        if not records or all(r.fold_fingerprint == schema.fold_fingerprint for r in records):
            return tx.entries(doc, since_seq)
        restored = self._restore(tx, doc, tx.entries(doc, 0), strict=True)[0]
        return [e for e in restored if e.seq > since_seq]

    def _restore(self, tx: Tx, doc: str, entries: list[Entry], strict: bool) -> tuple[list[Entry], set[str]]:
        """`entries` with every synthetic entry replaced by the originals the archive keeps (recursively).

        A synthetic entry whose archive was expired stays as it is, or, with `strict`, raises HistoryCompactedError. The
        second result is the ids of the entries that came out of the archive."""
        archived: set[str] = set()

        def expand(entry: Entry) -> list[Entry]:
            if entry.synthetic is None:
                return [entry]
            originals = [o for o in tx.archive_of(entry.synthetic) if o.tier == entry.tier]  # this tier's sources
            if not originals:  # expired
                if strict:
                    watermark = max((c.watermark_seq for c in tx.compactions(doc)), default=entry.seq)
                    raise HistoryCompactedError(
                        f"the history of {doc!r} up to seq {watermark} was compacted and its originals are no longer kept",
                        earliest_seq=watermark,
                    )
                return [entry]
            found = [piece for original in originals for piece in expand(original)]
            archived.update(piece.id for piece in found)
            return found

        restored = [piece for entry in entries for piece in expand(entry)]
        return sorted(restored, key=lambda e: e.seq), archived

    def _forget(self, doc: str) -> None:
        """Drop what this process kept in memory about a document (after the stored ledger was rewritten)."""
        with self._states_lock:
            self._states.pop(doc, None)
            self._views.pop(doc, None)
            for key in [k for k in self._shadow_cache if k[0] == doc]:
                del self._shadow_cache[key]
            for key in [k for k in self._retried_effects if k[0] == doc]:
                del self._retried_effects[key]

    def _rebuild(
        self, schema: Schema, state: DerivationState | None, entries: list[Entry], effects: EffectView | None = None
    ) -> Extension:
        """The document, with its errors, from a kept state plus new entries, or from the whole ledger."""
        if state is None:
            return self._replay(schema, entries, effects=effects).final
        return advance(schema, self.behaviors, self._engine(schema), state, entries, effects)

    def _catch_up(
        self,
        schema: Schema,
        state: DerivationState | None,
        entries: list[Entry],
        effects: EffectView | None = None,
    ) -> DerivationState:
        if state is None:
            return self._replay(schema, entries, effects=effects).final.state
        return catch_up(schema, self.behaviors, self._engine(schema), state, entries, effects)

    # -- impure computes: the effect log and the hooks -------------------------------------------------------------

    @staticmethod
    def _effect_view(tx: Tx, ready: Prepared, doc: str) -> EffectView | None:
        """The outcomes in force for a document, or None when its schema has no impure fields."""
        return view_from_records(tx.latest_effects(doc)) if ready.has_impure else None

    def _call_hooks(
        self, schema: Schema, doc: str, calls: Sequence[PendingCall], shared: bool = True
    ) -> list[EffectRecord]:
        """Call the hooks a derivation needs. Each result, a value or a failure, becomes a record; nothing is stored here.

        Callers that need the same outcome at the same moment share one call (`shared`); a `recompute` never does.
        """
        fields = {spec.pattern: spec for spec in schema.fields if spec.impure is not None}
        records: list[EffectRecord] = []
        for call in calls:
            if shared:
                records.append(self._one_flight(doc, call, partial(self._call_one, schema, fields, doc, call)))
            else:
                records.append(self._call_one(schema, fields, doc, call))
        return records

    def _one_flight(self, doc: str, call: PendingCall, produce: Callable[[], EffectRecord]) -> EffectRecord:
        key = (doc, call.field, call.fingerprint, call.hook)
        with self._states_lock:
            flight = self._flights.get(key)
            leading = flight is None
            if flight is None:
                flight = self._flights[key] = _Flight()
        if not leading:
            flight.done.wait()
            if flight.record is not None:
                return flight.record
            return produce()  # the leader failed to produce a record: do it ourselves
        try:
            flight.record = produce()
            return flight.record
        finally:
            flight.done.set()
            with self._states_lock:
                self._flights.pop(key, None)

    def _call_one(self, schema: Schema, fields: dict[str, Any], doc: str, call: PendingCall) -> EffectRecord:
        spec = fields[call.field]
        impure = spec.impure
        assert impure is not None
        behavior = self.behaviors.get(impure.hook, impure.version)
        assert behavior is not None and behavior.kind == "hook"  # checked when the schema was loaded
        result = self._hooks.run(behavior.impl, impure.policy, dict(call.inputs))
        value, error = result.value, result.error
        if result.outcome == "value" and spec.type is not None and not conforms(spec.type, value):
            value, error = None, "bad_value"  # the hook answered with a value of the wrong type
        succeeded = error is None and result.outcome == "value"
        emit(
            "hook_call",
            doc=doc,
            field=call.field,
            hook=call.hook,
            attempts=result.attempts,
            result="value" if succeeded else "failure",
        )
        return EffectRecord(
            doc=doc,
            field=call.field,
            fingerprint=call.fingerprint,
            hook=call.hook,
            outcome="value" if succeeded else "failure",
            value=to_canonical(normalize(value)) if succeeded else None,
            error=error,
            attempts=result.attempts,
            recorded_at=self.clock.now(),
        )

    def _fill(
        self,
        schema: Schema,
        doc: str,
        ext: Extension,
        effects: EffectView,
        derive: Callable[[EffectView], Extension],
    ) -> tuple[Extension, EffectView]:
        """For a read: call the hooks a derivation still needs, record their outcomes, and derive again until none is.

        The calls happen outside every transaction; the outcomes are stored in a short write transaction of their own.
        """
        for _ in range(sum(1 for spec in schema.fields if spec.impure is not None) + 1):
            if not ext.pending:
                break
            records = self._call_hooks(schema, doc, ext.pending)
            with self.storage.transaction() as tx:
                known = view_from_records(tx.latest_effects(doc))  # a caller that shared the call may have stored it
                new = [r for r in records if known.lookup(r.field, r.fingerprint, r.hook) is None]
                numbers = tx.append_effects(new)
            stored = [replace(record, effect_seq=number) for record, number in zip(new, numbers, strict=True)]
            effects = effects.with_outcomes(known).with_outcomes(view_from_records(stored))
            ext = derive(effects)
        return ext, effects

    def _outcomes_for(
        self, schema: Schema, doc: str, entries: list[EntryInput], contributor: Identity, lenient: bool
    ) -> list[EffectRecord]:
        """Before the write lock is taken: derive what the request would produce and call the hooks it needs.

        Returns the outcomes (stored only if the request is accepted). A request that fails the schema checks calls no
        hook. Any problem here is left for the transaction to report, with the outcomes found so far.
        """
        ready = self._engine(schema)
        if not ready.has_impure:
            return []
        fresh: list[EffectRecord] = []
        try:
            with self.storage.read() as tx:
                last = tx.last_seq(doc)
                effects = self._effect_view(tx, ready, doc) or EffectView()
                state, behind = self._load(tx, schema, doc, last, effects.seq)
            if check_request(schema, entries, contributor, lenient, self.behaviors, ctx=ready.ctx):
                return []
            now = self.clock.now()
            provisional = [
                Entry(
                    id=f"provisional-{offset}",
                    doc=doc,
                    seq=last + 1 + offset,
                    request_id="provisional",
                    tier=entry.tier,
                    path=entry.path,
                    op=entry.op,
                    value=TOMBSTONE if entry.op == "delete" else normalize(entry.value),
                    contributor=contributor.id,
                    tx_time=now,
                    schema_version=schema.version,
                    lenient=lenient,
                )
                for offset, entry in enumerate(entries)
            ]
            base = self._catch_up(schema, state, behind, effects)
            ext = extend(
                schema, self.behaviors, ready, base, provisional, Origin(request_id="provisional"), base.errors, effects
            )
            for _ in range(sum(1 for spec in schema.fields if spec.impure is not None) + 1):
                if not ext.pending:
                    break
                new = self._call_hooks(schema, doc, ext.pending)
                fresh.extend(new)
                effects = effects.with_outcomes(view_from_records(new))
                ext = extend(
                    schema,
                    self.behaviors,
                    ready,
                    base,
                    provisional,
                    Origin(request_id="provisional"),
                    base.errors,
                    effects,
                )
            return fresh
        except (ValidatorFault, ComputeFault, RejectedError, IonError):
            # Derive again in the transaction with what the hooks answered so far: it meets the same fault and rejects
            # with its own code. Dropping the answers would leave the request waiting for hooks it can never settle.
            return fresh

    def _warm(self, doc: str, schema: Schema, refresh: bool = False) -> None:
        """Make sure an in-memory state exists (and, if `refresh`, is current), folding outside every write transaction.

        Best effort: a ledger that cannot be folded (a validator faults, a schema version is gone) is left to the
        admission itself, which turns it into a rejection.
        """
        with self._states_lock:
            known = self._states.get(doc)
        if known is not None and known.schema_ref is schema and not refresh:
            return
        try:
            with self.storage.read() as tx:
                last = tx.last_seq(doc)
                effects = self._effect_view(tx, self._engine(schema), doc)
                state, entries = self._load(tx, schema, doc, last, effects.seq if effects else 0)
                epoch = tx.epoch(doc)
            if entries:
                self._remember(doc, replace(self._catch_up(schema, state, entries, effects), epoch=epoch))
        except (ValidatorFault, ComputeFault, RejectedError):
            return

    @staticmethod
    def _settle(document: FrozenStruct) -> tuple[FrozenStruct, str]:
        """The document as it reads back from its canonical text, and that text: fresh and cached views are one value."""
        text = to_canonical(document)
        return parse(text), text

    def _view_row(
        self, doc: str, last: int, schema: Schema, text: str, state: DerivationState, epoch: int = 0
    ) -> ViewRow:
        notes = FrozenStruct({"skipped": tuple(state.skipped), "conflicts": state.conflicts})
        return ViewRow(
            doc,
            last,
            schema.version,
            schema.rules_fingerprint,
            text,
            _encode_errors(state.errors),
            to_canonical(notes),
            state.effects_seq,
            epoch,
        )

    def _remember(self, doc: str, folded: DerivationState) -> None:
        with self._states_lock:
            kept = self._states.get(doc)
            if kept is None or kept.schema_ref is not folded.schema_ref or kept.watermark <= folded.watermark:
                self._states[doc] = folded
            self._states.move_to_end(doc)
            while len(self._states) > self.state_cache_size:
                self._states.popitem(last=False)

    # -- purge (feature 005) --------------------------------------------------------------------------------------------

    def purge(self, doc: str, scope: PurgeScope, *, reason: str, caller: Identity) -> PurgeReport:
        """Erase contributions for good (legal erasure) and record that it was done, without the values.

        Allowed only to a caller whose tier or role the schema lists in `purge_by` (nobody, by default), and only with a
        reason. The erased values, `meta` and `valid_time` go from the ledger, the archive, the compacted layers, the saved
        layers and views, the effect-log records and derived values downstream of the erased paths, and the values in the
        rejection audit log, in one transaction; the entries stay as placeholders so that seq numbers stay continuous.
        """
        schema = self.schemas.version_for(doc)
        ready = self._engine(schema)
        if not {*caller.tiers, *caller.roles} & set(schema.purge_by):
            raise RejectedError(ErrorCode.NOT_AUTHORIZED, message=f"{caller.id} may not purge documents of this schema")
        if not reason.strip():
            raise RejectedError(ErrorCode.PURGE_REASON_REQUIRED, message="a purge needs a reason or a ticket")
        if (scope.path is None) == (not scope.entries):
            raise ValueError("a purge scope names a path or entries, not both and not neither")
        if scope.tier is not None and scope.tier not in schema.tiers:
            raise RejectedError(
                ErrorCode.TIER_NOT_PERMITTED, message=f"tier {scope.tier!r} is not defined by the schema"
            )
        parsed: Any = None
        if scope.path is not None:
            try:
                parsed = parse_path(scope.path)
            except PathError as error:
                raise RejectedError(ErrorCode.UNKNOWN_PATH, message=f"invalid path: {error}") from error
            if schema.field_for(parsed) is None:
                raise RejectedError(ErrorCode.UNKNOWN_PATH, message="the schema does not declare this path")
            derived = schema.derived_field_at_or_above(parsed)
            if derived is not None:
                raise RejectedError(
                    ErrorCode.FIELD_NOT_WRITABLE,
                    message=f"{derived.pattern} is a {derived.derived} field: nothing was contributed there",
                )
        with self.storage.transaction() as tx:
            records = {r.id: r for r in tx.compactions(doc)}
            live = tx.entries(doc, 0)
            archive = [(cid, entry) for cid in records for entry in tx.archive_of(cid)]
            known = {e.id for e in live if e.synthetic is None} | {e.id for _, e in archive if e.synthetic is None}
            for entry_id in scope.entries:
                if entry_id not in known:
                    raise RejectedError(
                        ErrorCode.UNKNOWN_ENTRY,
                        message=f"no entry {entry_id!r} in {doc!r} (an entry whose originals were expired is purged by path)",
                    )
            changes = purge_engine.plan(
                live, archive, records, schema, self.behaviors, ready,
                path=parsed, tier=scope.tier, entry_ids=frozenset(scope.entries),
            )  # fmt: skip
            fields = purge_engine.downstream_fields(schema, [parse_path(p) for p in changes.paths])
            effects, derived_rows = tx.apply_purge(
                PurgeApplication(doc, changes.live, changes.archive, changes.requests, fields)
            )
            scrubbed = self._scrub_audit(doc, changes.paths)
            erased_ids = sorted(e.id for e in (*changes.live, *changes.archive) if e.purged)
            counts = FrozenStruct(
                {
                    "entries_erased": changes.erased,
                    "entries_rewritten": changes.rewritten,
                    "archive_rows": len(changes.archive),
                    "effects_erased": effects,
                    "derived_erased": derived_rows,
                    "audit_scrubbed": scrubbed,
                    "expired_sources": changes.expired_sources,
                    "ids": tuple(erased_ids),
                }
            )
            record = PurgeRecord(
                self.ids.new_id(), doc, to_canonical(_scope_text(scope)), reason.strip(), caller.id, self.clock.now(),
                to_canonical(counts),
            )  # fmt: skip
            tx.append_purge_log(record)
        self._forget(doc)
        emit(
            "purge",
            doc=doc,
            purge=record.id,
            actor=caller.id,
            entries_erased=changes.erased,
            entries_rewritten=changes.rewritten,
            archive_rows=len(changes.archive),
            effects_erased=effects,
            derived_erased=derived_rows,
            audit_scrubbed=scrubbed,
        )
        return PurgeReport(
            record.id, doc, changes.erased, changes.rewritten, len(changes.archive), effects, derived_rows, scrubbed,
            changes.expired_sources,
        )  # fmt: skip

    def purge_log(self, doc: str) -> list[PurgeRecord]:
        """The record of every purge of a document, oldest first (never a value)."""
        with self.storage.read() as tx:
            return tx.purge_log(doc)

    def _scrub_audit(self, doc: str, paths: Sequence[str]) -> int:
        """Take the attempted values out of the rejection audit records that touch an erased path."""
        erased = [parse_path(p) for p in paths]
        backend = self.storage.audit_backend()
        changed = 0
        for row in backend.rows(doc):
            attempts = list(parse(row.attempted))
            scrubbed: list[Any] = []
            touched = False
            for item in attempts:
                try:
                    where = parse_path(str(item.get("path", "")))
                except PathError:
                    scrubbed.append(item)
                    continue
                if any(where[: len(p)] == p or p[: len(where)] == where for p in erased) and (
                    "value" in item or "meta" in item
                ):
                    item = FrozenStruct(
                        {k: v for k, v in item.items() if k not in ("value", "meta")} | {"purged": True}
                    )
                    touched = True
                scrubbed.append(item)
            if touched and row.id is not None:
                backend.update_attempted(row.id, to_canonical(tuple(scrubbed)))
                changed += 1
        return changed

    # -- compaction, archive and expiry (feature 005) ------------------------------------------------------------------

    def compact(
        self,
        doc: str,
        *,
        through_seq: int | None = None,
        older_than: timedelta | None = None,
        verify: bool = True,
    ) -> CompactionReport:
        """Replace the entries up to a request boundary by one synthetic entry per tier, keeping the originals in the archive.

        Before anything is written the document, its model errors (with who introduced each), its skipped and conflicting
        paths and the `explain` answer of every path the range touched are derived from the compacted ledger and compared
        with those of the ledger as it is; any difference refuses with COMPACTION_CHECK_FAILED and changes nothing. Entries
        recorded meanwhile are kept, in order, after the watermark (LEDGER_CHANGED if the range itself changed).
        """
        if (through_seq is None) == (older_than is None):
            raise ValueError("give exactly one of through_seq and older_than")
        schema = self.schemas.version_for(doc)
        ready = self._engine(schema)
        with self.storage.read() as tx:
            epoch0 = tx.epoch(doc)
            entries = tx.entries(doc, 0)
            records = tx.compactions(doc)
            effects = self._effect_view(tx, ready, doc)
        nothing = CompactionReport(doc, None, 0, 0, 0, 0, noop=True)
        if not entries:
            return nothing
        if any(r.fold_fingerprint != schema.fold_fingerprint for r in records):
            raise HistoryCompactedError(
                f"{doc!r} was compacted under rules that fold differently from version {schema.version}"
            )
        if older_than is not None:
            cutoff = self.clock.now() - older_than
            bound = 0
            for entry in entries:
                if entry.tx_time >= cutoff:
                    break
                bound = entry.seq
            policy = f"older_than={older_than.total_seconds():g}s"
        else:
            bound = int(through_seq or 0)
            policy = f"through_seq={bound}"
        watermark = compaction_engine.watermark_for(entries, bound)
        if watermark == 0:
            return nothing
        head = [e for e in entries if e.seq <= watermark]
        trail: dict[str, tuple[tuple[ModelError, ...], tuple[ModelError, ...]]] = {}
        head_ext = self._replay(schema, head, effects=effects, trail=trail).final
        base = Base(head_ext.errors, head_ext.state.skipped)
        compaction_id = self.ids.new_id()
        earlier = {
            r.id: compaction_engine.EarlierCompaction(r.first_tx_time, r.last_tx_time, r.has_valid_time)
            for r in records
        }
        result = compaction_engine.plan(
            entries,
            schema,
            self.behaviors,
            ready,
            doc=doc,
            through_seq=bound,
            compaction_id=compaction_id,
            new_id=self.ids.new_id,
            created=self.clock.now(),
            policy=policy,
            base_errors=_encode_errors(base.errors),
            base_skipped=base.skipped,
            earlier=earlier,
        )
        if result is None:
            return nothing
        effects_of = []
        for request_id in dict.fromkeys(row[0] for row in result.request_index):
            was, now = trail.get(request_id, ((), ()))
            kept, found = {e.identity for e in was}, {e.identity for e in now}
            effects_of.append(
                (
                    request_id,
                    _encode_errors(tuple(e for e in now if e.identity not in kept)),
                    _encode_errors(tuple(e for e in was if e.identity not in found)),
                )
            )
        result = replace(result, request_effects=tuple(effects_of))
        after_entries = compaction_engine.compacted_ledger(result, entries)
        recorded = Base(_decode_errors(result.record.base_errors), result.record.base_skipped)  # as it will be stored
        after_ext = self._replay(schema, after_entries, effects=effects, bases={compaction_id: recorded}).final
        checked = 0
        if verify:
            before_ext = self._replay(schema, entries, effects=effects).final
            paths = self._touched_paths(schema, result.replaced)
            found_differences = compaction_engine.shape_problems(result) + compaction_engine.differences(
                self._observe(schema, ready, before_ext, entries, paths, effects),
                self._observe(schema, ready, after_ext, after_entries, paths, effects),
                compaction_engine.id_map(result),
            )
            if found_differences:
                raise RejectedError(
                    ErrorCode.COMPACTION_CHECK_FAILED,
                    [Reason(ErrorCode.COMPACTION_CHECK_FAILED, text) for text in found_differences],
                )
            checked = len(paths)
        try:
            with self.storage.transaction() as tx:
                same = [(e.id, e.seq) for e in tx.entries(doc, 0, until_seq=watermark)] == [(e.id, e.seq) for e in head]
                outcomes_moved = effects is not None and tx.effects_seq(doc) != effects.seq
                if tx.epoch(doc) != epoch0 or not same or outcomes_moved:
                    raise RejectedError(
                        ErrorCode.LEDGER_CHANGED, message="the ledger changed while the check ran; try again"
                    )
                tx.apply_compaction(result)
                tx.drop_layers(doc)
                if after_ext.pending:  # impure fields still waiting for an outcome: leave the view to the next read
                    tx.drop_view(doc)
                else:
                    epoch = tx.epoch(doc)
                    state = replace(after_ext.state, epoch=epoch)
                    _, text = self._settle(after_ext.document)
                    tx.put_view(self._view_row(doc, state.watermark, schema, text, state, epoch))
                    self._save_layers(tx, doc, schema, state, set(schema.tiers))
        except ConstraintError as error:
            raise RejectedError(
                ErrorCode.LEDGER_CHANGED, message=f"the ledger changed during the compaction: {error}"
            ) from error
        with self._states_lock:
            self._bases[compaction_id] = recorded
        self._forget(doc)
        emit(
            "compaction",
            doc=doc,
            compaction=compaction_id,
            watermark=watermark,
            replaced=len(result.replaced),
            synthetic=len(result.synthetic),
        )
        return CompactionReport(doc, compaction_id, watermark, len(result.replaced), len(result.synthetic), checked)

    def _touched_paths(self, schema: Schema, replaced: Sequence[Entry]) -> list[str]:
        """The declared, contributed paths a range of entries wrote (what `explain` is compared on)."""
        ready = self._engine(schema)
        found: set[str] = set()
        for entry in replaced:
            if entry.purged or entry.tier not in schema.tiers:
                continue
            try:
                if entry.op == "layer":
                    written = list(decode_layer(entry.value))
                elif entry.op == "set":
                    written = list(spread(ready.ctx, parse_path(entry.path), entry.value))
                else:
                    written = [parse_path(entry.path)]
            except (PathError, IonError):
                continue
            for path in written:
                if schema.field_for(path) is not None and schema.derived_field_at_or_above(path) is None:
                    found.add(format_path(path))
        return sorted(found)

    def _observe(
        self,
        schema: Schema,
        ready: Prepared,
        ext: Extension,
        entries: list[Entry],
        paths: Sequence[str],
        effects: EffectView | None,
    ) -> compaction_engine.Observed:
        explained = {
            path: explain_path(schema, self.behaviors, ready, entries, path, ext.errors, effects) for path in paths
        }
        return compaction_engine.Observed(
            to_canonical(ext.document), ext.errors, ext.state.skipped, ext.conflicts, explained
        )

    def verify_archive(self, doc: str) -> ArchiveReport:
        """Derive the document from the archived originals and from the compacted ledger, and say where they differ.

        Not checked (checked=False) when the originals are no longer all kept, or the rules fold differently now."""
        schema = self.schemas.version_for(doc)
        ready = self._engine(schema)
        with self.storage.read() as tx:
            records = tx.compactions(doc)
            entries = tx.entries(doc, 0)
            effects = self._effect_view(tx, ready, doc)
            if not records or any(r.fold_fingerprint != schema.fold_fingerprint for r in records):
                return ArchiveReport(doc, False)
            try:
                originals, _ = self._restore(tx, doc, entries, strict=True)
            except HistoryCompactedError:
                return ArchiveReport(doc, False)
        compacted = self._replay(schema, entries, effects=effects).final
        full = self._replay(schema, originals, effects=effects).final
        return ArchiveReport(
            doc, True, tuple(compaction_engine.differences(self._observe_all(full), self._observe_all(compacted), {}))
        )

    @staticmethod
    def _observe_all(ext: Extension) -> compaction_engine.Observed:
        return compaction_engine.Observed(to_canonical(ext.document), ext.errors, ext.state.skipped, ext.conflicts)

    def expire_archive(self, doc: str, *, through_seq: int) -> ExpiryReport:
        """Delete the archived originals of the compactions with a watermark at or below `through_seq` (a recorded act).

        The synthetic entries, their source ids, the request index and the retired idempotency keys stay."""
        with self.storage.transaction() as tx:
            removed = tx.expire_archive(doc, through_seq, self.clock.now())
        emit("archive_expired", doc=doc, through_seq=through_seq, expired=removed)
        return ExpiryReport(doc, through_seq, removed)

    def _compaction_problem(self, schema: Schema, doc: str | None) -> str | None:
        """Why a schema version may not serve a document (or any): its compacted history cannot be re-folded under it."""
        with self.storage.read() as tx:
            for name in [doc] if doc is not None else tx.compacted_docs():
                if tx.epoch(name) == 0:
                    continue
                records = tx.compactions(name)
                if any(r.fold_fingerprint != schema.fold_fingerprint for r in records) and any(
                    not r.archived for r in records
                ):
                    return (
                        f"HISTORY_COMPACTED: version {schema.version} folds differently from the rules {name!r} was "
                        "compacted under, and the originals are no longer kept"
                    )
        return None

    def list_entries(self, doc: str, *, since_seq: int = 0, limit: int | None = None) -> list[Entry]:
        with self.storage.read() as tx:
            return tx.entries(doc, since_seq, limit)

    def last_seq(self, doc: str) -> int:
        with self.storage.read() as tx:
            return tx.last_seq(doc)

    # -- deferred fields (feature 003) ---------------------------------------------------------------------------

    def doc_ids(self) -> list[str]:
        with self.storage.read() as tx:
            return tx.doc_ids()

    def _core_of(self, doc: str) -> tuple[Schema, DocumentResult]:
        schema = self.schemas.version_for(doc)
        return schema, self._core(doc, schema)

    def invalidate(self, doc: str | None, field: str, *, caller: Identity) -> int:
        """Mark the stored value of a deferred field out of date, for one document or (`doc=None`) for every document
        that holds one. Nothing is called: the next fetch under the field's policy recomputes it, and a successful call
        clears the mark. Only the tiers and roles the field lists in `invalidate_by` may; others get NOT_AUTHORIZED."""
        schema = self.schemas.version_for(doc) if doc is not None else self.schemas.active()
        spec = next((s for s in schema.fields if s.pattern == field and s.deferred is not None), None)
        if spec is None or spec.deferred is None:
            raise RejectedError(ErrorCode.UNKNOWN_PATH, message=f"{field} is not a deferred field of the schema")
        if not ({*caller.tiers, *caller.roles} & set(spec.deferred.invalidate_by)):
            raise RejectedError(ErrorCode.NOT_AUTHORIZED, message=f"{caller.id} may not invalidate {field}")
        with self.storage.transaction() as tx:
            marked = tx.mark_invalidated([field], [doc] if doc is not None else None)
        emit("invalidate", caller=caller.id, doc=doc, field=field, marked=marked)
        return marked

    def list_deferred(self, doc: str) -> list[DeferredInfo]:
        """The stored state of each deferred field of a document: metadata only, never a value."""
        with self.storage.read() as tx:
            records = tx.get_derived(doc)
        return [DeferredInfo(r.field, r.status, r.as_of, r.attempts, r.invalidated) for r in records]

    def notify(self, topic: str, docs: Sequence[str] | None = None) -> int:
        """Mark out of date the stored values of the fields linked to `topic`, for the named documents or, with none
        named, for every document that holds one. No source is called: the next `scheduler_run` recomputes them. Safe to
        repeat; returns how many stored values match. Delivering events is the deployment's job, so this is not gated."""
        fields = [
            spec.pattern
            for spec in self.schemas.active().fields
            if spec.deferred is not None
            and any(p.kind == "notification" and p.topic == topic for p in spec.deferred.refresh)
        ]
        if not fields or (docs is not None and not docs):
            return 0
        with self.storage.transaction() as tx:
            marked = tx.mark_invalidated(fields, list(docs) if docs is not None else None)
        emit("notification", topic=topic, documents=len(docs) if docs is not None else None, marked=marked)
        return marked

    def scheduler_run(self, hook: str, *, caller: Identity) -> SchedulerReport:
        """Refresh what is due for a source (`"name@version"`), across all documents, in batches, within its rate limit.

        Only the fields the caller may invalidate (`invalidate_by`) are refreshed: a caller who may refresh none is
        refused (NOT_AUTHORIZED), and a source that no deferred field of the active schema uses is UNKNOWN_PATH. A run
        that finds another run of the same source going reports `reason="lease held"` and calls nothing.
        """
        held = {*caller.tiers, *caller.roles}

        def permitted(spec: FieldSpec) -> bool:
            return spec.deferred is not None and bool(held & set(spec.deferred.invalidate_by))

        serving = [s for s in self.schemas.active().fields if s.deferred is not None and s.deferred.source == hook]
        if not serving:
            raise RejectedError(ErrorCode.UNKNOWN_PATH, message=f"no deferred field uses the source {hook}")
        if not any(permitted(spec) for spec in serving):
            raise RejectedError(ErrorCode.NOT_AUTHORIZED, message=f"{caller.id} may not refresh the source {hook}")
        return self._scheduler.run(hook, permitted)

    # -- effect log admin ----------------------------------------------------------------------------------------

    def list_effects(self, doc: str, field: str | None = None, *, limit: int | None = None) -> list[EffectInfo]:
        """The outcomes of impure computes recorded for a document, oldest first. Metadata only: no value, no error text."""
        with self.storage.read() as tx:
            records = tx.list_effects(doc, field, limit)
        return [
            EffectInfo(r.effect_seq or 0, r.doc, r.field, r.hook, r.outcome, r.attempts, r.recorded_at) for r in records
        ]

    def recompute(self, doc: str, field: str | None = None, *, caller: Identity) -> RecomputeReport:
        """Call impure hooks again with the current inputs and record the outcomes, which supersede the earlier ones.

        Only the tiers and roles a field lists in `recompute_by` may recompute it: naming a field the caller may not
        recompute is refused (NOT_AUTHORIZED); without a field, only the permitted ones are recomputed and the rest are
        reported as skipped. A successful outcome removes the failure's model error on the next derivation.
        """
        schema = self.schemas.version_for(doc)
        ready = self._engine(schema)
        impure_fields = [spec for spec in ready.computed if spec.impure is not None]
        if field is not None:
            chosen = [spec for spec in impure_fields if spec.pattern == field]
            if not chosen:
                raise RejectedError(ErrorCode.UNKNOWN_PATH, message=f"{field} is not an impure computed field")
        else:
            chosen = impure_fields
        held = {*caller.tiers, *caller.roles}
        permitted = [spec for spec in chosen if held & set(spec.impure.recompute_by if spec.impure else ())]
        if field is not None and not permitted:
            raise RejectedError(ErrorCode.NOT_AUTHORIZED, message=f"{caller.id} may not recompute {field}")
        skipped = tuple(sorted(spec.pattern for spec in chosen if spec not in permitted))
        with self.storage.read() as tx:
            entries = self._entries(tx, doc, schema)
            effects = self._effect_view(tx, ready, doc)
        document = self._replay(schema, entries, effects=effects).final.document
        results = compute_fields(schema, self.behaviors, document, ready.computed, effects).results
        failed: list[tuple[str, str]] = []
        calls: list[PendingCall] = []
        for spec in sorted(permitted, key=lambda s: s.pattern):
            current = results.get(spec.pattern)
            if current is None or any(v is ABSENT or v is None for _, v in current.dependencies):
                failed.append((spec.pattern, "missing_input"))
            else:
                hook = current.source
                calls.append(
                    PendingCall(spec.pattern, hook, current.dependencies, fingerprint(hook, current.dependencies))
                )
        records = self._call_hooks(schema, doc, calls, shared=False)  # an explicit new call, never a shared one
        numbers: list[int] = []
        if records:
            with self.storage.transaction() as tx:
                numbers = tx.append_effects(records)
        recomputed: list[str] = []
        effect_seqs: list[tuple[str, int]] = []
        for record, number in zip(records, numbers, strict=True):
            if record.outcome == "value":
                recomputed.append(record.field)
                effect_seqs.append((record.field, number))
            else:
                failed.append((record.field, record.error or "error"))
        emit(
            "recompute",
            doc=doc,
            caller=caller.id,
            recomputed=recomputed,
            failed=[name for name, _ in sorted(failed)],
            skipped=list(skipped),
        )
        return RecomputeReport(tuple(recomputed), skipped, tuple(sorted(failed)), tuple(effect_seqs))

    def prune_effects(self, doc: str | None = None) -> int:
        """Remove superseded outcomes beyond the newest 5 per key and those older than 90 days. Nothing derived changes."""
        with self.storage.transaction() as tx:
            return tx.prune_effects(doc, self.clock.now())

    # -- rejection audit admin -----------------------------------------------------------------------------------

    def list_rejections(
        self, doc: str, *, since: datetime | None = None, limit: int | None = None
    ) -> list[AuditRecord]:
        return self._audit.list_records(doc, since=since, limit=limit)

    # -- schema versions (feature 006) -----------------------------------------------------------------------------

    @staticmethod
    def _operator(operator: str) -> str:
        if not isinstance(operator, str) or not operator.strip():
            raise ValueError("an operator name is required to change which schema version serves")
        return operator

    def activate_schema(
        self,
        version: int,
        *,
        operator: str,
        acknowledge_breaking: bool = False,
        rebuild: bool = False,
        expect_generation: int | None = None,
    ) -> ActivationReport:
        """Make `version` the active one for every process on the store, recording who did it and how risky it was.

        A breaking change needs `acknowledge_breaking` (a rollback needs none: it is allowed whenever the tier and compaction
        checks pass). Activating the active version changes and records nothing. No document is touched: each re-derives on its
        next read, unless `rebuild` asks for the saved layers and views of the unpinned documents to be refreshed now. The
        activation stands whatever the rebuild finds (a document it cannot derive is reported); undoing it is another
        activation. `expect_generation` refuses the change if the shared state moved since the caller looked at it.
        """
        self._operator(operator)
        previous = self.schemas.active_version()  # syncs with the shared state first
        seen = self.schemas.generation()  # the state this decision is based on: the write refuses if it moved since
        target = self.schemas.get(version)
        if previous == version:
            return ActivationReport(version=version, previous=version, change_class=None, rollback=False)
        rollback = previous is not None and version < previous
        changes = (
            schema_changes(self.schemas.get(previous), target)
            if previous is not None and previous in self.schemas.versions()
            else None
        )
        change_class = changes.overall if changes is not None else None
        if change_class == "breaking" and not acknowledge_breaking and not rollback and changes is not None:
            first = [f"{c.kind} {c.path}".strip() for c in changes.changes if c.change_class == "breaking"][:5]
            emit(
                "schema_activation_refused",
                operator=operator,
                previous=previous,
                version=version,
                code=ErrorCode.BREAKING_CHANGE_NOT_ACKNOWLEDGED.value,
                first=first,
                **{"class": change_class},
            )
            raise RejectedError(
                ErrorCode.BREAKING_CHANGE_NOT_ACKNOWLEDGED,
                message=(
                    f"version {version} is a breaking change from version {previous} ({', '.join(first)}); "
                    "acknowledge it to activate"
                ),
            )
        changed: tuple[str, ...] = ()
        if rebuild and previous is not None:  # which documents will read differently, worked out before the state moves
            changed = tuple(c.doc for c in self.plan_schema(version).documents if c.changed)
        try:
            self.schemas.activate(
                version,
                operator=operator,
                rollback=rollback,
                change_class=change_class,
                expect_generation=seen if expect_generation is None else expect_generation,
            )
        except (SchemaError, RejectedError) as refusal:
            emit(
                "schema_activation_refused",
                operator=operator,
                previous=previous,
                version=version,
                code=(
                    refusal.codes[0] if isinstance(refusal, SchemaError) and refusal.codes else type(refusal).__name__
                ),
                **{"class": change_class},
            )
            raise
        emit(
            "schema_activated",
            operator=operator,
            previous=previous,
            version=version,
            rollback=rollback,
            rebuild=rebuild,
            **{"class": change_class},
        )
        report = self._rebuild_unpinned() if rebuild else None
        return ActivationReport(
            version=version,
            previous=previous,
            change_class=change_class,
            rollback=rollback,
            rebuild=report,
            changed=changed,
        )

    def _rebuild_unpinned(self) -> RebuildReport:
        """Refresh the saved layers and views of every document that follows the active version."""
        pinned = self.schemas.pins()
        with self.storage.read() as tx:
            names = [name for name in tx.doc_ids() if name not in pinned]
        return self._rebuild_names(names)

    def schema_changes(self, old_version: int, new_version: int) -> ChangeList:
        """The changes between two registered versions, each with its class (no store is read)."""
        return schema_changes(self.schemas.get(old_version), self.schemas.get(new_version))

    def served_versions(self) -> dict[int, str]:
        """The full fingerprint of every version served so far, by number: what a restart must present again."""
        return self.schemas.served_fingerprints()

    def plan_schema(self, target: int, *, docs: Sequence[str] | None = None, limit: int | None = None) -> DryRunReport:
        """What activating `target` would change, document by document, without changing anything.

        Each document is derived under the version now active and under `target` over the same entries and the same recorded
        outcomes. Nothing is written and no hook or source is called: an impure field with no recorded outcome under `target`
        is only counted (`would_call_hook`). Every document is examined unless `docs` or `limit` narrows it; `limit` takes the
        first N by a hash of the document name, so the same N each time. Pinned documents are left out (an activation does not
        touch them). `examined` and `left_out` say how much was looked at. Values are never part of the report.
        """
        current = self.schemas.active()
        wanted = self.schemas.get(target)
        pinned = self.schemas.pins()
        generation = self.schemas.generation()
        with self.storage.read() as tx:
            stored = tx.doc_ids()
        candidates = list(dict.fromkeys(docs)) if docs is not None else stored
        candidates = sorted((d for d in candidates if d not in pinned), key=_by_hash)
        if limit is not None:
            candidates = candidates[: max(limit, 0)]
        changes: list[DocumentChange] = []
        for name in candidates:
            change = self._dry_run_one(name, current, wanted)
            if change.failed or change.changed or change.would_call_hook:
                changes.append(change)
        failed = sum(1 for c in changes if c.failed)
        changed = sum(1 for c in changes if c.changed)
        unchanged = len(candidates) - changed - failed
        hooks = sum(1 for c in changes if c.would_call_hook)
        examined = set(candidates)
        report = DryRunReport(
            change_list=schema_changes(current, wanted),
            target=target,
            generation=generation,
            examined=len(candidates),
            left_out=len([d for d in stored if d not in examined]),
            changed=changed,
            unchanged=unchanged,
            failed=failed,
            would_call_hook=hooks,
            documents=tuple(sorted(changes, key=lambda c: c.doc)),
        )
        emit(
            "schema_planned",
            target=target,
            generation=generation,
            examined=report.examined,
            left_out=report.left_out,
            changed=changed,
            unchanged=unchanged,
            failed=failed,
            would_call_hook=hooks,
        )
        return report

    def _dry_run_one(self, doc: str, current: Schema, wanted: Schema) -> DocumentChange:
        """One document under two versions over the same entries. A document that cannot be derived is `failed`."""
        try:
            with self.storage.read() as tx:
                entries = tx.entries(doc, 0)
                if tx.epoch(doc) != 0:
                    problem = self._compaction_problem(wanted, doc)
                    if problem is not None:
                        return dryrun.failed(doc, ErrorCode.HISTORY_COMPACTED.value)
                    entries, _ = self._restore(tx, doc, entries, strict=False)
                seen = {}
                for schema in (current, wanted):
                    seen[schema.version] = self._effect_view(tx, self._engine(schema), doc)
            before = self._replay(current, entries, effects=seen[current.version]).final
            after = self._replay(wanted, entries, effects=seen[wanted.version]).final
            ready = self._engine(wanted)
            return dryrun.compare(
                doc,
                self._observed(before),
                self._observed(after),
                shadow_reasons(wanted, self.behaviors, ready, entries, after.state.skipped),
                bool(after.pending),
            )
        except (RejectedError, SchemaError, StorageError, ComputeFault, ValidatorFault) as error:
            code = error.code.value if isinstance(error, RejectedError) else type(error).__name__
            return dryrun.failed(doc, code)

    def _observed(self, ext: Extension) -> compaction_engine.Observed:
        _, text = self._settle(ext.document)
        return compaction_engine.Observed(text, ext.errors, ext.state.skipped, ext.conflicts)

    def plan_migration(self, doc: str, to: int | None = None) -> DocumentChange:
        """What moving one pinned document to `to` (the active version by default) would change; writes nothing.

        Raises RejectedError(NOT_PINNED) for a document that is not pinned.
        """
        pinned = self.schemas.pins().get(doc)
        if pinned is None:
            raise RejectedError(ErrorCode.NOT_PINNED, message=f"document {doc!r} is not pinned to a version")
        target = self.schemas.get(to) if to is not None else self.schemas.active()
        return self._dry_run_one(doc, self.schemas.get(pinned), target)

    def migrate(
        self,
        docs: Sequence[str] | None = None,
        *,
        to: int | None = None,
        operator: str,
        batch: int = 100,
        progress: Callable[[MigrationProgress], None] | None = None,
    ) -> MigrationReport:
        """Move pinned documents to `to` (the active version by default), each in its own transaction with its record.

        Over `docs`, or over every pinned document when none are given. A document moved to the active version is unpinned.
        One that cannot be derived under the target is reported `failed` and stays on its version, and the rest go on. Run
        again after a stop, the documents already moved are reported and left alone. A document that is not pinned is reported
        `not_pinned`; asked about alone it is refused (NOT_PINNED). `progress` is called after each batch of `batch` documents.
        """
        self._operator(operator)
        if batch < 1:
            raise ValueError("a batch holds at least one document")
        target = self.schemas.get(to) if to is not None else self.schemas.active()
        names = list(dict.fromkeys(docs)) if docs is not None else sorted(self.schemas.pins())
        if docs is not None and len(names) == 1 and not self._migrated_to(names[0], target.version):
            if names[0] not in self.schemas.pins():
                raise RejectedError(ErrorCode.NOT_PINNED, message=f"document {names[0]!r} is not pinned to a version")
        moved: list[str] = []
        already: list[str] = []
        not_pinned: list[str] = []
        failed: list[tuple[str, str]] = []
        batches = 0
        for start in range(0, len(names), batch):
            chunk = names[start : start + batch]
            pins = self.schemas.pins()  # read once per batch, not once per document
            active = self.schemas.active_version()
            for name in chunk:
                outcome = self._migrate_one(name, target, operator, pins, active)
                if outcome == "moved":
                    moved.append(name)
                elif outcome == "already_moved":
                    already.append(name)
                elif outcome == "not_pinned":
                    not_pinned.append(name)
                else:
                    failed.append((name, outcome))
            batches += 1
            emit(
                "schema_migration_batch",
                operator=operator,
                target=target.version,
                batch=batches,
                moved=len(moved),
                already_moved=len(already),
                not_pinned=len(not_pinned),
                failed=len(failed),
            )
            if progress is not None:
                progress(
                    MigrationProgress(
                        batches, start + len(chunk), len(names), len(moved), len(already), len(not_pinned), len(failed)
                    )
                )
        return MigrationReport(tuple(moved), tuple(already), tuple(not_pinned), tuple(failed), batches)

    def _migrated_to(self, doc: str, version: int) -> bool:
        """True if the last recorded act on the document was a migration to `version`."""
        last = self.schemas.history(limit=1, doc=doc)
        return bool(last) and last[0].action == "migrate" and last[0].to_version == version

    def _migrate_one(self, doc: str, target: Schema, operator: str, pins: dict[str, int], active: int | None) -> str:
        """`moved`, `already_moved`, `not_pinned`, or the code of the failure; a failure leaves the document as it was.

        `pins` and `active` are what the batch read when it began; a move updates `pins`."""
        pinned = pins.get(doc)
        if pinned is None:
            return "already_moved" if self._migrated_to(doc, target.version) else "not_pinned"
        if pinned == target.version:
            return "already_moved"
        current = self.schemas.get(pinned)
        change = self._dry_run_one(doc, current, target)
        if change.failed:
            emit("schema_migration_failed", operator=operator, doc=doc, target=target.version, code=change.failure_code)
            return change.failure_code or "FAILED"
        try:
            self.schemas.pin(
                doc,
                None if target.version == active else target.version,
                operator=operator,
                action="migrate",
                change_class=schema_changes(current, target).overall,
                to_version=target.version,
            )
        except (SchemaError, RejectedError) as refusal:
            code = refusal.codes[0] if isinstance(refusal, SchemaError) and refusal.codes else type(refusal).__name__
            if isinstance(refusal, RejectedError):
                code = refusal.code.value
            emit("schema_migration_failed", operator=operator, doc=doc, target=target.version, code=code)
            return code
        if target.version == active:
            pins.pop(doc, None)
        else:
            pins[doc] = target.version
        emit("schema_migrated", operator=operator, doc=doc, **{"from": pinned, "to": target.version})
        return "moved"

    def pin_document(self, doc: str, version: int, *, operator: str) -> None:
        """Serve one document from `version` whatever the active version is."""
        self._operator(operator)
        self.schemas.pin(doc, version, operator=operator)
        emit("schema_pinned", operator=operator, doc=doc, version=version)

    def unpin_document(self, doc: str, *, operator: str) -> None:
        """Serve the document from the active version again."""
        self._operator(operator)
        self.schemas.pin(doc, None, operator=operator)
        emit("schema_unpinned", operator=operator, doc=doc)

    def schema_history(self, *, limit: int | None = None, doc: str | None = None) -> list[HistoryRecord]:
        """The recorded changes of the active version and the pins, oldest first (`limit` keeps the newest)."""
        return self.schemas.history(limit=limit, doc=doc)

    def set_rejection_audit(self, doc: str, setting: str) -> None:
        """`setting` is "enabled", "disabled" or "inherit" (the schema decides)."""
        self._audit.set_override(self.schemas.version_for(doc), doc, setting)

    def prune_rejections(self, doc: str | None = None) -> int:
        """Prune one document, or every document, each under the schema version it is read under."""
        names = [doc] if doc is not None else self._audit.docs()
        return sum(self._audit.prune(self.schemas.version_for(name), name) for name in names)

    def rejection_audit_summary(self, doc: str) -> dict[str, int]:
        return self._audit.summary(doc)

    # -- logging -------------------------------------------------------------------------------------------------

    def _log(self, decision: str, doc: str, contributor: Identity, detail: list[Any]) -> None:
        emit("admission", doc=doc, contributor=contributor.id, decision=decision, detail=detail)
