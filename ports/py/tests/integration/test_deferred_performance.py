"""Plan performance goals: a read with fresh deferred values costs next to nothing and makes no call; a source that never
answers costs the fetch deadline and no more; planning a large run is cheap (SC-003, SC-004, SC-006)."""

import gc
import statistics
import time
from datetime import UTC, datetime

from aduku.behaviors.registry import BehaviorRegistry
from aduku.engine.deferred import plan
from aduku.ion_codec import parse
from aduku.schema.loader import load_schema
from aduku.storage.memory import MemoryStorage
from tests.deferred_helpers import build, deferred, delayed
from tests.service_helpers import ALICE, put

FIVE = ", ".join(f'"/f{n}": {deferred(refresh="per_fetch", ttl=chr(34) + "1d" + chr(34))}' for n in range(5))


def median_ms(call, repeats: int = 40) -> float:  # type: ignore[no-untyped-def]
    samples = []
    for _ in range(repeats):
        started = time.perf_counter()
        call()
        samples.append((time.perf_counter() - started) * 1000)
    return statistics.median(samples)


def test_five_fresh_deferred_fields_add_under_five_milliseconds_to_a_cached_read_and_make_no_call() -> None:
    built = build(MemoryStorage(), FIVE)
    built.service.append("d1", [put("/name", "Alice")], contributor=ALICE)
    built.service.get("d1")  # fetches the five values
    calls = built.calls("fx.score@1")
    core = median_ms(lambda: built.service._core("d1", built.service.schemas.version_for("d1")))  # type: ignore[attr-defined]
    full = median_ms(lambda: built.service.get("d1"))
    assert full - core < 5.0, (core, full)
    assert built.calls("fx.score@1") == calls


def test_a_source_that_never_answers_ends_the_read_within_100_ms_of_the_fetch_deadline() -> None:
    fields = f'"/f": {deferred(hook="fx.never@1", refresh="per_fetch", ttl=chr(34) + "5m" + chr(34))}'
    built = build(
        MemoryStorage(), fields, extra=delayed("fx.never", None), sources=[], real_time=True, fetch_deadline=0.3
    )
    built.service.append("d1", [put("/name", "Alice")], contributor=ALICE)
    started = time.monotonic()
    built.service.get("d1")
    assert time.monotonic() - started < 0.3 + 0.1


def test_planning_ten_thousand_due_documents_takes_under_two_seconds() -> None:
    behaviors = BehaviorRegistry.with_builtins()
    behaviors.register_hook("fx.score", 1, lambda inputs, timeout: 1)
    fields = ", ".join(
        f'"/s{n}": {deferred(refresh="scheduled::{every: " + chr(34) + "1h" + chr(34) + "}")}' for n in range(5)
    )
    schema = load_schema(
        'schema::{id: "t", version: 1, tiers: [user, admin], fields: {"/name": {type: string, merge: lww}, '
        + fields
        + "}}",
        behaviors,
    )
    document = parse('{name: "Alice"}')
    now = datetime(2026, 10, 19, 12, 0, tzinfo=UTC)
    # a pure-CPU timing: the best of three runs, after a collection, so that a busy machine or the heap left by the tests before
    # it (the full suite builds documents of tens of thousands of entries) cannot fail it; it takes about one second alone
    best = float("inf")
    for _ in range(3):
        gc.collect()
        started = time.perf_counter()
        due = sum(len(plan(schema, document, {}, now, mode="scheduler").due()) for _ in range(10_000))
        assert due == 50_000
        best = min(best, time.perf_counter() - started)
        if best < 2.0:
            break
    assert best < 2.0
