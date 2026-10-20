"""Feature 006 (US4): an entry a version cannot use is left out and says why, with the document and in `explain`."""

from __future__ import annotations

from aduku.storage.base import Storage
from tests.schema_helpers import Env, field_text, versions_text
from tests.service_helpers import ALICE, put

V1 = field_text(', "/nick": {type: string, merge: lww}')
# /nick became computed; /age now needs a string
V2 = field_text(', "/nick": {type: string, computed: {macro: upper, args: ["/name"]}}').replace(
    '"/age": {type: int, merge: max}', '"/age": {type: string, merge: lww}'
)


def service(backend: Storage):  # type: ignore[no-untyped-def]
    db = Env(backend, *versions_text(V1, V2)).service(active=1)
    db.append("d", [put("/name", "A"), put("/age", 3), put("/nick", "n")], contributor=ALICE)
    return db


def test_reading_under_the_version_that_wrote_the_entries_shadows_nothing(backend: Storage) -> None:
    db = service(backend)
    result = db.get("d")
    assert result.skipped == () and result.shadowed == ()


def test_a_version_that_shadows_entries_names_each_one_with_its_reason(backend: Storage) -> None:
    db = service(backend)
    db.activate_schema(2, operator="alice", acknowledge_breaking=True)
    result = db.get("d")
    assert result.skipped == (2, 3)
    assert [(s.seq, s.tier, s.path, s.reason) for s in result.shadowed] == [
        (2, "user", "/age", "no_longer_conforms"),
        (3, "user", "/nick", "now_derived"),
    ]
    assert result.shadowed[0].detail == "string" and result.shadowed[1].detail
    assert {entry.id for entry in db.list_entries("d") if entry.seq in (2, 3)} == {s.entry_id for s in result.shadowed}
    assert "age" not in result.document and result.document["name"] == "A"


def test_the_reasons_come_with_every_read_of_the_document_and_the_write_view(backend: Storage) -> None:
    db = service(backend)
    db.activate_schema(2, operator="alice", acknowledge_breaking=True)
    again = db.append("d", [put("/name", "B")], contributor=ALICE)
    assert again.view is not None and [s.seq for s in again.view.shadowed] == [2, 3]
    assert db.get("d", consistency="fresh").shadowed == again.view.shadowed


def test_rolling_back_shows_no_shadowed_entries_again(backend: Storage) -> None:
    db = service(backend)
    db.activate_schema(2, operator="alice", acknowledge_breaking=True)
    assert db.get("d").shadowed
    db.activate_schema(1, operator="alice")
    assert db.get("d").shadowed == () and db.get("d").skipped == ()


def test_a_pinned_document_is_read_without_shadowing_while_others_are_shadowed(backend: Storage) -> None:
    db = service(backend)
    db.append("e", [put("/name", "E"), put("/age", 4)], contributor=ALICE)
    db.activate_schema(2, operator="alice", acknowledge_breaking=True)
    db.pin_document("d", 1, operator="alice")
    assert db.get("d").shadowed == () and [s.reason for s in db.get("e").shadowed] == ["no_longer_conforms"]


def test_explain_lists_the_shadowed_entries_that_touch_the_path_and_never_as_winner_or_input(backend: Storage) -> None:
    db = service(backend)
    db.activate_schema(2, operator="alice", acknowledge_breaking=True)
    shadowed = {s.seq: s for s in db.get("d").shadowed}
    nick = db.explain("d", "/nick")
    assert [(s.seq, s.reason) for s in nick.shadowed_by_schema] == [(3, "now_derived")]
    assert nick.winner != shadowed[3].entry_id and shadowed[3].entry_id not in nick.inputs
    assert db.explain("d", "/name").shadowed_by_schema == ()
    age = db.explain("d", "/age")
    assert [(s.seq, s.reason) for s in age.shadowed_by_schema] == [(2, "no_longer_conforms")]
    assert age.winner is None or age.winner != shadowed[2].entry_id


def test_explain_keeps_its_earlier_meaning_of_shadowed(backend: Storage) -> None:
    db = Env(backend, *versions_text(V1, V2)).service(active=1)
    db.append("d", [put("/name", "A")], contributor=ALICE)
    db.append("d", [put("/name", "B")], contributor=ALICE)
    explained = db.explain("d", "/name")
    assert explained.shadowed_by_schema == () and len(explained.shadowed) == 1  # the older write made no difference


def test_an_as_of_read_says_why_too(backend: Storage) -> None:
    from aduku.model import AsOf

    db = service(backend)
    db.activate_schema(2, operator="alice", acknowledge_breaking=True)
    result = db.get("d", as_of=AsOf(seq=3))
    assert [s.seq for s in result.shadowed] == [2, 3]
    assert db.get("d", as_of=AsOf(seq=1)).shadowed == ()
