"""US4: `scheduler_run` refreshes the due values of a source in batches across documents, within its rate limit, once
per due time, and records every run (FR-009, FR-010, FR-012, FR-025, SC-006)."""

import threading
import time

import pytest

from aduku.behaviors.registry import BehaviorRegistry
from aduku.errors import ErrorCode, RejectedError
from aduku.model import Identity
from aduku.storage.base import Storage
from tests.deferred_helpers import START, Built, build, deferred, deferred_value
from tests.fixtures import BatchHook
from tests.service_helpers import ALICE, ROOT, put

HOURLY = chr(34) + "1h" + chr(34)
BAND = f'"/riskBand": {deferred(hook="fx.batch@1", depends=chr(34) + "/country" + chr(34), refresh="scheduled::{every: " + HOURLY + "}", type_="string")}'
SOURCE = "fx.batch@1"


def populate(built: Built, count: int) -> None:
    for n in range(count):
        country = "US" if n % 2 else "DE"
        built.service.append(f"d{n:03d}", [put("/name", f"N{n}"), put("/country", country)], contributor=ALICE)


def batches(built: Built) -> list[int]:
    return list(built.behaviors.get("fx.batch", 1).impl.batches)  # type: ignore[union-attr]


def run(built: Built, hook: str = SOURCE, caller: Identity = ROOT):  # type: ignore[no-untyped-def]
    return built.service.scheduler_run(hook, caller=caller)


def test_due_documents_are_refreshed_in_batches_of_the_source_maximum(backend: Storage) -> None:
    built = build(backend, BAND, sources=[SOURCE])
    populate(built, 120)
    report = run(built)
    assert batches(built) == [50, 50, 20]
    assert (report.attempted, report.succeeded, report.failed, report.skipped, report.calls) == (120, 120, 0, 0, 3)
    assert report.reason is None and report.hook == SOURCE and len(report.items) == 120
    assert deferred_value(built.service.get("d001"), "riskBand") == "band-US"
    assert deferred_value(built.service.get("d000"), "riskBand") == "band-DE"


def test_a_second_run_at_once_does_nothing_and_a_run_an_interval_later_refreshes_every_document(
    backend: Storage,
) -> None:
    built = build(backend, BAND, sources=[SOURCE])
    populate(built, 120)
    run(built)
    again = run(built)
    assert (again.attempted, again.calls) == (0, 0) and batches(built) == [50, 50, 20]
    built.clock.advance(3599)
    assert run(built).attempted == 0
    built.clock.advance(1)
    assert run(built).attempted == 120 and batches(built) == [50, 50, 20] * 2


def test_a_read_never_calls_the_source_even_when_the_value_is_overdue(backend: Storage) -> None:
    built = build(backend, BAND, sources=[SOURCE])
    populate(built, 3)
    before = built.calls(SOURCE) if hasattr(built.behaviors.get("fx.batch", 1).impl, "calls") else 0  # type: ignore[union-attr]
    result = built.service.get("d000")
    assert (
        result.deferred[0].status == "pending" and built.calls(SOURCE) == before
    )  # not fetched yet: the scheduler's job
    run(built)
    built.clock.advance(86400 * 3)
    after = built.service.get("d000")
    assert after.deferred[0].status == "ok" and deferred_value(after, "riskBand") == "band-DE"
    assert built.calls(SOURCE) == 1  # only the scheduler's single run of one batch


def test_a_rate_limit_stops_the_run_and_leaves_the_rest_due_for_the_next_one(backend: Storage) -> None:
    def register(behaviors: BehaviorRegistry) -> None:
        behaviors.register_hook("fx.paced", 1, BatchHook("fx.paced"), batch_max=50, rate_limit=1.0)

    fields = f'"/riskBand": {deferred(hook="fx.paced@1", depends=chr(34) + "/country" + chr(34), refresh="scheduled::{every: " + HOURLY + "}", type_="string")}'
    built = build(backend, fields, extra=register, sources=[], scheduler_budget=1.5)
    populate(built, 120)
    first = run(built, "fx.paced@1")
    assert (first.attempted, first.calls, first.skipped, first.reason) == (100, 2, 20, "time limit")
    built.clock.advance(2)
    second = run(built, "fx.paced@1")
    assert (second.attempted, second.calls, second.reason) == (20, 1, None)  # only what was left; nothing twice
    assert run(built, "fx.paced@1").attempted == 0


def test_a_failing_batch_fails_its_documents_and_they_wait_for_their_next_slot(backend: Storage) -> None:
    built = build(backend, BAND, sources=[SOURCE])
    populate(built, 120)
    built.behaviors.get("fx.batch", 1).impl.fail_calls = {2, 3, 4}  # type: ignore[union-attr]  # batch 2, with its retries
    report = run(built)
    assert (report.attempted, report.succeeded, report.failed, report.calls) == (120, 70, 50, 3)
    assert run(built).attempted == 0  # a down source is not asked again before its next slot
    built.clock.advance(3600)
    built.behaviors.get("fx.batch", 1).impl.fail_calls = set()  # type: ignore[union-attr]
    assert run(built).succeeded == 120


