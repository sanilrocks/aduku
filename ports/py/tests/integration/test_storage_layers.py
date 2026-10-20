"""Feature 005: saved layers, the epoch, the until-seq read and tx-time resolution, on both backends."""

from datetime import UTC, datetime, timedelta

from aduku.model import Entry
from aduku.storage.base import LayerRow, RequestRecord, Storage, ViewRow

T0 = datetime(2026, 1, 1, tzinfo=UTC)


def row(tier: str = "user", tier_seq: int = 3, epoch: int = 0, state: str = "[]") -> LayerRow:
    return LayerRow("d", tier, tier_seq, 1, "fp", epoch, state)


def test_layers_are_stored_per_tier_and_listed_in_tier_order(backend: Storage) -> None:
    with backend.transaction() as tx:
        tx.put_layers([row("user"), row("admin", 5)])
    with backend.read() as tx:
        assert [(r.tier, r.tier_seq) for r in tx.get_layers("d")] == [("admin", 5), ("user", 3)]
        assert tx.get_layers("other") == []


def test_a_layer_is_replaced_only_by_an_equal_or_higher_tier_seq_of_the_same_epoch(backend: Storage) -> None:
    with backend.transaction() as tx:
        tx.put_layers([row(tier_seq=5, state="[1]")])
        tx.put_layers([row(tier_seq=4, state="[2]")])  # a slow writer: ignored
    with backend.read() as tx:
        assert tx.get_layers("d")[0].state == "[1]"
    with backend.transaction() as tx:
        tx.put_layers([row(tier_seq=5, state="[3]")])  # equal: replaces
        tx.put_layers([row(tier_seq=1, epoch=1, state="[4]")])  # a newer epoch always wins
        tx.put_layers([row(tier_seq=9, epoch=0, state="[5]")])  # an older epoch never does
    with backend.read() as tx:
        saved = tx.get_layers("d")[0]
    assert (saved.state, saved.epoch) == ("[4]", 1)


def test_drop_layers_and_rollback(backend: Storage) -> None:
    with backend.transaction() as tx:
        tx.put_layers([row()])
    try:
        with backend.transaction() as tx:
            tx.drop_layers("d")
            raise RuntimeError("abort")
    except RuntimeError:
        pass
    with backend.read() as tx:
        assert len(tx.get_layers("d")) == 1
    with backend.transaction() as tx:
        tx.drop_layers("d")
    with backend.read() as tx:
        assert tx.get_layers("d") == []


def test_epoch_starts_at_zero_grows_and_rolls_back(backend: Storage) -> None:
    with backend.read() as tx:
        assert tx.epoch("d") == 0
    with backend.transaction() as tx:
        assert tx.bump_epoch("d") == 1
        assert tx.bump_epoch("d") == 2
    try:
        with backend.transaction() as tx:
            tx.bump_epoch("d")
            raise RuntimeError("abort")
    except RuntimeError:
        pass
    with backend.read() as tx:
        assert tx.epoch("d") == 2 and tx.epoch("e") == 0


def _history(backend: Storage, stamps: list[datetime]) -> None:
    entries = [
        Entry(f"e{i + 1}", "d", i + 1, "r1", "user" if i % 2 == 0 else "admin", "/a", "set", i, "c", stamp, 1)
        for i, stamp in enumerate(stamps)
    ]
    with backend.transaction() as tx:
        tx.append_request(RequestRecord("r1", "d", "c"), entries)


def test_tier_last_seq_and_until_seq(backend: Storage) -> None:
    _history(backend, [T0 + timedelta(seconds=i) for i in range(6)])
    with backend.read() as tx:
        assert tx.tier_last_seq("d", "user", 6) == 5
        assert tx.tier_last_seq("d", "user", 4) == 3
        assert tx.tier_last_seq("d", "admin", 1) == 0
        assert [e.seq for e in tx.entries("d", 1, until_seq=4)] == [2, 3, 4]
        assert [e.seq for e in tx.entries("d", 0, 2, until_seq=4)] == [1, 2]
        assert [e.seq for e in tx.entries("d", 4, until_seq=None)] == [5, 6]


def test_seq_at_tx_time_is_a_maximum_not_a_search(backend: Storage) -> None:
    # recording times that do not grow with seq (clock skew): seq 3 was recorded before seq 2
    stamps = [T0 + timedelta(seconds=s) for s in (10, 30, 20, 40)]
    _history(backend, stamps)
    with backend.read() as tx:
        assert tx.seq_at_tx_time("d", T0 + timedelta(seconds=5)) == 0
        assert tx.seq_at_tx_time("d", T0 + timedelta(seconds=10)) == 1
        assert tx.seq_at_tx_time("d", T0 + timedelta(seconds=25)) == 3  # seq 3 (20s) is the highest at or before 25s
        assert tx.seq_at_tx_time("d", T0 + timedelta(seconds=45)) == 4


def test_the_view_carries_an_epoch_that_defaults_to_zero(backend: Storage) -> None:
    with backend.transaction() as tx:
        tx.put_view(ViewRow("d", 1, 1, "fp", "{}", "[]", "{}"))
        tx.put_view(ViewRow("e", 1, 1, "fp", "{}", "[]", "{}", 0, 3))
    with backend.read() as tx:
        assert tx.get_view("d").epoch == 0  # type: ignore[union-attr]
        assert tx.get_view("e").epoch == 3  # type: ignore[union-attr]
