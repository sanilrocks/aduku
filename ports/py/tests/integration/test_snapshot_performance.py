"""Feature 005, SC-001: the first write after a restart, the read after it and the first read after a restart cost about
the same on a 100,000-entry document as on a 100-entry one (the saved layers cover the old entries)."""

import time
from collections.abc import Callable

from aduku.storage.memory import MemoryStorage
from aduku.storage.sqlite import SqliteStorage
from tests.service_helpers import ALICE, put
from tests.snapshot_helpers import CountingStorage, bulk_history, restart, service_on

# seconds: below this a ratio of two timings means nothing. The guarantee that does not depend on timing is the count of ledger
# entries read (asserted below): a cold write and read touch only what the saved layers do not cover.
FLOOR = 0.05


def best(run: Callable[[], object], repeat: int = 3) -> float:
    times = []
    for _ in range(repeat):
        start = time.perf_counter()
        run()
        times.append(time.perf_counter() - start)
    return min(times)


def check(storage: object) -> None:
    counting = CountingStorage(storage)  # type: ignore[arg-type]
    db = service_on(counting)  # type: ignore[arg-type]
    bulk_history(counting, "small", 100)  # type: ignore[arg-type]
    bulk_history(counting, "big", 100_000, per_request=100)  # type: ignore[arg-type]
    db.rebuild()  # saves the layers (the documented migration for data written without them)
    results: dict[str, dict[str, float]] = {}
    for doc in ("small", "big"):
        cold = restart(db)
        counting.reset()
        first_read = best(lambda d=doc: restart(db).get(d))  # noqa: B023
        read_entries = counting.entries_read
        cold = restart(db)
        counting.reset()
        start = time.perf_counter()
        cold.append(doc, [put("/name", "w", "user")], contributor=ALICE)
        first_write = time.perf_counter() - start
        write_entries = counting.entries_read
        counting.reset()
        read_after = best(lambda d=doc, c=cold: c.get(d))  # noqa: B023
        results[doc] = {"read": first_read, "write": first_write, "after": read_after}
        if doc == "big":
            assert write_entries <= 2 and read_entries <= 2, (write_entries, read_entries)
    for step in ("read", "write", "after"):
        assert results["big"][step] <= max(5 * results["small"][step], FLOOR), (step, results)


def test_memory_backend() -> None:
    check(MemoryStorage())


def test_sqlite_backend(tmp_path) -> None:  # noqa: ANN001
    storage = SqliteStorage(str(tmp_path / "big.db"))
    try:
        check(storage)
    finally:
        storage.close()
