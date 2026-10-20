"""Feature 005: the new fields of an entry and of an entry input."""

from datetime import UTC, datetime

from aduku.model import AsOf, Entry, EntryInput


def test_new_entry_fields_default_to_nothing() -> None:
    entry = Entry("e", "d", 1, "r", "user", "/a", "set", 1, "c", datetime(2026, 1, 1, tzinfo=UTC), 1)
    assert entry.valid_time is None and entry.synthetic is None and entry.purged is False


def test_entry_input_takes_a_valid_time() -> None:
    moment = datetime(2024, 3, 1, tzinfo=UTC)
    assert EntryInput("user", "/a", value=1, valid_time=moment).valid_time == moment
    assert EntryInput("user", "/a", value=1).valid_time is None


def test_as_of_names_one_point_at_most_per_field() -> None:
    assert AsOf(seq=3).seq == 3 and AsOf(seq=3).tx_time is None