def test_a_second_runner_skips_while_a_lease_is_held(backend: Storage) -> None:
    built = build(backend, BAND, sources=[SOURCE])
    populate(built, 3)
    with backend.transaction() as tx:
        assert tx.take_lease(SOURCE, "another-runner", START.replace(minute=15), START)
    report = run(built)
    assert (report.reason, report.attempted, report.calls) == ("lease held", 0, 0) and batches(built) == []
    built.clock.advance(16 * 60)
    assert run(built).attempted == 3  # the lease expired


def test_every_run_leaves_a_record_and_frees_its_lease(backend: Storage) -> None:
    built = build(backend, BAND, sources=[SOURCE])
    populate(built, 60)
    report = run(built)
    with backend.read() as tx:
        (record,) = tx.list_runs(SOURCE)
    assert (record.run_id, record.attempted, record.succeeded, record.failed, record.calls) == (
        report.run_id,
        60,
        60,
        0,
        2,
    )
    assert record.finished >= record.started
    with backend.transaction() as tx:
        assert tx.take_lease(SOURCE, "next", START.replace(hour=23), START)  # the run released its lease


def test_only_callers_listed_in_invalidate_by_may_run_and_only_for_the_fields_they_may(backend: Storage) -> None:
    two = (
        f'"/a": {deferred(hook="fx.batch@1", depends=chr(34) + "/country" + chr(34), refresh="scheduled::{every: " + HOURLY + "}", type_="string", invalidate_by="[admin]")}, '
        f'"/b": {deferred(hook="fx.batch@1", depends=chr(34) + "/country" + chr(34), refresh="scheduled::{every: " + HOURLY + "}", type_="string", invalidate_by="[user]")}'
    )
    built = build(backend, two, sources=[SOURCE])
    populate(built, 2)
    with pytest.raises(RejectedError) as caught:
        run(built, caller=Identity("eve", ("backfill",)))
    assert caught.value.code is ErrorCode.NOT_AUTHORIZED and batches(built) == []
    report = run(built, caller=Identity("alice", ("user",)))  # may refresh /b only
    assert {(d, f) for d, f, _ in report.items} == {("d000", "/b"), ("d001", "/b")}
    stored = built.stored("d000")
    assert "/b" in stored and "/a" not in stored


def test_a_source_nobody_declares_is_an_unknown_path(backend: Storage) -> None:
    built = build(backend, BAND, sources=[SOURCE])
    with pytest.raises(RejectedError) as caught:
        run(built, "fx.nobody@1")
    assert caught.value.code is ErrorCode.UNKNOWN_PATH


def test_once_and_scheduled_together_the_read_fetches_first_and_the_scheduler_refreshes(backend: Storage) -> None:
    both = f'"/riskBand": {deferred(hook="fx.batch@1", depends=chr(34) + "/country" + chr(34), refresh="[once, scheduled::{every: " + HOURLY + "}]", type_="string")}'
    built = build(backend, both, sources=[SOURCE])
    populate(built, 2)
    assert deferred_value(built.service.get("d001"), "riskBand") == "band-US" and batches(built) == [
        1
    ]  # the read: one item
    built.clock.advance(3600)
    report = run(built)
    assert report.attempted == 2 and batches(built) == [1, 2]  # d000 never fetched; d001 refreshed


def test_a_run_and_a_read_that_need_the_same_value_share_one_call(backend: Storage) -> None:
    class SlowBatch(BatchHook):
        def __call__(self, items, timeout):  # type: ignore[no-untyped-def]
            time.sleep(0.4)
            return super().__call__(items, timeout)

    def register(behaviors: BehaviorRegistry) -> None:
        behaviors.register_hook("fx.slowbatch", 1, SlowBatch("fx.slowbatch"), batch_max=50)

    fields = f'"/riskBand": {deferred(hook="fx.slowbatch@1", depends=chr(34) + "/country" + chr(34), refresh="[once, scheduled::{every: " + HOURLY + "}]", type_="string")}'
    built = build(backend, fields, extra=register, sources=[], real_time=True)
    built.service.append("d1", [put("/name", "A"), put("/country", "US")], contributor=ALICE)
    reader = threading.Thread(target=built.service.get, args=("d1",))
    reader.start()
    time.sleep(0.15)  # the read is in its call now
    report = built.service.scheduler_run("fx.slowbatch@1", caller=ROOT)
    reader.join(10)
    assert built.behaviors.get("fx.slowbatch", 1).impl.calls == 1  # type: ignore[union-attr]
    assert report.succeeded == 1 and deferred_value(built.service.get("d1"), "riskBand") == "band-US"


def test_a_run_that_loses_its_lease_stops_instead_of_working_beside_another_runner(backend: Storage) -> None:
    holder: dict[str, Built] = {}

    class Takeover(BatchHook):
        def __call__(self, items, timeout):  # type: ignore[no-untyped-def]
            built = holder["built"]
            built.clock.advance(5000)  # the lease runs out while this call is going on...
            with backend.transaction() as tx:  # ...and another runner takes it
                tx.take_lease(SOURCE, "other-runner", built.clock.now().replace(year=2030), built.clock.now())
            return super().__call__(items, timeout)

    def register(behaviors: BehaviorRegistry) -> None:
        behaviors.register_hook("fx.batch", 1, Takeover("fx.batch", 2), batch_max=2)

    built = build(backend, BAND, extra=register, sources=[])
    holder["built"] = built
    populate(built, 5)
    report = run(built)
    assert report.reason == "lease lost" and (report.attempted, report.skipped, report.calls) == (2, 3, 1)
