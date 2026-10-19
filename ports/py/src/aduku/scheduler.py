"""The deferred scheduler (SPEC 9.5, feature 003 research R-8, R-9). An edge module: it calls sources and reads time.

A run refreshes, for one source, every value that is due: the ones no read will fetch (`scheduled`, `notification`) and
those marked out of date. It collects the due items across documents, groups them by field, sends them in batches of the
source's maximum, waits for the source's rate limit (never past the run's time budget), and writes after every batch, so
a run that stops early has lost nothing and the next run goes on where it ended. What is due comes from the pure planner,
whose rule is "the next fire after the last attempt", so repeating a run changes nothing. One run per source at a time:
a lease row in the store stops a second runner, in this process or another.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

from aduku.engine.deferred import FieldPlan, plan
from aduku.errors import AdukuError
from aduku.fetch import DeferredStage
from aduku.log import emit
from aduku.model import DerivedRecord, DocumentResult, SchedulerReport, SchedulerRun
from aduku.schema.model import FieldSpec, Schema
from aduku.storage.base import Clock, IdSource, Storage

__all__ = ["DEFAULT_BUDGET", "Scheduler"]

DEFAULT_BUDGET = 900.0  # seconds a run may take (it waits for rate limits within it); the rest stays due
MAX_ROUNDS = 16  # a chain of deferred fields needs one round per level


@dataclass(frozen=True)
class _Due:
    doc: str
    schema: Schema
    field: FieldPlan


class Scheduler:
    def __init__(
        self,
        storage: Storage,
        stage: DeferredStage,
        clock: Clock,
        ids: IdSource,
        monotonic: Callable[[], float],
        sleep: Callable[[float], None],
        budget: float,
        documents: Callable[[], list[str]],
        core_of: Callable[[str], tuple[Schema, DocumentResult]],
    ) -> None:
        self._storage = storage
        self._stage = stage
        self._clock = clock
        self._ids = ids
        self._monotonic = monotonic
        self._sleep = sleep
        self._budget = budget
        self._documents = documents
        self._core_of = core_of

    def run(self, hook: str, permitted: Callable[[FieldSpec], bool]) -> SchedulerReport:
        started = self._clock.now()
        run_id = self._ids.new_id()
        lease = timedelta(seconds=self._budget + 60)
        with self._storage.transaction() as tx:
            held = tx.take_lease(hook, run_id, started + lease, started)
        if not held:
            return self._finish(run_id, hook, started, [], 0, "lease held")
        try:
            items, calls, reason = self._work(hook, run_id, permitted)
        finally:
            with self._storage.transaction() as tx:
                tx.release_lease(hook, run_id)
        return self._finish(run_id, hook, started, items, calls, reason)

    # -- the work ------------------------------------------------------------------------------------------------

    def _work(
        self, hook: str, run_id: str, permitted: Callable[[FieldSpec], bool]
    ) -> tuple[list[tuple[str, str, str]], int, str | None]:
        cores: dict[str, tuple[Schema, DocumentResult] | None] = {}
        records: dict[str, dict[str, DerivedRecord]] = {}
        tried: dict[str, set[str]] = {}
        outcome: list[tuple[str, str, str]] = []
        calls = 0
        reason: str | None = None
        limit = self._monotonic() + self._budget
        for _ in range(MAX_ROUNDS):
            due = self._collect(hook, permitted, cores, records, tried)
            if not due:
                break
            done: set[tuple[str, str]] = set()
            for path in sorted({d.field.path for d in due}):
                group = [d for d in due if d.field.path == path]
                size = self._stage.behavior_of(group[0].field).batch_max or 1
                for start in range(0, len(group), size):
                    chunk = group[start : start + size]
                    reason = self._turn(chunk[0].field, limit)
                    if reason is not None:
                        break
                    schema = chunk[0].schema
                    marked = {
                        (d.doc, d.field.path): d.field.record.invalidated if d.field.record else False for d in chunk
                    }
                    results = self._stage.run_items(schema, [(d.doc, d.field) for d in chunk])
                    calls += 1
                    made = self._clock.now()
                    fresh = []
                    for item, result in zip(chunk, results, strict=True):
                        record = self._stage.record_of(
                            item.schema, item.doc, item.field, result, records[item.doc].get(item.field.path), made
                        )
                        fresh.append(record)
                        records[item.doc][item.field.path] = record
                        tried[item.doc].add(item.field.path)
                        done.add((item.doc, item.field.path))
                        outcome.append((item.doc, item.field.path, "ok" if result.outcome == "value" else "failed"))
                    self._stage.store(fresh, marked)
                    if all(r.error == "breaker_open" for r in results):
                        reason = "breaker open"
                        break
                    with self._storage.transaction() as tx:
                        renewed = tx.renew_lease(hook, run_id, self._clock.now() + timedelta(seconds=self._budget + 60))
                    if not renewed:  # the lease ran out or was taken: another runner may be working on this source
                        reason = "lease lost"
                        break
                if reason is not None:
                    break
            if reason is not None:
                outcome.extend((d.doc, d.field.path, "skipped") for d in due if (d.doc, d.field.path) not in done)
                break
        return outcome, calls, reason

    def _collect(
        self,
        hook: str,
        permitted: Callable[[FieldSpec], bool],
        cores: dict[str, tuple[Schema, DocumentResult] | None],
        records: dict[str, dict[str, DerivedRecord]],
        tried: dict[str, set[str]],
    ) -> list[_Due]:
        """What is due now for `hook`, document by document: from the planner, for fields the caller may refresh."""
        due: list[_Due] = []
        now = self._clock.now()
        for doc in self._documents():
            if doc not in cores:
                try:
                    schema, core = self._core_of(doc)
                    cores[doc] = (schema, core) if DeferredStage.has_deferred(schema) else None
                except AdukuError:
                    cores[doc] = None  # a document that cannot be derived is left alone
                if cores[doc] is not None:
                    records[doc] = self._stage.records_of(doc)
                    tried[doc] = set()
            found = cores[doc]
            if found is None:
                continue
            schema, core = found
            planned = plan(schema, core.document, records[doc], now, mode="scheduler", skip=frozenset(tried[doc]))
            for field in planned.due():
                deferred = field.spec.deferred
                if deferred is not None and deferred.source == hook and permitted(field.spec):
                    due.append(_Due(doc, schema, field))
        return due

    def _turn(self, field: FieldPlan, limit: float) -> str | None:
        """Wait for the source's rate limit if the run has the time; the reason to stop if it has not."""
        behavior = self._stage.behavior_of(field)
        deferred = field.spec.deferred
        assert deferred is not None
        limiter = self._stage.limiter(deferred.source, behavior.rate_limit)
        if self._monotonic() >= limit:
            return "time limit"
        for _ in range(2):  # a read may take the token in between; one more wait settles it
            if limiter is None:
                return None
            wait = limiter.wait_time()
            if self._monotonic() + wait > limit:
                return "time limit"
            if wait > 0:
                self._sleep(wait)
            if limiter.try_acquire():
                return None
        return "time limit"

    # -- the record ----------------------------------------------------------------------------------------------

    def _finish(
        self,
        run_id: str,
        hook: str,
        started: datetime,
        items: list[tuple[str, str, str]],
        calls: int,
        reason: str | None,
    ) -> SchedulerReport:
        finished = self._clock.now()
        counts = {name: sum(1 for _, _, outcome in items if outcome == name) for name in ("ok", "failed", "skipped")}
        record = SchedulerRun(
            run_id=run_id,
            hook=hook,
            started=started,
            finished=finished,
            attempted=counts["ok"] + counts["failed"],
            succeeded=counts["ok"],
            failed=counts["failed"],
            skipped=counts["skipped"],
            calls=calls,
            reason=reason,
        )
        with self._storage.transaction() as tx:
            tx.append_run(record)
        emit(
            "scheduler_run",
            source=hook,
            run=run_id,
            attempted=record.attempted,
            succeeded=record.succeeded,
            failed=record.failed,
            skipped=record.skipped,
            calls=calls,
            reason=reason,
        )
        return SchedulerReport(
            run_id=run_id,
            hook=hook,
            started=started,
            finished=finished,
            attempted=record.attempted,
            succeeded=record.succeeded,
            failed=record.failed,
            skipped=record.skipped,
            calls=calls,
            reason=reason,
            items=tuple(items),
        )
