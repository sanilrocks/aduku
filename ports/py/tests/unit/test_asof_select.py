"""Feature 005 (FR-013 to FR-016): which entries take part in a time-travel read. Pure."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from aduku.engine.asof import select
from aduku.errors import ErrorCode, RejectedError
from aduku.model import AsOf
from tests.helpers import T0, make_entry


def entries():  # noqa: ANN201
    # two requests: seqs 1-2 (r1) and 3-4 (r2); seq 3 was recorded before seq 2 (clock skew); valid times on some
    return [
        make_entry(
            "d", 1, request_id="r1", tx_time=T0 + timedelta(seconds=10), valid_time=datetime(2024, 1, 1, tzinfo=UTC)
        ),
        make_entry("d", 2, request_id="r1", tx_time=T0 + timedelta(seconds=30)),
        make_entry(
            "d", 3, request_id="r2", tx_time=T0 + timedelta(seconds=20), valid_time=datetime(2024, 6, 1, tzinfo=UTC)
        ),
        make_entry(
            "d", 4, request_id="r2", tx_time=T0 + timedelta(seconds=40), valid_time=datetime(2025, 1, 1, tzinfo=UTC)
        ),
    ]


def seqs(chosen) -> list[int]:  # noqa: ANN001
    return [e.seq for e in chosen]


def test_seq_takes_the_prefix_and_says_where_it_stands() -> None:
    chosen, info = select(entries(), AsOf(seq=3))
    assert seqs(chosen) == [1, 2, 3] and (info.kind, info.resolved_seq, info.included) == ("seq", 3, 3)
    assert info.inside_request is True  # seq 3 and 4 are one request


def test_seq_on_a_request_boundary_is_not_inside_a_request() -> None:
    _, info = select(entries(), AsOf(seq=2))
    assert info.inside_request is False and info.resolved_seq == 2


def test_before_the_first_entry_is_the_empty_document_at_seq_zero() -> None:
    chosen, info = select(entries(), AsOf(seq=0))
    assert chosen == [] and (info.resolved_seq, info.included, info.inside_request) == (0, 0, False)


def test_beyond_the_last_entry_reads_the_current_state_and_says_which_seq() -> None:
    chosen, info = select(entries(), AsOf(seq=99))
    assert seqs(chosen) == [1, 2, 3, 4] and info.resolved_seq == 4 and info.point == 99


def test_tx_time_uses_the_seq_the_caller_resolved_and_then_acts_as_that_seq() -> None:
    moment = T0 + timedelta(seconds=25)
    chosen, info = select(entries(), AsOf(tx_time=moment), tx_seq=3)
    assert seqs(chosen) == [1, 2, 3] and (info.kind, info.resolved_seq, info.point) == ("tx_time", 3, moment)


def test_valid_time_leaves_out_entries_valid_later_and_keeps_those_with_no_valid_time() -> None:
    chosen, info = select(entries(), AsOf(valid_time=datetime(2024, 7, 1, tzinfo=UTC)))
    assert seqs(chosen) == [1, 2, 3]  # seq 4 is valid from 2025; seq 2 states no time and always takes part
    assert (info.kind, info.included, info.resolved_seq) == ("valid_time", 3, 3)
    chosen, info = select(entries(), AsOf(valid_time=datetime(2023, 1, 1, tzinfo=UTC)))
    assert seqs(chosen) == [2] and info.resolved_seq == 2


def test_valid_time_compares_instants_not_offsets() -> None:
    from datetime import timezone

    plus_one = timezone(timedelta(hours=1))
    chosen, _ = select(entries(), AsOf(valid_time=datetime(2024, 6, 1, 0, 30, tzinfo=plus_one)))  # 2024-05-31 23:30 UTC
    assert 3 not in seqs(chosen)


@pytest.mark.parametrize(
    "asof",
    [
        AsOf(),
        AsOf(seq=1, valid_time=datetime(2024, 1, 1, tzinfo=UTC)),
        AsOf(seq=1, tx_time=T0),
        AsOf(tx_time=T0, valid_time=T0),
    ],
)
def test_zero_or_several_points_are_ambiguous(asof: AsOf) -> None:
    with pytest.raises(RejectedError) as caught:
        select(entries(), asof)
    assert caught.value.code is ErrorCode.AS_OF_AMBIGUOUS


def test_a_naive_moment_is_refused() -> None:
    with pytest.raises(RejectedError) as caught:
        select(entries(), AsOf(valid_time=datetime(2024, 1, 1)))
    assert caught.value.code is ErrorCode.AS_OF_AMBIGUOUS
    _ = replace
