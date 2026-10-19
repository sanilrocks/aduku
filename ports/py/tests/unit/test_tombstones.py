"""US5 and SPEC 6.7: a delete beats lower tiers and earlier writes; a later write at its tier or above re-creates."""

from aduku.behaviors.registry import BehaviorRegistry
from aduku.engine.derive import derive
from aduku.ion_codec import FrozenStruct, to_canonical
from aduku.schema.loader import load_schema
from aduku.storage.base import Storage
from tests.helpers import make_entry
from tests.service_helpers import ALICE, FIELDS, JOB, ROOT, delete, make_service, put, schema_text

PHONE_FIELDS = (
    FIELDS
    + """,
  "/phone": {type: string, merge: lww},
  "/address": {type: struct, merge: struct},
  "/address/city": {type: string, merge: lww},
  "/address/zip": {type: string, merge: lww},
  "/address/geo": {type: struct, merge: struct},
  "/address/geo/lat": {type: int, merge: lww},
  "/address/geo/lng": {type: int, merge: lww}
"""
)
SCHEMA = load_schema(schema_text(fields=PHONE_FIELDS))
BEHAVIORS = BehaviorRegistry.with_builtins()


def e(seq, tier, path, value=None, op="set"):  # type: ignore[no-untyped-def]
    return make_entry(seq=seq, request_id=f"r{seq}", tier=tier, path=path, op=op, value=value)


def doc(*entries):  # type: ignore[no-untyped-def]
    return derive(SCHEMA, BEHAVIORS, list(entries)).document


# -- the four scenarios -----------------------------------------------------------------------------------------------


def test_a_delete_from_a_higher_tier_removes_the_field() -> None:
    """Scenario 1."""
    merged = doc(e(1, "backfill", "/phone", "555-0100"), e(2, "admin", "/phone", op="delete"))
    assert "phone" not in merged


def test_a_lower_tier_write_cannot_revive_it() -> None:
    """Scenario 2: the write is later in time but lower in tier."""
    merged = doc(
        e(1, "backfill", "/phone", "555-0100"),
        e(2, "admin", "/phone", op="delete"),
        e(3, "backfill", "/phone", "555-0199"),
    )
    assert "phone" not in merged


def test_a_later_write_from_the_deleting_tier_recreates_it() -> None:
    """Scenario 3."""
    merged = doc(
        e(1, "backfill", "/phone", "555-0100"),
        e(2, "admin", "/phone", op="delete"),
        e(3, "admin", "/phone", "555-0300"),
    )
    assert merged["phone"] == "555-0300"


def test_a_write_from_a_higher_tier_than_the_delete_recreates_it() -> None:
    merged = doc(e(1, "backfill", "/phone", "old"), e(2, "user", "/phone", op="delete"), e(3, "admin", "/phone", "new"))
    assert merged["phone"] == "new"


def test_deleting_a_struct_removes_the_whole_subtree() -> None:
    """Scenario 4."""
    merged = doc(
        e(1, "backfill", "/address/city", "Paris"),
        e(2, "backfill", "/address/geo/lat", 48),
        e(3, "user", "/address", FrozenStruct({"zip": "1", "geo": FrozenStruct({"lng": 2})})),
        e(4, "admin", "/address", op="delete"),
        e(5, "backfill", "/name", "kept"),
    )
    assert "address" not in merged and merged["name"] == "kept"


def test_writes_beneath_a_deleted_struct_recreate_only_what_they_write() -> None:
    merged = doc(
        e(1, "backfill", "/address/city", "Paris"),
        e(2, "backfill", "/address/zip", "78701"),
        e(3, "admin", "/address", op="delete"),
        e(4, "admin", "/address/geo/lat", 48),
        e(5, "backfill", "/address/zip", "99999"),  # lower tier: cannot bring the subtree back
    )
    assert merged["address"] == FrozenStruct({"geo": FrozenStruct({"lat": 48})})


# -- edge cases -------------------------------------------------------------------------------------------------------


def test_a_delete_that_arrives_before_any_value_still_suppresses_lower_tiers() -> None:
    merged = doc(
        e(1, "admin", "/phone", op="delete"), e(2, "backfill", "/phone", "late"), e(3, "user", "/phone", "later")
    )
    assert "phone" not in merged
    assert doc(e(1, "admin", "/phone", op="delete"), e(2, "admin", "/phone", "mine"))["phone"] == "mine"


