"""SC-006: an uncached read of a document with 1,000 accepted entries completes in under one second."""

import time

from aduku.storage.memory import MemoryStorage
from aduku.storage.sqlite import SqliteStorage
from tests.service_helpers import ALICE, BOB, JOB, make_service, put


def fill(storage):  # type: ignore[no-untyped-def]
    service = make_service(storage)
    people = [(JOB, "backfill"), (ALICE, "user"), (BOB, "user")]
    for index in range(1000):  # 1,000 single-entry requests: the worst case for anything that replays per request
        who, tier = people[index % 3]
        path = ("/age", "/name", "/tags")[index % 3]
        value = {"/age": index, "/name": f"n{index}", "/tags": (str(index % 7),)}[path]
        service.append("d1", [put(path, value, tier=tier)], contributor=who)
    return service


def test_a_cold_read_of_1000_entries_takes_under_a_second(tmp_path) -> None:  # type: ignore[no-untyped-def]
    for storage in (MemoryStorage(), SqliteStorage(str(tmp_path / "perf.db"))):
        fill(storage)
        with storage.transaction() as tx:
            tx.drop_view("d1")  # appends store the view, so drop it: the read must derive from the ledger
        fresh = make_service(storage)  # and nothing is cached in memory either
        derivations = fresh.derivations
        started = time.perf_counter()
        document = fresh.get("d1").document
        elapsed = time.perf_counter() - started
        assert fresh.derivations == derivations + 1  # it really derived
        assert document["age"] == 999 and sorted(document["tags"]) == [str(n) for n in range(7)]
        assert elapsed < 1.0, f"{type(storage).__name__}: {elapsed:.2f}s"
        storage.close()
