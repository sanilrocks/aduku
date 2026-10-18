"""The `Aduku` facade: append, read entries, rejection audit admin (contracts/library-api.md)."""

from __future__ import annotations

import threading
from collections import OrderedDict
from collections.abc import Sequence
from dataclasses import replace
from datetime import datetime
from typing import Any

from aduku.audit import AuditRecord, RejectionAudit
from aduku.behaviors.registry import BehaviorRegistry
from aduku.engine.admission import check_request
from aduku.engine.derive import DerivationState, Prepared, prepare
from aduku.engine.explain import explain as explain_path
from aduku.engine.model_errors import (
    Extension,
    Origin,
    Replay,
    advance,
    catch_up,
    extend,
    has_validators,
    replay,
)
from aduku.engine.project import project
from aduku.engine.request_hash import request_hash
from aduku.engine.validate import ValidatorFault
from aduku.errors import ErrorCode, Reason, RejectedError, SchemaError, StorageError
from aduku.ion_codec import TOMBSTONE, FrozenStruct, IonError, normalize, parse, to_canonical
from aduku.log import emit
from aduku.model import (
    AppendResult,
    DocumentResult,
    Entry,
    EntryInput,
    Explanation,
    Identity,
    ModelError,
    RebuildReport,
)
from aduku.paths import PathError, parse_path
from aduku.schema.model import Schema
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


