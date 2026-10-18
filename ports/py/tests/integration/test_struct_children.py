"""A struct written to a field-wise parent is checked child by child: authority, derived fields, key names."""

import pytest

from aduku.behaviors.registry import BehaviorRegistry
from aduku.engine.admission import check_request
from aduku.engine.derive import derive
from aduku.errors import ErrorCode
from aduku.ion_codec import FrozenStruct
from aduku.schema.loader import load_schema
from tests.helpers import make_entry
from tests.service_helpers import ALICE, FIELDS, ROOT, delete, put, schema_text

PROFILE = (
    FIELDS
    + """,
  "/profile": {type: struct, merge: struct},
  "/profile/ok": {type: int, merge: lww},
  "/profile/score": {type: int, computed: {macro: sum, args: ["/age"]}},
  "/profile/secret": {type: string, merge: lww, authority: [admin]},
  "/profile/free": {type: string, merge: lww},
  "/profile/frozen": {type: string, merge: lww, ops: [delete]}
"""
)
SCHEMA = load_schema(schema_text(fields=PROFILE))
BEHAVIORS = BehaviorRegistry.with_builtins()


def check(entry, who=ALICE):  # type: ignore[no-untyped-def]
    return check_request(SCHEMA, [entry], who, False, BEHAVIORS)


def test_a_derived_child_cannot_be_written_through_its_parent() -> None:
    reasons = check(put("/profile", FrozenStruct({"score": 99})))
    assert [r.code for r in reasons] == [ErrorCode.FIELD_NOT_WRITABLE]
    assert reasons[0].path == "/profile" and "/profile/score" in reasons[0].message


def test_a_childs_authority_applies_to_a_write_through_its_parent() -> None:
    reasons = check(put("/profile", FrozenStruct({"secret": "x"})))
    assert [r.code for r in reasons] == [ErrorCode.NOT_AUTHORIZED] and "/profile/secret" in reasons[0].message
    assert (
        check(
            put(
                "/profile",
                FrozenStruct({"secret": "x"}),
            ),
            ROOT,
        )
        != []
    )  # ROOT writes at tier user: still not enough
    assert (
        check(
            put(
                "/profile",
                FrozenStruct(
                    {"secret": "x"},
                ),
                tier="admin",
            ),
            ROOT,
        )
        == []
    )


def test_ordinary_children_are_fine() -> None:
    assert check(put("/profile", FrozenStruct({"ok": 1, "free": "form"}))) == []


def test_derivation_drops_a_child_that_this_schema_version_derives() -> None:
    """An older entry may have written a field that is derived now; the other children of the entry still count."""
    entries = [make_entry(seq=1, request_id="r1", path="/profile", value=FrozenStruct({"score": 9, "ok": 1}))]
    derived = derive(SCHEMA, BEHAVIORS, entries)
    assert derived.document["profile"] == FrozenStruct({"ok": 1}) and derived.state.skipped == (1,)


@pytest.mark.parametrize("key", ["a[1]", "a/b", "has space", "", "a.b"])
def test_struct_keys_must_be_valid_field_names(key: str) -> None:
    reasons = check(put("/profile", FrozenStruct({key: 1})))
    assert [r.code for r in reasons] == [ErrorCode.TYPE_MISMATCH] and "field name" in reasons[0].message


def test_keys_of_a_struct_held_as_a_whole_value_are_not_restricted() -> None:
    schema = load_schema(schema_text(fields='"/blob": {type: struct, merge: lww}'))
    assert check_request(schema, [put("/blob", FrozenStruct({"a[1]": 1, "a/b": 2}))], ALICE, False, BEHAVIORS) == []


def test_an_empty_request_has_no_authority_to_use_the_flag() -> None:
    reasons = check_request(SCHEMA, [], ALICE, True, BEHAVIORS)
    assert [r.code for r in reasons] == [ErrorCode.LENIENT_NOT_PERMITTED]


def test_a_childs_ops_apply_to_a_write_through_its_parent() -> None:
    reasons = check(put("/profile", FrozenStruct({"frozen": "x"})))
    assert [r.code for r in reasons] == [ErrorCode.OP_NOT_ALLOWED] and "/profile/frozen" in reasons[0].message


def test_an_undeclared_child_cannot_be_written_through_its_parent() -> None:
    reasons = check(put("/profile", FrozenStruct({"bogus": 1})))
    assert [r.code for r in reasons] == [ErrorCode.UNKNOWN_PATH] and "/profile/bogus" in reasons[0].message


def test_deleting_a_parent_needs_the_authority_of_the_children_it_wipes() -> None:
    reasons = check(delete("/profile"))
    assert [r.code for r in reasons] == [ErrorCode.NOT_AUTHORIZED] and "/profile/secret" in reasons[0].message
    assert check(delete("/profile", tier="admin"), ROOT) == []
    assert check(delete("/profile/ok")) == []  # a child with no authority rule is fine
