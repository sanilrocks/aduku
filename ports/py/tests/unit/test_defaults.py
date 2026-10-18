"""US4 and SPEC 8: defaults are the lowest layer; contributions replace them; deletes follow `on_delete`."""

from aduku.behaviors.registry import BehaviorRegistry
from aduku.engine.derive import derive
from aduku.ion_codec import FrozenStruct, to_canonical
from aduku.schema.loader import load_schema
from tests.helpers import make_entry
from tests.service_helpers import schema_text

FIELDS = """
  "/currency": {type: string, merge: lww, default: "USD", on_delete: absent},
  "/plan": {type: string, merge: lww, default: "free", on_delete: default},
  "/age": {type: int, merge: max, default: 5},
  "/flag": {type: bool, merge: lww, default: true, on_delete: default},
  "/address": {type: struct, merge: struct},
  "/address/country": {type: string, merge: lww, default: "US"},
  "/address/city": {type: string, merge: lww},
  "/limits": {type: struct, merge: struct, default: {max: 10, min: 1}},
  "/limits/max": {type: int, merge: lww},
  "/limits/min": {type: int, merge: lww}
"""
BEHAVIORS = BehaviorRegistry.with_builtins()
SCHEMA = load_schema(schema_text(fields=FIELDS))
RESTORING = load_schema(
    schema_text(
        fields="""
  "/address": {type: struct, merge: struct},
  "/address/country": {type: string, merge: lww, default: "US", on_delete: default},
  "/address/city": {type: string, merge: lww},
  "/limits": {type: struct, merge: struct, default: {max: 10, min: 1}, on_delete: default},
  "/limits/max": {type: int, merge: lww},
  "/limits/min": {type: int, merge: lww}
"""
    )
)


def e(seq, tier, path, value=None, op="set"):  # type: ignore[no-untyped-def]
    return make_entry(seq=seq, request_id=f"r{seq}", tier=tier, path=path, op=op, value=value)


def doc(*entries, schema=SCHEMA):  # type: ignore[no-untyped-def]
    return derive(schema, BEHAVIORS, list(entries)).document


def test_an_empty_ledger_gives_only_the_defaults() -> None:
    """Scenario 1, and `Given a plan field with default free and no contributions`."""
    assert to_canonical(doc()) == (
        '{address:{country:"US"},age:5,currency:"USD",flag:true,limits:{max:10,min:1},plan:"free"}'
    )


def test_a_contribution_replaces_the_default_under_the_fields_rule() -> None:
    """Scenario 2: lww simply replaces; max keeps the larger of the default and the contribution (SPEC 6.2)."""
    assert doc(e(1, "user", "/currency", "EUR"))["currency"] == "EUR"
    assert doc(e(1, "user", "/age", 3))["age"] == 5
    assert doc(e(1, "user", "/age", 9))["age"] == 9
    assert doc(e(1, "backfill", "/plan", "pro"), e(2, "user", "/plan", "team"))["plan"] == "team"


def test_deleting_a_field_whose_on_delete_is_absent_removes_it() -> None:
    """Scenario 3: the default does not come back."""
    merged = doc(e(1, "admin", "/currency", op="delete"))
    assert "currency" not in merged and merged["plan"] == "free"
    assert "currency" not in doc(e(1, "user", "/currency", "EUR"), e(2, "admin", "/currency", op="delete"))


def test_deleting_a_field_whose_on_delete_is_default_shows_the_default() -> None:
    """Scenario 4."""
    assert doc(e(1, "admin", "/plan", op="delete"))["plan"] == "free"
    assert doc(e(1, "user", "/plan", "pro"), e(2, "admin", "/plan", op="delete"))["plan"] == "free"  # not "pro"
    assert doc(e(1, "admin", "/flag", op="delete"))["flag"] is True


def test_a_write_after_a_delete_recreates_the_field() -> None:
    assert doc(e(1, "admin", "/plan", op="delete"), e(2, "admin", "/plan", "pro"))["plan"] == "pro"
    assert doc(e(1, "admin", "/currency", op="delete"), e(2, "admin", "/currency", "JPY"))["currency"] == "JPY"


def test_a_lower_tier_cannot_revive_a_deleted_field_or_its_default() -> None:
    merged = doc(e(1, "admin", "/currency", op="delete"), e(2, "backfill", "/currency", "GBP"))
    assert "currency" not in merged


def test_struct_defaults_apply_per_child() -> None:
    """SPEC 8.4: `/address/country` is filled in although nobody wrote `/address`."""
    merged = doc(e(1, "user", "/address/city", "Paris"))
    assert merged["address"] == FrozenStruct({"country": "US", "city": "Paris"})
    assert doc(e(1, "user", "/address/country", "FR"))["address"]["country"] == "FR"


def test_a_struct_declaring_its_own_default_spreads_it_over_the_children() -> None:
    assert doc()["limits"] == FrozenStruct({"max": 10, "min": 1})
    assert doc(e(1, "user", "/limits/max", 3))["limits"] == FrozenStruct({"max": 3, "min": 1})
    assert doc(e(1, "user", "/limits", FrozenStruct({"max": 7})))["limits"] == FrozenStruct({"max": 7, "min": 1})


def test_deleting_a_struct_follows_each_childs_own_setting() -> None:
    """FR-016: per child. Here nothing falls back, so the whole subtree is gone."""
    merged = doc(e(1, "user", "/address/city", "Paris"), e(2, "admin", "/address", op="delete"))
    assert "address" not in merged
    assert "limits" not in doc(e(1, "admin", "/limits", op="delete"))


def test_deleting_a_struct_restores_the_children_that_fall_back_to_their_default() -> None:
    merged = doc(e(1, "user", "/address/city", "Paris"), e(2, "admin", "/address", op="delete"), schema=RESTORING)
    assert merged["address"] == FrozenStruct({"country": "US"})  # city had no default, country falls back
    restored = doc(e(1, "user", "/limits/max", 99), e(2, "admin", "/limits", op="delete"), schema=RESTORING)
    assert restored["limits"] == FrozenStruct({"max": 10, "min": 1})


def test_writes_after_deleting_a_struct_recreate_only_what_is_written() -> None:
    merged = doc(
        e(1, "user", "/address/city", "Paris"),
        e(2, "admin", "/address", op="delete"),
        e(3, "admin", "/address/city", "Rome"),
        schema=RESTORING,
    )
    assert merged["address"] == FrozenStruct({"country": "US", "city": "Rome"})


def test_a_child_that_says_absent_is_not_restored_by_its_parents_fallback() -> None:
    """SPEC 8.5: deleting a struct falls back child by child, each child by its own on_delete."""
    schema = load_schema(
        schema_text(
            fields="""
  "/address": {type: struct, merge: struct, default: {city: "X", zip: "1"}, on_delete: default},
  "/address/city": {type: string, merge: lww, on_delete: absent},
  "/address/zip": {type: string, merge: lww}
"""
        )
    )
    merged = doc(e(1, "admin", "/address", op="delete"), schema=schema)
    assert merged["address"] == FrozenStruct({"zip": "1"})  # city said absent; zip did not say anything, so it follows
    assert doc(schema=schema)["address"] == FrozenStruct({"city": "X", "zip": "1"})  # without a delete, both defaults
