"""SC-007: an uncached read of a document with 1,000 accepted entries and 5 computed fields takes under one second."""

import time

from aduku.ion_codec import Annotated
from aduku.storage.memory import MemoryStorage
from aduku.storage.sqlite import SqliteStorage
from tests.service_helpers import ALICE, BOB, JOB, make_service, put, schema_text

FIELDS = (
    '"/age": {type: int, merge: max}, "/name": {type: string, merge: lww}, "/tags": {type: "list<string>", merge: union},'
    ' "/shout": {type: string, computed: {macro: upper, args: ["/name"]}},'
    ' "/greeting": {type: string, computed: {macro: concat, args: [lit::"Hello, ", "/shout"]}},'
    ' "/count": {type: int, computed: {macro: len, args: ["/tags"]}},'
    ' "/next": {type: int, computed: {macro: sum, args: ["/age", lit::1]}},'
    ' "/known": {type: bool, computed: {macro: eq, args: ["/shout", "/shout"]}}'
)


def fill(storage):  # type: ignore[no-untyped-def]
    service = make_service(storage, schema=schema_text(fields=FIELDS))
    people = [(JOB, "backfill"), (ALICE, "user"), (BOB, "user")]
    for index in range(1000):
        who, tier = people[index % 3]
        path = ("/age", "/name", "/tags")[index % 3]
        value = {"/age": index, "/name": f"n{index}", "/tags": (str(index % 7),)}[path]
        service.append("d1", [put(path, value, tier=tier)], contributor=who)
    return service


def test_a_cold_read_with_five_computed_fields_takes_under_a_second(tmp_path) -> None:  # type: ignore[no-untyped-def]
    for storage in (MemoryStorage(), SqliteStorage(str(tmp_path / "perf.db"))):
        fill(storage)
        with storage.transaction() as tx:
            tx.drop_view("d1")
        fresh = make_service(storage, schema=schema_text(fields=FIELDS))
        before = fresh.derivations
        started = time.perf_counter()
        document = fresh.get("d1").document
        elapsed = time.perf_counter() - started
        assert fresh.derivations == before + 1
        computed = [key for key, value in document.items() if isinstance(value, Annotated)]
        assert sorted(computed) == ["count", "greeting", "known", "next", "shout"]
        assert elapsed < 1.0, f"{type(storage).__name__}: {elapsed:.2f}s"
        storage.close()
