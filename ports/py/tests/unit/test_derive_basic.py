"""US2 scenarios 2 and 3 and FR-019: tier then seq decide; the same ledger always gives the same bytes."""

from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

from aduku.behaviors.registry import BehaviorRegistry
from aduku.engine.derive import derive
from aduku.ion_codec import FrozenStruct, to_canonical
from aduku.schema.loader import load_schema
from tests.engine_helpers import FIELDS
from tests.helpers import T0, make_entry
from tests.service_helpers import schema_text

BEHAVIORS = BehaviorRegistry.with_builtins()
SCHEMA = load_schema(schema_text(fields=FIELDS))


def doc(*entries):  # type: ignore[no-untyped-def]
    return derive(SCHEMA, BEHAVIORS, list(entries)).document


def e(seq, tier, path, value=None, op="set"):  # type: ignore[no-untyped-def]
    return make_entry(seq=seq, request_id=f"r{seq}", tier=tier, path=path, op=op, value=value)


def test_an_empty_ledger_gives_an_empty_document() -> None:
    assert doc() == FrozenStruct()
    assert to_canonical(doc()) == "{}"


def test_higher_tiers_win_whatever_arrived_first() -> None:
    """Scenario 3: a late write from a lower tier cannot beat an earlier higher-tier value."""
    admin_first = doc(e(1, "admin", "/name", "Admin"), e(2, "backfill", "/name", "Late backfill"))
    assert admin_first["name"] == "Admin"
    backfill_first = doc(e(1, "backfill", "/name", "Early"), e(2, "admin", "/name", "Admin"))
    assert backfill_first["name"] == "Admin"
    same_tier = doc(e(1, "user", "/name", "first"), e(2, "user", "/name", "second"))
    assert same_tier["name"] == "second"  # within a tier the later seq wins


def test_the_rules_decide_conflicts_between_tiers() -> None:
    merged = doc(
        e(1, "backfill", "/age", 30),
        e(2, "user", "/age", 28),
        e(3, "backfill", "/low", 5),
        e(4, "admin", "/low", 9),
        e(5, "backfill", "/tags", ("x",)),
        e(6, "user", "/tags", ("y", "x")),
        e(7, "admin", "/tags", ("z",)),
    )
    assert merged["age"] == 30 and merged["low"] == 5 and merged["tags"] == ("x", "y", "z")


def test_nested_structs_merge_field_by_field_across_tiers_and_paths() -> None:
    merged = doc(
        e(1, "backfill", "/address/zip", "78701"),
        e(2, "admin", "/address/city", "Paris"),
        e(3, "user", "/address", FrozenStruct({"city": "Rome", "n": 4})),  # a whole struct, expanded child by child
        e(4, "backfill", "/address/n", 9),
    )
    assert merged["address"] == FrozenStruct({"zip": "78701", "city": "Paris", "n": 9})  # city: admin beat user; n: max


def test_a_struct_written_at_a_container_path_interleaves_with_leaf_writes_by_seq() -> None:
    later_struct = doc(e(1, "user", "/address/city", "Paris"), e(2, "user", "/address", FrozenStruct({"city": "Rome"})))
    assert later_struct["address"]["city"] == "Rome"
    later_leaf = doc(e(1, "user", "/address", FrozenStruct({"city": "Rome"})), e(2, "user", "/address/city", "Paris"))
    assert later_leaf["address"]["city"] == "Paris"


def test_deletes_remove_fields_and_a_later_write_recreates_them() -> None:
    assert "name" not in doc(e(1, "backfill", "/name", "A"), e(2, "admin", "/name", op="delete"))
    assert "name" not in doc(
        e(1, "admin", "/name", op="delete"), e(2, "backfill", "/name", "late")
    )  # lower tier cannot revive
    assert doc(e(1, "admin", "/name", op="delete"), e(2, "admin", "/name", "again"))["name"] == "again"


def test_deleting_a_struct_removes_its_whole_subtree() -> None:
    merged = doc(
        e(1, "backfill", "/address/city", "Paris"),
        e(2, "backfill", "/address/zip", "1"),
        e(3, "admin", "/address", op="delete"),
        e(4, "backfill", "/name", "kept"),
    )
    assert "address" not in merged and merged["name"] == "kept"
    rebuilt = doc(
        e(1, "backfill", "/address/city", "Paris"),
        e(2, "admin", "/address", op="delete"),
        e(3, "admin", "/address/zip", "9"),
    )
    assert rebuilt["address"] == FrozenStruct({"zip": "9"})  # only the written leaf is re-created


def test_the_same_ledger_always_gives_the_same_bytes() -> None:
    entries = [e(1, "backfill", "/name", "A"), e(2, "user", "/address/city", "Paris"), e(3, "admin", "/age", 3)]
    first = to_canonical(doc(*entries))
    assert first == to_canonical(doc(*reversed(entries))) == to_canonical(doc(*entries))
    assert first.encode("utf-8") == to_canonical(doc(*entries)).encode("utf-8")
    assert first == '{address:{city:"Paris"},age:3,name:"A"}'  # fields in a stable order


def test_recorded_times_do_not_matter() -> None:
    entries = [e(1, "user", "/name", "A"), e(2, "admin", "/name", "B"), e(3, "user", "/age", 1)]
    skewed = [replace(x, tx_time=T0 - timedelta(days=x.seq * 40)) for x in entries]
    assert doc(*skewed) == doc(*entries)


def test_decimals_and_lists_keep_their_exact_form() -> None:
    schema = load_schema(schema_text(fields='"/price": {type: decimal, merge: max}'))
    merged = derive(schema, BEHAVIORS, [e(1, "user", "/price", Decimal("12.50"))]).document
    assert to_canonical(merged) == "{price:12.50}"
