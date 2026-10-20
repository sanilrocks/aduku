"""Feature 006 (SC-005, E1): an activation costs the same on a large store as on a small one, a limited dry run reads a proportional
part, and the shared state adds one single-row read to a request."""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta

import pytest

from aduku.model import Entry
from aduku.storage.base import RequestRecord, Storage
from aduku.storage.memory import MemoryStorage
from aduku.storage.sqlite import SqliteStorage
from tests.schema_helpers import Env, field_text, versions_text
from tests.service_helpers import ALICE, put
from tests.snapshot_helpers import CountingStorage

BASE = field_text()
COMPATIBLE = BASE + ', "/nick": {type: string, merge: lww}'
TEXTS = versions_text(BASE, COMPATIBLE)
EPOCH = datetime(2026, 1, 1, tzinfo=UTC)


def fill(storage: Storage, count: int) -> None:
    """`count` documents of one entry each, written through the storage interface (no admission, no derivation)."""
    with storage.transaction() as tx:
        for number in range(count):
            doc = f"doc-{number:06d}"
            entry = Entry(
                id=f"e-{number}", doc=doc, seq=1, request_id=f"r-{number}", tier="user", path="/name", op="set",
                value=f"n{number}", contributor="bulk", tx_time=EPOCH + timedelta(seconds=number), schema_version=1,
            )  # fmt: skip
            tx.append_request(RequestRecord(f"r-{number}", doc, "bulk"), [entry])


def activation_seconds(storage: Storage, version: int = 2) -> tuple[float, int]:
    counting = CountingStorage(storage)
    db = Env(counting, *TEXTS).service(active=1)  # type: ignore[arg-type]
    counting.reset()
    started = time.perf_counter()
    db.activate_schema(version, operator="alice")
    return time.perf_counter() - started, counting.entries_read


def best_of(make, count: int, runs: int = 3) -> tuple[float, int]:  # type: ignore[no-untyped-def]
    results = []
    for _ in range(runs):
        storage = make()
        fill(storage, count)
        results.append(activation_seconds(storage))
        storage.close()
    return min(t for t, _ in results), max(n for _, n in results)


@pytest.mark.parametrize(
    ("make", "small", "large"),
    [(MemoryStorage, 100, 100_000), (lambda: SqliteStorage(":memory:"), 100, 10_000)],
    ids=["memory", "sqlite"],
)
def test_activating_a_compatible_version_costs_the_same_on_a_large_store(make, small: int, large: int) -> None:  # type: ignore[no-untyped-def]
    few, few_read = best_of(make, small)
    many, many_read = best_of(make, large)
    assert few_read == many_read == 0  # no ledger entry is read
    assert many < max(few * 3, 0.005), (few, many)  # about the same: not growing with the number of documents


def test_a_limited_dry_run_reads_a_proportional_part_of_the_entries() -> None:
    storage = MemoryStorage()
    fill(storage, 10_000)
    counting = CountingStorage(storage)
    db = Env(counting, *TEXTS).service(active=1)  # type: ignore[arg-type]
    counting.reset()
    limited = db.plan_schema(2, limit=1_000)
    part = counting.entries_read
    counting.reset()
    everything = db.plan_schema(2)
    whole = counting.entries_read
    assert (limited.examined, limited.left_out, everything.examined, everything.left_out) == (1_000, 9_000, 10_000, 0)
    assert whole == 10_000 and 0.09 <= part / whole <= 0.11, (part, whole)


class Reads:
    """Counts the read transactions of a storage."""

    def __init__(self, inner: Storage) -> None:
        self.inner, self.reads = inner, 0

    def transaction(self):  # type: ignore[no-untyped-def]
        return self.inner.transaction()

    def read(self):  # type: ignore[no-untyped-def]
        self.reads += 1
        return self.inner.read()

    def audit_backend(self):  # type: ignore[no-untyped-def]
        return self.inner.audit_backend()

    def close(self) -> None:
        self.inner.close()


def test_a_request_on_a_bound_registry_adds_exactly_one_generation_read(backend: Storage) -> None:
    reads = []
    for shared in (False, True):
        counted = Reads(MemoryStorage() if not shared else backend)
        db = Env(counted, *TEXTS).service(active=1, schema_state=shared)  # type: ignore[arg-type]
        db.append(f"d{shared}", [put("/name", "A")], contributor=ALICE)
        counted.reads = 0
        db.get(f"d{shared}")
        db.get(f"d{shared}")
        reads.append(counted.reads)
    assert reads[1] - reads[0] == 2  # one generation read for each of the two requests
