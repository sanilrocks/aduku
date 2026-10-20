"""Feature 005 (FR-011, FR-035): `valid_time` on a contribution is checked at admission and read by nothing but `as_of`."""

from datetime import UTC, datetime, timedelta, timezone

import pytest

from aduku.errors import ErrorCode, RejectedError
from aduku.model import EntryInput
from aduku.storage.base import Storage
from tests.service_helpers import ALICE, make_service, put


def test_a_timezone_aware_valid_time_is_stored_in_utc_and_listed(backend: Storage) -> None:
    db = make_service(backend)
    plus_two = datetime(2024, 3, 1, 14, 0, tzinfo=timezone(timedelta(hours=2)))
    db.append("d", [put("/name", "ann", valid_time=plus_two), put("/age", 3)], contributor=ALICE)
    first, second = db.list_entries("d")
    assert first.valid_time == datetime(2024, 3, 1, 12, 0, tzinfo=UTC) and first.valid_time.utcoffset() == timedelta(0)  # type: ignore[union-attr]
    assert second.valid_time is None


@pytest.mark.parametrize("bad", [datetime(2024, 3, 1), "2024-03-01T00:00:00Z", 1709251200])
def test_anything_but_an_aware_timestamp_rejects_the_whole_request_and_records_nothing(
    backend: Storage, bad: object
) -> None:
    db = make_service(backend)
    with pytest.raises(RejectedError) as caught:
        db.append("d", [put("/age", 3), put("/name", "secret-name", valid_time=bad)], contributor=ALICE)
    reasons = caught.value.reasons
    assert [(r.code, r.entry_index, r.path) for r in reasons] == [(ErrorCode.BAD_VALID_TIME, 1, "/name")]
    assert "secret-name" not in reasons[0].message and str(bad) not in reasons[0].message
    assert db.last_seq("d") == 0 and db.list_entries("d") == []


def test_valid_time_changes_nothing_about_a_normal_read(backend: Storage) -> None:
    plain = make_service(backend)
    for doc, moment in (("a", None), ("b", datetime(2999, 1, 1, tzinfo=UTC))):  # a "future" valid_time is just a claim
        plain.append(
            doc, [put("/name", "x", valid_time=moment), put("/tags", ("t",), valid_time=moment)], contributor=ALICE
        )
    assert plain.get("a").document == plain.get("b").document


@pytest.mark.parametrize("op", ["layer", "purge", "increment", "reset"])
def test_a_contributor_cannot_send_any_operation_but_set_and_delete(backend: Storage, op: str) -> None:
    db = make_service(backend)
    with pytest.raises(RejectedError) as caught:
        db.append("d", [EntryInput("user", "/name", op, "x")], contributor=ALICE)  # type: ignore[arg-type]
    assert caught.value.reasons[0].code is ErrorCode.OP_NOT_ALLOWED
    assert db.last_seq("d") == 0
