"""The `Aduku` facade: append, read entries, rejection audit admin (contracts/library-api.md)."""

from __future__ import annotations

import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Sequence
from dataclasses import replace
from datetime import datetime
from functools import partial
from typing import Any, Literal

from aduku.audit import AuditRecord, RejectionAudit
from aduku.behaviors.registry import BehaviorRegistry
from aduku.engine.admission import check_request
from aduku.engine.compute import compute_fields, failure_mode_of
from aduku.engine.derive import DerivationState, Prepared, prepare
from aduku.engine.effects import EffectView, PendingCall, fingerprint, view_from_records
from aduku.engine.explain import explain as explain_path
from aduku.engine.model_errors import (
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
from aduku.engine.validate import ValidatorFault
from aduku.errors import ComputeFault, ErrorCode, Reason, RejectedError, SchemaError, StorageError
from aduku.fetch import DeferredStage
from aduku.hooks import HookRunner
from aduku.ion_codec import ABSENT, TOMBSTONE, FrozenStruct, IonError, normalize, parse, to_canonical
from aduku.log import emit
from aduku.model import (
    AppendResult,
    DeferredInfo,
    DocumentResult,
    EffectInfo,
    EffectRecord,
    Entry,
    EntryInput,
    Explanation,
    Identity,
    ModelError,
    RebuildReport,
    RecomputeReport,
    SchedulerReport,
)
from aduku.paths import PathError, parse_path
from aduku.scheduler import DEFAULT_BUDGET, Scheduler
from aduku.schema.model import FieldSpec, Schema, conforms
from aduku.schema.registry import SchemaRegistry
from aduku.storage.base import Clock, ConstraintError, IdSource, RequestRecord, Storage, Tx, ViewRow

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


def _fits(view: ViewRow | None, last: int, schema: Schema, effects_seq: int = 0) -> bool:
    """A cached view serves a read only if it is of the latest entry and effect, and of this very version and rules."""
    return (
        view is not None
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


def _known_request(
    tx: Tx, doc: str, contributor: Identity, request_key: str, digest: str | None
) -> AppendResult | None:
    """The duplicate answer for a request key already used, None if it is new. A different payload is refused."""
    known = tx.find_request(doc, contributor.id, request_key)
    if known is None:
        return None
    if known.request_hash != digest:
        raise RejectedError(
            ErrorCode.REQUEST_KEY_REUSED, message=f"request key {request_key!r} was used for a different request"
        )
    stored = tx.entries_of_request(known.request_id)
    return AppendResult(entries=tuple((e.id, e.seq) for e in stored), request_id=known.request_id, duplicate=True)


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
    ) -> None:
        self.storage = storage
        self.schemas = schemas
        self.clock = clock or Clock.system()
        self.ids = ids or IdSource.system()
        self.behaviors = behaviors or BehaviorRegistry.with_builtins()
        self._audit = RejectionAudit(lambda: self.storage.audit_backend(), self.clock)
        # In-memory per-tier layers, so that a read or an append folds only the entries it has not seen. Disposable:
        # losing it only costs a derivation from the ledger.
        self._states: OrderedDict[str, DerivationState] = OrderedDict()
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
        self._states_lock = threading.Lock()
        self._prepared: dict[
            tuple[str, int], Prepared
        ] = {}  # per schema version: the merge context, defaults and fallbacks
        self.state_cache_size = state_cache_size  # documents kept in memory; the least recently used are dropped
        self.derivations = 0  # how many times a document was derived (not served from a cache); for tests
        schemas.guard(self._tiers_in_use)  # activating or pinning a version checks it against the ledger

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
        state_now = ext.state
        to_store = [
            r for r in fresh if stored_view is not None and stored_view.lookup(r.field, r.fingerprint, r.hook) is None
        ]
        if to_store:  # the request is accepted: its outcomes become part of the log, in the same transaction
            tx.append_effects(to_store)
            state_now = replace(state_now, effects_seq=tx.effects_seq(doc))
        document, text = self._settle(ext.document)
        tx.put_view(self._view_row(doc, last + len(stored_entries), schema, text, state_now))
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
                entries = tx.entries(doc, 0)
                effects = self._effect_view(tx, self._engine(schema), doc)
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
        if result.view is None or not DeferredStage.has_deferred(schema):
            return result
        return replace(result, view=self._deferred.stored(schema, doc, result.view))

    # -- read ----------------------------------------------------------------------------------------------------

    def get(
        self,
        doc: str,
        *,
        consistency: Literal["cached", "fresh"] = "cached",
        include: Sequence[str] = (),
    ) -> DocumentResult:
        """The merged document: a pure function of the ledger and the schema version the document is read under.

        A read never takes the write lock: a view it has to derive is kept in memory only. The stored view is written
        by `append` and `rebuild`. Deferred fields (feature 003) are overlaid after that: sources that are due are
        called within the fetch deadline, and a failing source only changes a field's status. `consistency="fresh"`
        ignores the `ttl` of `per_fetch` fields; `include=("pending",)` says what each pending field waits for.
        """
        if consistency not in ("cached", "fresh"):
            raise ValueError("consistency is cached or fresh")
        schema = self.schemas.version_for(doc)
        core = self._core(doc, schema)
        if not DeferredStage.has_deferred(schema):
            return core
        return self._deferred.read(schema, doc, core, consistency=consistency, detail="pending" in include)

    def _core(self, doc: str, schema: Schema) -> DocumentResult:
        """P1 to P6 and the projection: everything that does not depend on the derived store."""
        ready = self._engine(schema)
        with self.storage.read() as tx:
            last = tx.last_seq(doc)
            effects = self._effect_view(tx, ready, doc)
            seen = effects.seq if effects else 0
            view = tx.get_view(doc)
            if not _fits(view, last, schema, seen):
                view = self._kept_view(doc)
            if view is not None and _fits(view, last, schema, seen):
                return _decode_view(view)
            state, entries = self._load(tx, schema, doc, last, seen)
        ext = self._rebuild(schema, state, entries, effects)  # outside the read transaction
        if ext.pending:  # impure fields with no outcome for their inputs: call the hooks, record, and derive again
            ext, effects = self._fill(
                schema, doc, ext, effects or EffectView(), lambda view: self._rebuild(schema, state, entries, view)
            )
        self.derivations += 1
        self._remember(doc, ext.state)
        document, text = self._settle(ext.document)
        self._keep_view(self._view_row(doc, last, schema, text, ext.state))
        return project(document, ext.errors, ext.state.skipped, ext.conflicts)

    def explain(self, doc: str, path: str) -> Explanation:
        """Why `path` has its value in `doc`: the winning entry, the rule, and the entries that made no difference.

        Worked out from the ledger under the schema version the document is read under. Raises RejectedError
        (UNKNOWN_PATH) for a path the schema does not declare.
        """
        schema = self.schemas.version_for(doc)
        try:
            parsed = parse_path(path)
        except PathError as error:
            raise RejectedError(ErrorCode.UNKNOWN_PATH, message=f"invalid path: {error}") from error
        if schema.field_for(parsed) is None:
            raise RejectedError(ErrorCode.UNKNOWN_PATH, message="the schema does not declare this path")
        derived = schema.derived_field_at_or_above(parsed)
        if derived is not None and derived.derived == "deferred":
            return self._with_authority(
                schema, parsed, self._deferred.explain(schema, doc, self._core(doc, schema), derived.pattern, path)
            )
        ready = self._engine(schema)
        if ready.has_impure:
            self.get(doc)  # makes sure every impure field has an outcome for its current inputs
        with self.storage.read() as tx:
            entries = tx.entries(doc, 0)
            effects = self._effect_view(tx, ready, doc)
        errors = self._replay(schema, entries, effects=effects).final.errors  # of the same entries, so one snapshot
        return self._with_authority(
            schema, parsed, explain_path(schema, self.behaviors, ready, entries, path, errors, effects)
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
        differing: list[str] = []
        refreshed: list[str] = []
        failed: list[tuple[str, str]] = []
        for name in names:
            try:
                outcome = self._rebuild_one(name)
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
        report = RebuildReport(tuple(names), tuple(differing), tuple(refreshed), tuple(failed))
        emit("rebuild", checked=len(names), differing=differing, refreshed=refreshed, failed=[d for d, _ in failed])
        return report

    def _rebuild_one(self, doc: str) -> str:
        schema = self.schemas.version_for(doc)
        ready = self._engine(schema)
        with self.storage.read() as tx:
            last = tx.last_seq(doc)
            cached = tx.get_view(doc)
            entries = tx.entries(doc, 0)
            effects = self._effect_view(tx, ready, doc)
        if last == 0:  # no entries: nothing to derive, cache or compare
            return "same"
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
        fresh = self._view_row(doc, last, schema, text, ext.state)
        self._remember(doc, ext.state)
        if cached == fresh:
            return "same"
        with self.storage.transaction() as tx:  # repair, unless something was appended in the meantime
            if tx.last_seq(doc) == last:
                tx.put_view(fresh)
        return "differs" if _fits(cached, last, schema, fresh.effects_seq) else "refreshed"

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
        with self._states_lock:
            state = self._states.get(doc)
        if state is not None and (
            state.schema_ref is not schema or state.watermark > last or state.effects_seq != effects_seq
        ):
            state = (
                None  # built under other rules or an older log (or ahead of the ledger): start again from the ledger
            )
        entries = tx.entries(doc, state.watermark if state else 0)
        if state is not None and any(e.schema_version != schema.version for e in entries):
            state, entries = (
                None,
                tx.entries(doc, 0),
            )  # written under other rules: attribute as a replay of the ledger does
        return state, entries

    def _replay(
        self, schema: Schema, entries: list[Entry], capture: str | None = None, effects: EffectView | None = None
    ) -> Replay:
        return replay(schema, self.schemas.get, self.behaviors, self._engine, entries, capture, effects)

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
            if entries:
                self._remember(doc, self._catch_up(schema, state, entries, effects))
        except (ValidatorFault, ComputeFault, RejectedError):
            return

    @staticmethod
    def _settle(document: FrozenStruct) -> tuple[FrozenStruct, str]:
        """The document as it reads back from its canonical text, and that text: fresh and cached views are one value."""
        text = to_canonical(document)
        return parse(text), text

    def _view_row(self, doc: str, last: int, schema: Schema, text: str, state: DerivationState) -> ViewRow:
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
        )

    def _remember(self, doc: str, folded: DerivationState) -> None:
        with self._states_lock:
            kept = self._states.get(doc)
            if kept is None or kept.schema_ref is not folded.schema_ref or kept.watermark <= folded.watermark:
                self._states[doc] = folded
            self._states.move_to_end(doc)
            while len(self._states) > self.state_cache_size:
                self._states.popitem(last=False)

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
            entries = tx.entries(doc, 0)
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
