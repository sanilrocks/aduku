"""Point selection for time-travel reads (feature 005, research R-11). Pure.

A read `as_of` names exactly one point. A seq (or a recording time, which the caller resolves to a seq) is a prefix of the
ledger; a moment in the world is a filter on the entries' `valid_time`. The selected entries are then derived with the
engine's own replay, so a historical read is computed by the same code as a live one.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import NamedTuple

from aduku.errors import ErrorCode, RejectedError
from aduku.ion_codec import to_utc
from aduku.model import AsOf, AsOfInfo, Entry

__all__ = ["Selection", "point_of", "select"]


class Selection(NamedTuple):
    entries: list[Entry]
    info: AsOfInfo


def point_of(asof: AsOf) -> tuple[str, object]:
    """The one point of `asof` as (kind, value). Zero or several points, or a moment with no offset, are refused."""
    given = [
        (kind, value)
        for kind, value in (("seq", asof.seq), ("tx_time", asof.tx_time), ("valid_time", asof.valid_time))
        if value is not None
    ]
    if len(given) != 1:
        raise RejectedError(
            ErrorCode.AS_OF_AMBIGUOUS, message="an as_of read takes exactly one point: a seq, a tx_time or a valid_time"
        )
    kind, value = given[0]
    if kind != "seq":
        if not isinstance(value, datetime) or value.utcoffset() is None:
            raise RejectedError(ErrorCode.AS_OF_AMBIGUOUS, message=f"the {kind} point must carry a timezone offset")
    elif isinstance(value, bool) or not isinstance(value, int):
        raise RejectedError(ErrorCode.AS_OF_AMBIGUOUS, message="the seq point must be a whole number")
    return kind, value


def select(entries: Sequence[Entry], asof: AsOf, *, tx_seq: int | None = None) -> Selection:
    """The entries of `entries` (in seq order) that take part at `asof`, and a description of where that stands.

    For a `tx_time` point the caller passes `tx_seq`: the highest seq recorded at or before it (a maximum over all entries,
    because recording times need not grow with seq).
    """
    kind, value = point_of(asof)
    ordered = sorted(entries, key=lambda e: e.seq)
    last = ordered[-1].seq if ordered else 0
    if kind == "valid_time":
        assert value is not None
        limit = to_utc(value)
        chosen = [e for e in ordered if e.valid_time is None or to_utc(e.valid_time) <= limit]
        resolved = chosen[-1].seq if chosen else 0
        return Selection(chosen, AsOfInfo("valid_time", value, resolved, len(chosen), False))
    if kind == "tx_time":
        assert tx_seq is not None, "the caller resolves a tx_time to a seq"
        wanted = tx_seq
    else:
        assert isinstance(value, int)
        wanted = value
    resolved = max(0, min(wanted, last))
    chosen = [e for e in ordered if e.seq <= resolved]
    after = next((e for e in ordered if e.seq > resolved), None)
    inside = bool(chosen) and after is not None and chosen[-1].request_id == after.request_id
    return Selection(chosen, AsOfInfo(kind, value, resolved, len(chosen), inside))  # type: ignore[arg-type]
