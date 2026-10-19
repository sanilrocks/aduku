"""US3 / FR-029 / SC-011: requests that need the same value at the same moment share one call to the source."""

import threading

from aduku.storage.base import Storage
from tests.deferred_helpers import build, deferred, deferred_value, delayed
from tests.service_helpers import ALICE, put


def read_together(built, doc: str, readers: int):  # type: ignore[no-untyped-def]
    start = threading.Barrier(readers)
    results: list[object] = [None] * readers

    def read(index: int) -> None:
        start.wait()
        results[index] = built.service.get(doc)

    threads = [threading.Thread(target=read, args=(n,)) for n in range(readers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)
    return results


def test_readers_at_the_same_moment_share_one_call_and_all_get_the_value(backend: Storage) -> None:
    fields = f'"/score": {deferred(hook="fx.slow@1")}'
    built = build(backend, fields, extra=delayed("fx.slow", 0.3, 7), sources=[], real_time=True)
    built.service.append("d1", [put("/name", "Alice")], contributor=ALICE)
    results = read_together(built, "d1", 6)
    assert [deferred_value(r, "score") for r in results] == [7] * 6
    assert built.calls("fx.slow@1") == 1


def test_a_different_input_is_a_different_call(backend: Storage) -> None:
    fields = f'"/score": {deferred(hook="fx.slow@1")}'
    built = build(backend, fields, extra=delayed("fx.slow", 0.2, 7), sources=[], real_time=True)
    built.service.append("d1", [put("/name", "Alice")], contributor=ALICE)
    built.service.append("d2", [put("/name", "Bob")], contributor=ALICE)
    first = threading.Thread(target=built.service.get, args=("d1",))
    second = threading.Thread(target=built.service.get, args=("d2",))
    first.start()
    second.start()
    first.join(10)
    second.join(10)
    assert built.calls("fx.slow@1") == 2  # two documents, two questions: nothing is shared


def test_the_followers_wait_no_longer_than_their_own_deadline(backend: Storage) -> None:
    fields = f'"/score": {deferred(hook="fx.never@1")}'
    built = build(backend, fields, extra=delayed("fx.never", None), sources=[], real_time=True, fetch_deadline=0.2)
    built.service.append("d1", [put("/name", "Alice")], contributor=ALICE)
    import time

    started = time.monotonic()
    results = read_together(built, "d1", 4)
    assert time.monotonic() - started < 0.2 + 0.5
    assert all(r.deferred[0].status == "error" for r in results) and built.calls("fx.never@1") == 1  # type: ignore[attr-defined]
