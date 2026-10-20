"""Feature 005 (FR-012): a request without `valid_time` hashes exactly as it did before; with one it differs and is stable."""

from datetime import UTC, datetime, timedelta, timezone

from aduku.engine.request_hash import request_hash
from aduku.model import EntryInput

# computed with Feature 1 to 4's code, before `valid_time` existed
PINNED = {
    "one": "ad2aa54afd076f16a9b4d8d6659d00d2484e7c9d498c079b723baaa490e09aa4",
    "two_meta_idem": "9fa3b6c9a396da33e21f5718a5f3eefffd6d4c14a245b6c0d323baa3c93c8f92",
}


def test_requests_without_valid_time_keep_their_hash() -> None:
    assert request_hash("d1", "alice", False, [EntryInput("user", "/name", "set", "Alice")]) == PINNED["one"]
    two = [
        EntryInput("user", "/age", "set", 3, idem_key="E1", meta={"ticket": "T-1"}),
        EntryInput("admin", "/name", "delete"),
    ]
    assert request_hash("d1", "alice", True, two) == PINNED["two_meta_idem"]


def test_a_valid_time_changes_the_hash_and_is_stable() -> None:
    plain = request_hash("d1", "alice", False, [EntryInput("user", "/name", "set", "Alice")])
    moment = datetime(2024, 3, 1, tzinfo=UTC)
    with_time = [EntryInput("user", "/name", "set", "Alice", valid_time=moment)]
    assert request_hash("d1", "alice", False, with_time) != plain
    assert request_hash("d1", "alice", False, with_time) == request_hash("d1", "alice", False, with_time)


def test_the_same_instant_in_another_offset_hashes_alike_and_another_instant_does_not() -> None:
    utc = datetime(2024, 3, 1, 12, tzinfo=UTC)
    plus_two = utc.astimezone(timezone(timedelta(hours=2)))
    h = lambda t: request_hash("d", "c", False, [EntryInput("user", "/a", "set", 1, valid_time=t)])  # noqa: E731
    assert h(utc) == h(plus_two) and h(utc) != h(utc + timedelta(seconds=1))