def _fits(view: ViewRow | None, last: int, schema: Schema) -> bool:
    """A cached view serves a read only if it is of the latest entry and of this very version and rules."""
    return (
        view is not None
        and view.watermark_seq == last
        and view.schema_version == schema.version
        and view.rules_fingerprint == schema.rules_fingerprint
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
        self._effects: OrderedDict[tuple[str, str, int, str], tuple[tuple[ModelError, ...], tuple[ModelError, ...]]] = (
            OrderedDict()
        )
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
        return result

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
                    )
            except _Cold:
                continue
        raise AssertionError("the last attempt folds inline")

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
        state, pending = self._load(tx, schema, doc, last)
        if not inline and (len(pending) > self.CATCH_UP_LIMIT or (state is None and pending)):
            raise _Cold  # nothing is written yet: fold outside the lock and come back
        try:
            base = self._catch_up(schema, state, pending)
            ext = extend(
                schema, self.behaviors, ready, base, stored_entries, Origin(request_id=request_id), base.errors
            )
        except ValidatorFault as fault:
            raise RejectedError(ErrorCode.VALIDATION_FAILED, message=str(fault)) from fault
        self.derivations += 1
        self._refuse_unusable(entries, stored_entries, base, ext)
        if (
            ext.added and not lenient
        ):  # the flag, if given, was checked in check_request: only a permitted writer has it
            code = ErrorCode.MODEL_ERROR_INTRODUCED if base.errors else ErrorCode.VALIDATION_FAILED
            raise RejectedError(
                code,
                [
                    Reason(code, f"{e.rule_id} fails at {e.path_scope}: {e.message}", rule=e.rule_id, path=e.path_scope)
                    for e in ext.added
                ],
            )

        try:
            tx.append_request(RequestRecord(request_id, doc, contributor.id, request_key, digest), stored_entries)
        except ConstraintError as error:
            raise StorageError(f"ledger constraint violated: {error}") from error
        document, text = self._settle(ext.document)
        tx.put_view(self._view_row(doc, last + len(stored_entries), schema, text, ext.state))
        result = AppendResult(
            entries=tuple((e.id, e.seq) for e in stored_entries),
            request_id=request_id,
            model_errors_added=ext.added,
            model_errors_resolved=ext.resolved,
            view=project(document, ext.errors, ext.state.skipped, ext.conflicts),
        )
        return result, ext.state

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
        if not has_validators(schema):
            return result
        key = (doc, result.request_id, schema.version, schema.rules_fingerprint)
        with self._states_lock:
            memo = self._effects.get(key)
        if memo is None:  # the ledger never changes, so what a request did never changes: work it out once
            with self.storage.read() as tx:
                entries = tx.entries(doc, 0)
            replayed = self._replay(schema, entries, capture=result.request_id)
            before = {e.identity for e in replayed.before}
            after = {e.identity for e in replayed.after}
            memo = (
                tuple(e for e in replayed.after if e.identity not in before),
                tuple(e for e in replayed.before if e.identity not in after),
            )
            with self._states_lock:
                self._effects[key] = memo
                while len(self._effects) > self.state_cache_size:
                    self._effects.popitem(last=False)
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
        return replace(result, view=self.get(doc))

    # -- read ----------------------------------------------------------------------------------------------------

    def get(self, doc: str) -> DocumentResult:
        """The merged document: a pure function of the ledger and the schema version the document is read under.

        A read never takes the write lock: a view it has to derive is kept in memory only. The stored view is written
        by `append` and `rebuild`.
        """
        schema = self.schemas.version_for(doc)
        with self.storage.read() as tx:
            last = tx.last_seq(doc)
            view = tx.get_view(doc)
            if not _fits(view, last, schema):
                view = self._kept_view(doc)
            if view is not None and _fits(view, last, schema):
                return _decode_view(view)
            state, entries = self._load(tx, schema, doc, last)
        ext = self._rebuild(schema, state, entries)  # outside the read transaction
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
        with self.storage.read() as tx:
            entries = tx.entries(doc, 0)
        errors = self._replay(schema, entries).final.errors  # of the same entries, so one snapshot
        return explain_path(schema, self.behaviors, self._engine(schema), entries, path, errors)

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
            except (ValidatorFault, RejectedError, SchemaError) as error:  # this document only: go on with the rest
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
        with self.storage.read() as tx:
            last = tx.last_seq(doc)
            cached = tx.get_view(doc)
            entries = tx.entries(doc, 0)
        if last == 0:  # no entries: nothing to derive, cache or compare
            return "same"
        ext = self._replay(schema, entries).final  # from the ledger alone: no kept state is trusted
        self.derivations += 1
        _, text = self._settle(ext.document)
        fresh = self._view_row(doc, last, schema, text, ext.state)
        self._remember(doc, ext.state)
        if cached == fresh:
            return "same"
        with self.storage.transaction() as tx:  # repair, unless something was appended in the meantime
            if tx.last_seq(doc) == last:
                tx.put_view(fresh)
        return "differs" if _fits(cached, last, schema) else "refreshed"

    def _engine(self, schema: Schema) -> Prepared:
        with self._states_lock:
            ready = self._prepared.get((schema.id, schema.version))
            if ready is None or ready.ctx.schema is not schema:
                ready = self._prepared[(schema.id, schema.version)] = prepare(schema, self.behaviors)
            return ready

    def _load(self, tx: Tx, schema: Schema, doc: str, last: int) -> tuple[DerivationState | None, list[Entry]]:
        """The kept state if it fits this schema, and the entries it has not folded in yet."""
        with self._states_lock:
            state = self._states.get(doc)
        if state is not None and (state.schema_ref is not schema or state.watermark > last):
            state = None  # built under other rules (or ahead of the ledger): start again from the ledger
        entries = tx.entries(doc, state.watermark if state else 0)
        if state is not None and any(e.schema_version != schema.version for e in entries):
            state, entries = (
                None,
                tx.entries(doc, 0),
            )  # written under other rules: attribute as a replay of the ledger does
        return state, entries

    def _replay(self, schema: Schema, entries: list[Entry], capture: str | None = None) -> Replay:
        return replay(schema, self.schemas.get, self.behaviors, self._engine, entries, capture)

    def _rebuild(self, schema: Schema, state: DerivationState | None, entries: list[Entry]) -> Extension:
        """The document, with its errors, from a kept state plus new entries, or from the whole ledger."""
        if state is None:
            return self._replay(schema, entries).final
        return advance(schema, self.behaviors, self._engine(schema), state, entries)

    def _catch_up(self, schema: Schema, state: DerivationState | None, entries: list[Entry]) -> DerivationState:
        if state is None:
            return self._replay(schema, entries).final.state
        return catch_up(schema, self.behaviors, self._engine(schema), state, entries)

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
                state, entries = self._load(tx, schema, doc, last)
            if entries:
                self._remember(doc, self._catch_up(schema, state, entries))
        except (ValidatorFault, RejectedError):
            return

    @staticmethod
    def _settle(document: FrozenStruct) -> tuple[FrozenStruct, str]:
        """The document as it reads back from its canonical text, and that text: fresh and cached views are one value."""
        text = to_canonical(document)
        return parse(text), text

    def _view_row(self, doc: str, last: int, schema: Schema, text: str, state: DerivationState) -> ViewRow:
        notes = FrozenStruct({"skipped": tuple(state.skipped), "conflicts": state.conflicts})
        return ViewRow(
            doc, last, schema.version, schema.rules_fingerprint, text, _encode_errors(state.errors), to_canonical(notes)
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
