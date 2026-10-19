"""SC-010 and FR-021: a hook never runs while the write lock is held, and never delays another document.

The slow hook is held open on an event and released by the test, rather than sleeping for the 2 seconds of SC-010: the
property checked is the same (another document's write is not delayed) and the suite stays fast.
"""

import threading
import time

from aduku.behaviors.registry import BehaviorRegistry
from aduku.schema.loader import load_schema
from aduku.schema.registry import SchemaRegistry
from aduku.service import Aduku
from aduku.storage.base import SequentialIds, Storage
from tests.integration.test_concurrency_hardening import Spy
from tests.service_helpers import ALICE, put, schema_text

FIELDS = (
    '"/country": {type: string, merge: lww}, "/name": {type: string, merge: lww},'
    ' "/taxId": {type: string, computed: {impure: {hook: "fx.slow@1", depends: ["/country"], reason: "registry",'
    ' timeout: "5s", retry: {max: 0, backoff: "10ms"}, deadline: "9s", recompute_by: [admin]}}}'
)


def service_with(storage, hook):  # type: ignore[no-untyped-def]
    behaviors = BehaviorRegistry.with_builtins()
    behaviors.register_hook("fx.slow", 1, hook)
    registry = SchemaRegistry()
    registry.put(load_schema(schema_text(fields=FIELDS), behaviors))
    registry.activate(1)
    return Aduku(storage, registry, behaviors=behaviors, ids=SequentialIds("lock"))


def test_a_hook_is_never_called_inside_a_transaction(backend: Storage) -> None:
    spy = Spy(backend)
    seen: list[tuple[bool, bool]] = []

    def probe(inputs: dict[str, object], timeout: float) -> object:
        seen.append((spy.reading, spy.writing))
        return "ok"

    service = service_with(spy, probe)
    service.append("d1", [put("/country", "US")], contributor=ALICE)
    assert seen == [(False, False)]  # not in a read, and not in a write transaction


def test_a_slow_hook_does_not_delay_a_write_to_another_document(backend: Storage) -> None:
    started, release = threading.Event(), threading.Event()

    def slow(inputs: dict[str, object], timeout: float) -> object:
        started.set()
        release.wait(5)
        return "tax"

    service = service_with(backend, slow)
    worker = threading.Thread(target=lambda: service.append("a", [put("/country", "US")], contributor=ALICE))
    worker.start()
    assert started.wait(5)
    begun = time.monotonic()
    service.append("b", [put("/name", "Pat")], contributor=ALICE)  # another document, while the hook for "a" is running
    elapsed = time.monotonic() - begun
    release.set()
    worker.join(5)
    assert elapsed < 0.1 and service.last_seq("a") == 1 and service.last_seq("b") == 1


def test_a_document_that_changed_while_the_hook_ran_is_derived_again_without_a_second_call(backend: Storage) -> None:
    started, release = threading.Event(), threading.Event()
    counted: list[int] = []

    def slow(inputs: dict[str, object], timeout: float) -> object:
        counted.append(1)
        started.set()
        release.wait(5)
        return "tax-" + str(inputs["/country"])

    service = service_with(backend, slow)
    outcome: list[object] = []
    worker = threading.Thread(
        target=lambda: outcome.append(service.append("d1", [put("/country", "US")], contributor=ALICE))
    )
    worker.start()
    assert started.wait(5)
    service.append("d1", [put("/name", "Pat")], contributor=ALICE)  # lands while the hook is still running
    release.set()
    worker.join(5)
    assert len(outcome) == 1 and counted == [1]  # one call, though the request had to be derived again
    assert [(e.seq, e.path) for e in service.list_entries("d1")] == [(1, "/name"), (2, "/country")]
    assert service.get("d1").document["taxId"].value == "tax-US"


# -- one call per input, even when several callers need it at the same moment -----------------------------------------


def _stored(storage, doc="d1"):  # type: ignore[no-untyped-def]
    with storage.read() as tx:
        return tx.list_effects(doc)


def test_concurrent_first_reads_share_one_call_and_leave_one_record(backend: Storage) -> None:
    from aduku.schema.loader import load_schema

    calls: list[int] = []
    gate = threading.Event()

    def slow(inputs: dict[str, object], timeout: float) -> object:
        calls.append(1)
        gate.wait(5)
        return "v"

    behaviors = BehaviorRegistry.with_builtins()
    behaviors.register_hook("fx.slow", 1, slow)
    registry = SchemaRegistry()
    registry.put(load_schema(schema_text(fields='"/country": {type: string, merge: lww}'), behaviors))
    registry.put(load_schema(schema_text(version=2, fields=FIELDS), behaviors))
    registry.activate(1)
    service = Aduku(backend, registry, behaviors=behaviors, ids=SequentialIds("sf"))
    service.append("d1", [put("/country", "US")], contributor=ALICE)  # written before the impure field existed
    registry.activate(2)
    readers = [threading.Thread(target=lambda: service.get("d1")) for _ in range(6)]
    for reader in readers:
        reader.start()
    time.sleep(0.2)  # every reader is now waiting on the same call
    gate.set()
    for reader in readers:
        reader.join(5)
    assert len(calls) == 1 and len(_stored(backend)) == 1


def test_concurrent_requests_with_the_same_inputs_share_one_call(backend: Storage) -> None:
    calls: list[int] = []
    gate = threading.Event()

    def slow(inputs: dict[str, object], timeout: float) -> object:
        calls.append(1)
        gate.wait(5)
        return "tax-" + str(inputs["/country"])

    service = service_with(backend, slow)
    workers = [
        threading.Thread(target=lambda n=n: service.append("d1", [put("/country", "US")], contributor=ALICE))
        for n in range(3)
    ]
    for worker in workers:
        worker.start()
    time.sleep(0.2)
    gate.set()
    for worker in workers:
        worker.join(5)
    assert len(calls) == 1 and len(_stored(backend)) == 1
    assert service.get("d1").document["taxId"].value == "tax-US"
