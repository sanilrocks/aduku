"""Schema and generators shared by the engine property tests."""

from __future__ import annotations

from typing import Any

from hypothesis import strategies as st

from aduku.ion_codec import FrozenStruct
from aduku.model import Entry
from tests.helpers import T0, make_entry

TIERS = ("backfill", "user", "admin")
ints = st.integers(-3, 3)

FIELDS = """
  "/name": {type: string, merge: lww},
  "/age": {type: int, merge: max},
  "/low": {type: int, merge: min},
  "/tags": {type: "list<string>", merge: union},
  "/address": {type: struct, merge: struct},
  "/address/city": {type: string, merge: lww},
  "/address/zip": {type: string, merge: lww},
  "/address/n": {type: int, merge: max},
  "/address/geo": {type: struct, merge: struct},
  "/address/geo/lat": {type: int, merge: lww},
  "/address/geo/lng": {type: int, merge: lww}
"""

# The same fields with defaults and delete fallbacks, for the property tests of the layering.
PROPERTY_FIELDS = (
    FIELDS.replace(
        '"/name": {type: string, merge: lww}',
        '"/name": {type: string, merge: lww, default: "none", on_delete: default}',
    )
    .replace('"/age": {type: int, merge: max}', '"/age": {type: int, merge: max, default: 0}')
    .replace(
        '"/address/n": {type: int, merge: max}', '"/address/n": {type: int, merge: max, default: 1, on_delete: default}'
    )
)

geo = st.fixed_dictionaries({}, optional={"lat": ints, "lng": ints}).map(FrozenStruct)
address = st.fixed_dictionaries(
    {},
    optional={
        "city": st.sampled_from(["Paris", "Rome", "Oslo"]),
        "zip": st.sampled_from(["1", "2"]),
        "n": ints,
        "geo": geo,
    },
).map(FrozenStruct)

VALUES: dict[str, st.SearchStrategy[Any]] = {
    "/name": st.text("abc", max_size=3),
    "/age": ints,
    "/low": ints,
    "/tags": st.lists(st.sampled_from(["x", "y", "z"]), max_size=3).map(tuple),
    "/address": address,
    "/address/city": st.sampled_from(["Paris", "Rome", "Oslo"]),
    "/address/zip": st.sampled_from(["1", "2"]),
    "/address/n": ints,
    "/address/geo": geo,
    "/address/geo/lat": ints,
    "/address/geo/lng": ints,
}


@st.composite
def contribution(draw: st.DrawFn) -> tuple[str, str, str, Any]:
    path = draw(st.sampled_from(sorted(VALUES)))
    tier = draw(st.sampled_from(TIERS))
    if draw(st.integers(0, 4)) == 0:
        return (tier, path, "delete", None)
    return (tier, path, "set", draw(VALUES[path]))


@st.composite
def ledger(draw: st.DrawFn, max_size: int = 14) -> list[Entry]:
    """Accepted entries in arrival order: seq is the position, the tier is whatever the writer used."""
    rows = draw(st.lists(contribution(), min_size=0, max_size=max_size))
    return [
        make_entry(seq=index, request_id=f"r{index}", tier=tier, path=path, op=op, value=value, tx_time=T0)
        for index, (tier, path, op, value) in enumerate(rows, start=1)
    ]