def test_within_one_tier_the_later_entry_wins() -> None:
    assert "phone" not in doc(e(1, "user", "/phone", "a"), e(2, "user", "/phone", op="delete"))
    assert (
        doc(e(1, "user", "/phone", "a"), e(2, "user", "/phone", op="delete"), e(3, "user", "/phone", "b"))["phone"]
        == "b"
    )
    assert doc(e(1, "user", "/phone", op="delete"), e(2, "user", "/phone", "b"))["phone"] == "b"


def test_a_delete_of_something_never_written_is_harmless() -> None:
    merged = doc(e(1, "admin", "/phone", op="delete"), e(2, "user", "/name", "x"))
    assert merged == FrozenStruct({"name": "x"})
    assert doc(e(1, "admin", "/phone", op="delete"), e(2, "admin", "/phone", op="delete")) == FrozenStruct()


def test_a_lower_tier_delete_does_not_remove_a_higher_tier_value() -> None:
    merged = doc(e(1, "admin", "/phone", "boss"), e(2, "backfill", "/phone", op="delete"))
    assert merged["phone"] == "boss"


def test_deleting_one_child_leaves_its_siblings() -> None:
    merged = doc(
        e(1, "user", "/address", FrozenStruct({"city": "Paris", "zip": "1"})),
        e(2, "admin", "/address/city", op="delete"),
    )
    assert merged["address"] == FrozenStruct({"zip": "1"})


def test_a_delete_resets_a_rule_that_would_otherwise_remember_the_past() -> None:
    """With `max`, a value written after a delete is the new value, not the larger of it and what was deleted."""
    assert doc(e(1, "user", "/age", 30), e(2, "user", "/age", op="delete"), e(3, "user", "/age", 5))["age"] == 5
    assert doc(e(1, "backfill", "/age", 30), e(2, "admin", "/age", op="delete"), e(3, "admin", "/age", 5))["age"] == 5
    assert doc(e(1, "backfill", "/age", 30), e(2, "user", "/age", 7))["age"] == 30  # no delete: max as usual


def test_a_union_forgets_what_a_delete_removed() -> None:
    merged = doc(
        e(1, "backfill", "/tags", ("x", "y")), e(2, "admin", "/tags", op="delete"), e(3, "admin", "/tags", ("z",))
    )
    assert merged["tags"] == ("z",)


# -- through the service, on both backends ------------------------------------------------------------------------------


def test_the_story_end_to_end_and_the_same_document_when_derived_from_scratch(backend: Storage) -> None:
    service = make_service(backend, schema=schema_text(fields=PHONE_FIELDS))
    service.append(
        "d1", [put("/phone", "555", tier="backfill"), put("/address/zip", "1", tier="backfill")], contributor=JOB
    )
    service.append("d1", [delete("/phone", tier="admin")], contributor=ROOT)
    service.append("d1", [put("/phone", "999", tier="backfill")], contributor=JOB)  # lower tier, later: no effect
    assert "phone" not in service.get("d1").document
    service.append("d1", [put("/phone", "777", tier="admin")], contributor=ROOT)
    service.append(
        "d1", [delete("/address", tier="admin"), put("/address/city", "Paris", tier="admin")], contributor=ROOT
    )
    service.append("d1", [put("/address/zip", "2", tier="backfill")], contributor=JOB)
    warm = service.get("d1")
    assert to_canonical(warm.document) == '{address:{city:"Paris"},displayName:computed::null,phone:"777"}'
    with backend.transaction() as tx:
        tx.drop_view("d1")
    fresh = make_service(backend, schema=schema_text(fields=PHONE_FIELDS)).get("d1")  # derived from the ledger alone
    assert fresh.document == warm.document


def test_the_ledger_keeps_every_delete(backend: Storage) -> None:
    """Deletion is recordable and auditable: it is an entry, not an erasure of history."""
    service = make_service(backend, schema=schema_text(fields=PHONE_FIELDS))
    service.append("d1", [put("/phone", "555")], contributor=ALICE)
    service.append("d1", [delete("/phone", tier="user")], contributor=ALICE)
    ops = [(entry.seq, entry.op) for entry in service.list_entries("d1")]
    assert ops == [(1, "set"), (2, "delete")]
