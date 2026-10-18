"""US6 and SPEC 12.3: why a path has its value: the winning entry, the rule, what it superseded."""

import pytest

from aduku.errors import ErrorCode, RejectedError
from aduku.ion_codec import FrozenStruct
from aduku.storage.base import Storage
from tests.integration.test_model_errors import SCHEMA as VALIDATED
from tests.service_helpers import ALICE, FIELDS, JOB, delete, make_service, put, schema_text

EXTRA = (
    FIELDS
    + """,
  "/currency": {type: string, merge: lww, default: "USD"},
  "/address": {type: struct, merge: struct},
  "/address/city": {type: string, merge: lww},
  "/note": {type: string, merge: lww}
"""
)


def svc(backend: Storage, schema: str | None = None):  # type: ignore[no-untyped-def]
    return make_service(backend, schema=schema or schema_text(fields=EXTRA))


def ids(service, *seqs: int) -> tuple[str, ...]:  # type: ignore[no-untyped-def]
    by_seq = {e.seq: e.id for e in service.list_entries("d1")}
    return tuple(by_seq[seq] for seq in seqs)


def test_1_the_maximum_rule_and_the_entry_that_supplied_the_value(backend: Storage) -> None:
    service = svc(backend)
    service.append("d1", [put("/age", 10, tier="backfill")], contributor=JOB)
    service.append("d1", [put("/age", 30)], contributor=ALICE)
    service.append("d1", [put("/age", 20)], contributor=ALICE)
    why = service.explain("d1", "/age")
    assert (why.value, why.rule, why.winner_seq) == (30, "max@1", 2)
    assert why.winner == ids(service, 2)[0] and why.inputs == ids(service, 2)
    assert set(why.shadowed) == set(ids(service, 1, 3))


def test_2_an_untouched_defaulted_field_comes_from_the_default(backend: Storage) -> None:
    service = svc(backend)
    service.append("d1", [put("/name", "x")], contributor=ALICE)
    why = service.explain("d1", "/currency")
    assert (why.value, why.winner, why.winner_seq, why.inputs, why.shadowed) == ("USD", "default", None, (), ())
    assert why.rule == "lww@1"


def test_a_higher_tier_beats_a_later_entry_of_a_lower_one(backend: Storage) -> None:
    service = svc(backend)
    service.append("d1", [put("/name", "A", tier="backfill")], contributor=JOB)
    service.append("d1", [put("/name", "B")], contributor=ALICE)
    service.append("d1", [put("/name", "C", tier="backfill")], contributor=JOB)
    why = service.explain("d1", "/name")
    assert (why.value, why.winner_seq, why.rule) == ("B", 2, "lww@1")
    assert set(why.shadowed) == set(ids(service, 1, 3))


def test_every_entry_that_adds_to_a_union_is_an_input_and_none_is_shadowed(backend: Storage) -> None:
    service = svc(backend)
    service.append("d1", [put("/tags", ("a",))], contributor=ALICE)
    service.append("d1", [put("/tags", ("b",))], contributor=ALICE)
    why = service.explain("d1", "/tags")
    assert why.value == ("a", "b") and why.rule == "union@1"
    assert set(why.inputs) == set(ids(service, 1, 2)) and why.shadowed == () and why.winner_seq == 2


def test_an_entry_that_changes_nothing_is_shadowed(backend: Storage) -> None:
    service = svc(backend)
    service.append("d1", [put("/tags", ("a", "b"))], contributor=ALICE)
    service.append("d1", [put("/tags", ("a",))], contributor=ALICE)
    why = service.explain("d1", "/tags")
    assert why.winner_seq == 1 and why.shadowed == ids(service, 2)


def test_a_delete_that_wins_is_the_winner(backend: Storage) -> None:
    service = svc(backend)
    service.append("d1", [put("/note", "x")], contributor=ALICE)
    service.append("d1", [delete("/note")], contributor=ALICE)
    why = service.explain("d1", "/note")
    assert why.value is None and why.winner_seq == 2 and why.shadowed == ids(service, 1)


def test_a_child_written_through_its_parent_struct_is_explained(backend: Storage) -> None:
    service = svc(backend)
    service.append("d1", [put("/address", FrozenStruct({"city": "Paris"}))], contributor=ALICE)
    service.append("d1", [put("/address/city", "Rome")], contributor=ALICE)
    why = service.explain("d1", "/address/city")
    assert (why.value, why.winner_seq, why.rule) == ("Rome", 2, "lww@1") and why.shadowed == ids(service, 1)


def test_a_declared_path_nobody_wrote_has_no_winner(backend: Storage) -> None:
    why = svc(backend).explain("d1", "/note")
    assert (why.value, why.winner, why.winner_seq, why.inputs, why.shadowed) == (None, None, None, (), ())


def test_model_errors_are_limited_to_those_whose_scope_includes_the_path(backend: Storage) -> None:
    service = make_service(backend, schema=VALIDATED)
    service.append("d1", [put("/age", -5, tier="backfill")], contributor=JOB, lenient=True)
    service.append(
        "d1",
        [put("/country", "US", tier="backfill"), put("/spouses", ("A", "B"), tier="backfill")],
        contributor=JOB,
        lenient=True,
    )
    assert [e.rule_id for e in service.explain("d1", "/age").model_errors] == ["range"]
    assert [e.rule_id for e in service.explain("d1", "/spouses").model_errors] == ["max_spouses_by_country"]
    assert service.explain("d1", "/name").model_errors == ()


def test_a_path_the_schema_does_not_declare_is_refused(backend: Storage) -> None:
    for bad in ("/nope", "name", "/a/b/c"):
        with pytest.raises(RejectedError) as caught:
            svc(backend).explain("d1", bad)
        assert caught.value.code is ErrorCode.UNKNOWN_PATH


def test_one_call_explains_each_field_and_writes_nothing(backend: Storage) -> None:
    from tests.integration.test_resilience import Counting

    counting = Counting(backend)
    service = svc(counting)  # type: ignore[arg-type]
    service.append("d1", [put("/name", "x"), put("/age", 3), put("/tags", ("t",))], contributor=ALICE)
    writes = counting.writes
    for path in ("/name", "/age", "/tags", "/currency", "/note"):
        assert service.explain("d1", path).path == path
    assert counting.writes == writes and len(service.list_entries("d1")) == 3


def test_when_no_single_entry_is_essential_the_winner_still_supplied_the_value(backend: Storage) -> None:
    service = svc(backend)
    for age in (5, 3, 5):
        service.append("d1", [put("/age", age)], contributor=ALICE)
    why = service.explain("d1", "/age")
    assert why.value == 5 and why.winner_seq == 1 and why.inputs == ids(service, 1)
    assert set(why.shadowed) == set(ids(service, 2, 3))
