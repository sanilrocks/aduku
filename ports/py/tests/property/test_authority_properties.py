"""Feature 004, SC-004, SC-005: a rejected request leaves no trace, and the non-rejection flag never passes authority."""

from __future__ import annotations

import tempfile

from hypothesis import given
from hypothesis import strategies as st

from aduku.errors import ErrorCode, RejectedError
from aduku.model import EntryInput, Identity
from aduku.storage.base import Storage
from aduku.storage.memory import MemoryStorage
from aduku.storage.sqlite import SqliteStorage
from tests.rule_helpers import build

LISTS = st.sampled_from(
    ["", "authority: [admin]", "authority: [finance]", "authority: [user, finance]", "authority: [ops]"]
)
PEOPLE = [
    Identity("u", ("user",)),
    Identity("a", ("user", "admin")),
    Identity("f", ("user",), ("finance",)),
    Identity("o", ("user",), ("ops",)),
    Identity("j", ("backfill",)),
]
PATHS = ["/p", "/p/x", "/p/y", "/q", "/q/z"]


def fields(lists: list[str]) -> str:
    spec = lambda extra: "{type: int, merge: max" + (f", {extra}" if extra else "") + "}"  # noqa: E731
    return (
        f'"/p": {{type: struct, merge: struct{", " + lists[0] if lists[0] else ""}}}, "/p/x": {spec(lists[1])}, "/p/y": {spec(lists[2])}, '
        f'"/q": {{type: struct, merge: struct{", " + lists[3] if lists[3] else ""}}}, "/q/z": {spec(lists[4])}'
    )


@st.composite
def requests(draw) -> list[EntryInput]:  # type: ignore[no-untyped-def]
    entries = []
    for _ in range(draw(st.integers(1, 3))):
        path = draw(st.sampled_from(PATHS))
        tier = draw(st.sampled_from(["backfill", "user", "admin"]))
        if draw(st.booleans()) and path in ("/p/x", "/p/y", "/q/z"):
            entries.append(EntryInput(tier, path, "set", draw(st.integers(0, 9))))
        else:
            entries.append(EntryInput(tier, path, "delete"))
    return entries


def snapshot(service) -> tuple:  # type: ignore[no-untyped-def]
    got = service.get("d1")
    return (tuple((e.seq, e.path, e.op) for e in service.list_entries("d1")), str(got.document), got.model_errors)


def check(storage: Storage, other: Storage, lists: list[str], who: Identity, entries: list[EntryInput]) -> None:
    service = build(storage, fields(lists), prefix="a")
    before = snapshot(service)
    try:
        service.append("d1", entries, contributor=who)
        accepted = True
    except RejectedError as error:
        accepted = False
        unauthorized = any(r.code is ErrorCode.NOT_AUTHORIZED for r in error.reasons)
        assert snapshot(service) == before, "a rejected request left a trace"  # SC-004
    if accepted:
        return
    flagged = build(other, fields(lists), prefix="b")
    try:
        flagged.append("d1", entries, contributor=who, lenient=True)
    except RejectedError:
        return
    assert not unauthorized, "the non-rejection flag passed an authority check"  # SC-005


@given(st.lists(LISTS, min_size=5, max_size=5), st.sampled_from(PEOPLE), requests())
def test_in_memory(lists, who, entries) -> None:  # type: ignore[no-untyped-def]
    check(MemoryStorage(), MemoryStorage(), lists, who, entries)


@given(st.lists(LISTS, min_size=5, max_size=5), st.sampled_from(PEOPLE), requests())
def test_in_sqlite(lists, who, entries) -> None:  # type: ignore[no-untyped-def]
    with tempfile.TemporaryDirectory() as folder:
        one, two = SqliteStorage(f"{folder}/a.db"), SqliteStorage(f"{folder}/b.db")
        try:
            check(one, two, lists, who, entries)
        finally:
            one.close()
            two.close()
