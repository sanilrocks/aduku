"""Feature 005, user story 3: `as_of` by seq, tx_time and valid_time (scenarios 1 to 4, 6, 8)."""

from datetime import UTC, datetime, timedelta

import pytest

from aduku.errors import ErrorCode, RejectedError
from aduku.model import AsOf
from aduku.schema.loader import load_schema
from aduku.storage.base import Storage
from tests.service_helpers import ALICE, FIELDS, JOB, ROOT, make_service, put, schema_text


def history(backend: Storage):  # noqa: ANN201
    db = make_service(backend)
    live = {}
    plan = [
        ([put("/name", "ann", "backfill"), put("/age", 3, "backfill")], JOB),
        ([put("/name", "bob")], ALICE),
        ([put("/tags", ("x",))], ALICE),
        ([put("/age", 9, "admin")], ROOT),
        ([put("/name", "cy")], ALICE),
        ([put("/tags", ("y",))], ALICE),
    ]
    for entries, who in plan:
        result = db.append("d", entries, contributor=who)
        live[result.entries[-1][1]] = result.view
    return db, live


def test_each_seq_is_the_document_that_was_live_then(backend: Storage) -> None:
    db, live = history(backend)
    for seq, view in live.items():
        past = db.get("d", as_of=AsOf(seq=seq))
        assert (past.document, past.model_errors) == (view.document, view.model_errors)  # type: ignore[union-attr]
        assert past.as_of is not None and (past.as_of.kind, past.as_of.resolved_seq, past.as_of.included) == (
            "seq",
            seq,
            seq,
        )


def test_a_seq_inside_a_request_is_answered_and_says_so(backend: Storage) -> None:
    db, _ = history(backend)
    past = db.get("d", as_of=AsOf(seq=1))
    assert past.document["name"] == "ann" and "age" not in past.document and past.as_of.inside_request is True  # type: ignore[union-attr]


def test_tx_time_resolves_to_the_last_entry_recorded_at_or_before_it(backend: Storage) -> None:
    db, live = history(backend)
    entries = db.list_entries("d")
    moment = entries[2].tx_time + (entries[3].tx_time - entries[2].tx_time) / 2  # between the commits of seq 3 and 4
    past = db.get("d", as_of=AsOf(tx_time=moment))
    reference = db.get("d", as_of=AsOf(seq=3))
    assert past.document == reference.document and past.as_of.resolved_seq == 3 and past.as_of.kind == "tx_time"  # type: ignore[union-attr]


def test_valid_time_leaves_out_what_became_true_later_and_a_normal_read_is_unchanged(backend: Storage) -> None:
    db = make_service(backend)
    march, april = datetime(2024, 3, 1, tzinfo=UTC), datetime(2024, 4, 1, tzinfo=UTC)
    db.append("d", [put("/name", "march", valid_time=march)], contributor=ALICE)  # recorded in "June"
    db.append("d", [put("/name", "april", valid_time=april)], contributor=ALICE)
    db.append("d", [put("/age", 5)], contributor=ALICE)  # no valid_time: always takes part
    normal = db.get("d").document
    mid = db.get("d", as_of=AsOf(valid_time=datetime(2024, 3, 15, tzinfo=UTC)))
    assert mid.document["name"] == "march" and mid.document["age"] == 5
    assert mid.as_of.included == 2 and mid.as_of.kind == "valid_time"  # type: ignore[union-attr]
    early = db.get("d", as_of=AsOf(valid_time=datetime(2024, 1, 1, tzinfo=UTC)))
    assert "name" not in early.document and early.document["age"] == 5
    assert db.get("d").document == normal and normal["name"] == "april"


def test_before_the_first_entry_and_beyond_the_last(backend: Storage) -> None:
    db, live = history(backend)
    entries = db.list_entries("d")
    empty = db.get("d", as_of=AsOf(tx_time=entries[0].tx_time - timedelta(days=1)))
    assert empty.as_of.resolved_seq == 0 and "name" not in empty.document  # type: ignore[union-attr]
    zero = db.get("d", as_of=AsOf(seq=0))
    assert zero.document == empty.document
    beyond = db.get("d", as_of=AsOf(seq=10_000))
    assert beyond.document == db.get("d").document and beyond.as_of.resolved_seq == db.last_seq("d")  # type: ignore[union-attr]


def test_more_than_one_point_or_none_is_refused_as_ambiguous(backend: Storage) -> None:
    db, _ = history(backend)
    for asof in (
        AsOf(),
        AsOf(seq=1, valid_time=datetime(2024, 1, 1, tzinfo=UTC)),
        AsOf(seq=1, tx_time=datetime(2030, 1, 1, tzinfo=UTC)),
    ):
        with pytest.raises(RejectedError) as caught:
            db.get("d", as_of=asof)
        assert caught.value.code is ErrorCode.AS_OF_AMBIGUOUS
        with pytest.raises(RejectedError):
            db.explain("d", "/name", as_of=asof)


def test_explain_as_of_names_the_winner_at_that_point(backend: Storage) -> None:
    db, _ = history(backend)
    entries = db.list_entries("d")
    now = db.explain("d", "/name")
    past = db.explain("d", "/name", as_of=AsOf(seq=2))
    assert past.value == "ann" or past.winner == entries[0].id or past.winner_seq in (1, 2)
    assert past.winner != now.winner and now.value == "cy"
    assert db.explain("d", "/name", as_of=AsOf(seq=db.last_seq("d"))) == now


def test_an_as_of_read_writes_nothing(backend: Storage) -> None:
    db, _ = history(backend)
    with backend.read() as tx:
        before = (tx.get_view("d"), tx.get_layers("d"), tx.get_derived("d"), tx.entries("d"), tx.epoch("d"))
    derivations = db.derivations
    for seq in (1, 3, 5):
        db.get("d", as_of=AsOf(seq=seq))
    db.explain("d", "/age", as_of=AsOf(seq=4))
    with backend.read() as tx:
        assert (tx.get_view("d"), tx.get_layers("d"), tx.get_derived("d"), tx.entries("d"), tx.epoch("d")) == before
    assert db.derivations == derivations


def test_a_read_uses_the_schema_version_the_document_has_now(backend: Storage) -> None:
    db, _ = history(backend)
    v2 = load_schema(
        schema_text(
            version=2, fields=FIELDS.replace('"/age": {type: int, merge: max}', '"/age": {type: int, merge: min}')
        ),
        db.behaviors,
    )
    db.schemas.put(v2)
    db.schemas.activate(2)
    assert (
        db.get("d", as_of=AsOf(seq=4)).document["age"] == 3
    )  # min(3, 9) under the version in force now; max gave 9 then
