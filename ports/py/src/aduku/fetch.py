"""The deferred stage's executor (SPEC 9.5, feature 003 research R-1, R-6). An edge module: it calls sources, reads time
and writes the derived store. Every decision about what is due is the pure planner's (`aduku.engine.deferred`).

A read asks the planner what is due, calls those sources (the calls of one round run side by side), stores the answers
and asks again, because a field whose input is another deferred field only becomes due once that value is in. A source is
never called while the ledger's write lock is held: results are stored in a short transaction of their own.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from functools import partial
from typing import Literal

from aduku.behaviors.registry import Behavior, BehaviorRegistry
from aduku.breaker import BreakerBank, RateLimiter
from aduku.engine.deferred import FieldPlan, overlay, plan, statuses
from aduku.engine.document import value_at
from aduku.engine.graph import deferred_levels
from aduku.hooks import HookResult, HookRunner
from aduku.ion_codec import ABSENT, FrozenStruct, Value, to_canonical
from aduku.log import emit
from aduku.model import DerivedRecord, DocumentResult, Explanation
from aduku.paths import parse_path, parse_pattern
from aduku.schema.model import Schema, conforms
from aduku.storage.base import Clock, Storage

__all__ = ["DeferredStage"]


class _Flight:
    """A call in progress, which requests needing the same answer wait for instead of calling again."""

    def __init__(self) -> None:
        self.done = threading.Event()
        self.result: HookResult | None = None


class DeferredStage:
    def __init__(
        self,
        storage: Storage,
        behaviors: BehaviorRegistry,
        hooks: HookRunner,
        clock: Clock,
        monotonic: Callable[[], float],
        fetch_deadline: float,
    ) -> None:
        self._storage = storage
        self._behaviors = behaviors
        self._hooks = hooks
        self._clock = clock
        self._monotonic = monotonic
        self._fetch_deadline = fetch_deadline
        self._breakers = BreakerBank(monotonic, on_change=self._breaker_changed)
        self._limiters: dict[str, RateLimiter] = {}
        self._limiters_lock = threading.Lock()
        self._flights: dict[tuple[str, str, str, str], _Flight] = {}  # (document, field, fingerprint, source)
        self._flights_lock = threading.Lock()

    @staticmethod
    def has_deferred(schema: Schema) -> bool:
        return any(spec.deferred is not None for spec in schema.fields)

    # -- reads ---------------------------------------------------------------------------------------------------

    def read(
        self,
        schema: Schema,
        doc: str,
        core: DocumentResult,
        *,
        consistency: Literal["cached", "fresh"] = "cached",
        detail: bool = False,
    ) -> DocumentResult:
        """`core` with the deferred fields overlaid. Sources that are due are called first, within one read deadline."""
        document = core.document
        assert isinstance(document, FrozenStruct)
        records = self.records_of(doc)
        deadline_at = self._monotonic() + self._fetch_deadline
        tried: set[str] = set()
        rounds = len(deferred_levels(schema)) + 1
        for _ in range(rounds):
            planned = plan(schema, document, records, self._clock.now(), consistency=consistency, skip=frozenset(tried))
            due = planned.due()
            if not due or self._monotonic() >= deadline_at:
                break
            tried.update(f.path for f in due)
            marked = {(doc, path): record.invalidated for path, record in records.items()}
            fresh = self._fetch(schema, doc, due, records, deadline_at)
            if fresh:
                self.store(fresh, marked)
                records.update({r.field: r for r in fresh})
        final = plan(schema, document, records, self._clock.now(), consistency=consistency, skip=frozenset(tried))
        return replace(core, document=overlay(document, final), deferred=statuses(final, detail))

    def stored(self, schema: Schema, doc: str, core: DocumentResult) -> DocumentResult:
        """`core` with the stored deferred values and their statuses; no source is called (a write's view)."""
        document = core.document
        assert isinstance(document, FrozenStruct)
        final = plan(
            schema, document, self.records_of(doc), self._clock.now(), skip=frozenset(f.pattern for f in schema.fields)
        )
        return replace(core, document=overlay(document, final), deferred=statuses(final))

    def records_of(self, doc: str) -> dict[str, DerivedRecord]:
        with self._storage.read() as tx:
            return {r.field: r for r in tx.get_derived(doc)}

    # -- calls ---------------------------------------------------------------------------------------------------

    def _fetch(
        self,
        schema: Schema,
        doc: str,
        due: Sequence[FieldPlan],
        records: dict[str, DerivedRecord],
        deadline_at: float,
    ) -> list[DerivedRecord]:
        """Call the sources of one round side by side and turn each answer into the field's new record."""
        results: dict[str, HookResult] = {}

        def run(field: FieldPlan) -> None:
            results[field.path] = self._call(schema, doc, field, deadline_at)

        threads = [threading.Thread(target=run, args=(f,), name="aduku-fetch", daemon=True) for f in due]
        for thread in threads:
            thread.start()
        give_up_at = deadline_at + 0.5  # the calls honor the deadline themselves; this only guards a stuck one
        for thread in threads:
            thread.join(max(0.0, give_up_at - self._monotonic()))
        made = self._clock.now()
        fresh = []
        for field in due:
            result = results.get(field.path, HookResult("failure", None, "timeout", 0))
            fresh.append(self.record_of(schema, doc, field, result, records.get(field.path), made))
        return fresh

    def _call(self, schema: Schema, doc: str, field: FieldPlan, deadline_at: float) -> HookResult:
        """One source call for this field on a read: shared with any request that needs the same answer at the same
        moment, refused (not waited for) at a rate limit, and kept if it answers after the read has gone."""
        return self.run_items(
            schema,
            [(doc, field)],
            deadline_at,
            late=lambda _index, answer: self._keep_late(schema, doc, field, answer),
            read_token=True,
        )[0]

    def limiter(self, source: str, rate: float | None) -> RateLimiter | None:
        if rate is None:
            return None
        with self._limiters_lock:
            limiter = self._limiters.get(source)
            if limiter is None:
                limiter = self._limiters[source] = RateLimiter(rate, self._monotonic)
            return limiter

    def _keep_late(self, schema: Schema, doc: str, field: FieldPlan, answer: object) -> None:
        """An answer that arrived after its read had returned: if it is a good value for the inputs it was asked for, keep
        it for the next read (unless a good one for them is stored already)."""
        deferred = field.spec.deferred
        assert deferred is not None and field.fingerprint is not None
        if answer is None or (field.spec.type is not None and not conforms(field.spec.type, answer)):
            return
        try:
            text = to_canonical(answer)
        except (TypeError, ValueError):
            return
        made = self._clock.now()
        with self._storage.transaction() as tx:
            current = next((r for r in tx.get_derived(doc) if r.field == field.path), None)
            if current is not None and current.input_fingerprint != field.fingerprint:
                return  # the inputs have moved on: this answer is for a question nobody is asking now
            if current is not None and current.status == "ok":
                return  # a good value for these inputs is there already
            marked = current.invalidated if current is not None else False  # a mark made since the call started stays
            tx.put_derived(
                [
                    DerivedRecord(
                        doc=doc,
                        field=field.path,
                        value=text,
                        has_value=True,
                        as_of=made,
                        input_fingerprint=field.fingerprint,
                        hook=deferred.source,
                        shape=deferred.shape(field.spec.type),
                        schema_version=schema.version,
                        status="ok",
                        attempts=1,
                        last_attempt=made,
                        invalidated=marked,
                    )
                ]
            )
        emit("deferred_call", doc=doc, field=field.path, source=deferred.source, attempts=1, result="late")

    def record_of(
        self,
        schema: Schema,
        doc: str,
        field: FieldPlan,
        result: HookResult,
        before: DerivedRecord | None,
        made: object,
    ) -> DerivedRecord:
        """The record a call leaves: the new value, or the last good value (if any) with the failure noted."""
        deferred = field.spec.deferred
        assert deferred is not None and field.fingerprint is not None
        shape = deferred.shape(field.spec.type)
        base = {
            "doc": doc,
            "field": field.path,
            "input_fingerprint": field.fingerprint,
            "hook": deferred.source,
            "shape": shape,
            "schema_version": schema.version,
            "attempts": result.attempts,
            "last_attempt": made,
        }
        if result.attempts == 0:  # refused (breaker open, rate limit) or never started: that is not an attempt
            prior = before if before is not None and before.hook == deferred.source and before.shape == shape else None
            base["attempts"] = prior.attempts if prior is not None else 0
            base["last_attempt"] = prior.last_attempt if prior is not None else None
        if result.outcome == "value":
            return DerivedRecord(
                value=to_canonical(result.value),
                has_value=True,
                as_of=made,  # type: ignore[arg-type]
                status="ok",
                error=None,
                invalidated=False,
                **base,  # type: ignore[arg-type]
            )
        kept = before if before is not None and before.hook == deferred.source and before.shape == shape else None
        has_value = kept is not None and kept.has_value
        return DerivedRecord(
            value=kept.value if kept is not None and has_value else None,
            has_value=has_value,
            as_of=kept.as_of if kept is not None and has_value else None,
            status="stale" if has_value else "error",
            error=result.error,
            invalidated=kept.invalidated if kept is not None else False,
            **base,  # type: ignore[arg-type]
        )

    def explain(self, schema: Schema, doc: str, core: DocumentResult, pattern: str, path: str) -> Explanation:
        """Why a deferred path shows what it shows: read from the derived store; no source is called."""
        document = core.document
        assert isinstance(document, FrozenStruct)
        planned = plan(
            schema,
            document,
            self.records_of(doc),
            self._clock.now(),
            skip=frozenset(f.pattern for f in schema.fields),
        )
        field = planned.field(pattern)
        assert field is not None and field.spec.deferred is not None
        record = field.record
        value: Value = None
        if field.include and field.shown is not None:
            value = (
                field.shown
                if path == pattern
                else value_at(field.shown, parse_path(path)[len(parse_pattern(pattern)) :])
            )
            value = None if value is ABSENT else value
        return Explanation(
            path,
            value,
            "deferred",
            None,
            field.spec.deferred.source,
            dependencies=field.inputs,
            status=field.status,
            as_of=record.as_of if record is not None and field.status in ("ok", "stale") else None,
        )

    def behavior_of(self, field: FieldPlan) -> Behavior:
        deferred = field.spec.deferred
        assert deferred is not None
        behavior = self._behaviors.get(deferred.hook, deferred.version)
        assert behavior is not None and behavior.kind == "hook"
        return behavior

    def store(
        self, records: Sequence[DerivedRecord], marked_before: Mapping[tuple[str, str], bool] | None = None
    ) -> None:
        """Write the records a round of calls produced. `marked_before` says which values were marked out of date when
        the calls were planned: one that was clean then and is marked now was marked while its call ran, so the mark stays."""
        if not records:
            return
        with self._storage.transaction() as tx:
            if marked_before is None:
                tx.put_derived(records)
                return
            current: dict[str, dict[str, DerivedRecord]] = {}
            kept = []
            for record in records:
                if record.doc not in current:
                    current[record.doc] = {r.field: r for r in tx.get_derived(record.doc)}
                now = current[record.doc].get(record.field)
                raced = now is not None and now.invalidated and not marked_before.get((record.doc, record.field), False)
                kept.append(replace(record, invalidated=True) if raced else record)
            tx.put_derived(kept)

    def run_items(
        self,
        schema: Schema,
        items: Sequence[tuple[str, FieldPlan]],
        deadline_at: float | None = None,
        *,
        late: Callable[[int, object], None] | None = None,
        read_token: bool = False,
    ) -> list[HookResult]:
        """Call the source for `items` (documents' values of one field), the way the scheduler does: a batch source gets
        them in one call, any other source one call each. An item another request is already asking for is waited for,
        not asked again. A source whose breaker is open is not called. A read (`read_token`) takes a token of the source's rate
        limit for the call and is refused without one; the scheduler waits for its turn before it calls this. `late(i, answer)`
        gets the answer of an item whose call finished after the caller stopped waiting."""
        field = items[0][1]
        deferred = field.spec.deferred
        assert deferred is not None
        behavior = self._behaviors.get(deferred.hook, deferred.version)
        assert behavior is not None and behavior.kind == "hook"
        results: list[HookResult | None] = [None] * len(items)
        mine: dict[int, tuple[tuple[str, str, str, str], _Flight]] = {}
        waits: list[tuple[int, _Flight]] = []
        with self._flights_lock:
            for index, (doc, planned) in enumerate(items):
                key = (doc, planned.path, planned.fingerprint or "", deferred.source)
                existing = self._flights.get(key)
                if existing is None:
                    mine[index] = (key, self._flights.setdefault(key, _Flight()))
                else:
                    waits.append((index, existing))
        try:
            leaders = list(mine)
            if leaders:
                breaker = self._breakers.for_source(deferred.source)
                limiter = self.limiter(deferred.source, behavior.rate_limit) if read_token else None
                refusal = None
                if limiter is not None and not limiter.try_acquire():
                    refusal = "rate_limited"
                elif not breaker.allow():
                    refusal = "breaker_open"
                if refusal is not None:
                    for index in leaders:
                        results[index] = HookResult("failure", None, refusal, 0)
                else:
                    for index, result in zip(
                        leaders, self._invoke_items(behavior, items, leaders, deadline_at, late), strict=True
                    ):
                        results[index] = self._checked(items[index][1], result)
                    kinds = [results[i] for i in leaders]
                    if any(r is not None and r.outcome == "value" for r in kinds):
                        breaker.success()
                    else:
                        breaker.failure()
                for index in leaders:
                    done = results[index]
                    assert done is not None
                    emit(
                        "deferred_call",
                        doc=items[index][0],
                        field=items[index][1].path,
                        source=deferred.source,
                        attempts=done.attempts,
                        result="value" if done.outcome == "value" else done.error,
                    )
        finally:
            for index, (key, flight) in mine.items():
                flight.result = results[index]
                flight.done.set()
                with self._flights_lock:
                    self._flights.pop(key, None)
        for index, flight in waits:
            budget = (
                deadline_at if deadline_at is not None else self._monotonic() + deferred.deadline
            ) - self._monotonic()
            flight.done.wait(max(0.0, budget))
            results[index] = flight.result or HookResult("failure", None, "timeout", 0)
        return [r if r is not None else HookResult("failure", None, "timeout", 0) for r in results]

    def _invoke_items(
        self,
        behavior: Behavior,
        items: Sequence[tuple[str, FieldPlan]],
        leaders: Sequence[int],
        deadline_at: float | None,
        late: Callable[[int, object], None] | None = None,
    ) -> list[HookResult]:
        field = items[leaders[0]][1]
        deferred = field.spec.deferred
        assert deferred is not None
        if behavior.batch_max is not None:
            context = {"doc": [items[i][0] for i in leaders], "field": field.path} if behavior.with_context else None
            return self._hooks.run_batch(
                behavior.impl,
                deferred.policy,
                [dict(items[i][1].inputs) for i in leaders],
                deadline_at=deadline_at,
                context=context,
                on_late=(lambda answer: _late_batch(late, leaders, answer)) if late is not None else None,
            )
        found = []
        for i in leaders:
            doc, planned = items[i]
            context = {"doc": doc, "field": planned.path} if behavior.with_context else None
            found.append(
                self._hooks.run(
                    behavior.impl,
                    deferred.policy,
                    dict(planned.inputs),
                    deadline_at=deadline_at,
                    context=context,
                    on_late=partial(late, i) if late is not None else None,
                )
            )
        return found

    @staticmethod
    def _checked(field: FieldPlan, result: HookResult) -> HookResult:
        if result.outcome == "value" and field.spec.type is not None and not conforms(field.spec.type, result.value):
            return HookResult("failure", None, "bad_value", result.attempts)
        return result

    @staticmethod
    def _breaker_changed(key: str, old: str, new: str) -> None:
        emit("breaker", source=key, was=old, state=new)


def _late_batch(late: Callable[[int, object], None], leaders: Sequence[int], answer: object) -> None:
    """Hand each item of a batch source's late answer to `late`, by the item's index."""
    if isinstance(answer, list | tuple) and len(answer) == len(leaders):
        for index, item in zip(leaders, answer, strict=True):
            if not isinstance(item, BaseException):
                late(index, item)
