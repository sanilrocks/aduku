"""V0 admission (SPEC 10.1): schema errors are listed together and never bypassed by the flag."""

from aduku.behaviors.registry import BehaviorRegistry
from aduku.engine.admission import check_request as _check_request
from aduku.errors import ErrorCode
from aduku.model import EntryInput, Identity
from aduku.schema.loader import load_schema
from tests.service_helpers import ALICE, JOB, ROOT, delete, put, schema_text

SCHEMA = load_schema(schema_text())


def check_request(schema, entries, who, lenient):  # type: ignore[no-untyped-def]
    return _check_request(schema, entries, who, lenient, BehaviorRegistry.with_builtins())


def codes(reasons: list) -> list[ErrorCode]:  # type: ignore[type-arg]
    return [reason.code for reason in reasons]


def test_valid_request_has_no_reasons() -> None:
    assert check_request(SCHEMA, [put(), put("/age", 30)], ALICE, False) == []


def test_unknown_and_malformed_paths() -> None:
    reasons = check_request(SCHEMA, [put("/nope"), put("name"), put("/a/b/c")], ALICE, False)
    assert codes(reasons) == [ErrorCode.UNKNOWN_PATH] * 3
    assert [r.entry_index for r in reasons] == [0, 1, 2]


def test_type_mismatch() -> None:
    reasons = check_request(
        SCHEMA, [put("/age", "x"), put("/name", 5), put("/tags", ("a", 1)), put("/name", b"raw")], ALICE, False
    )
    assert codes(reasons) == [ErrorCode.TYPE_MISMATCH] * 4


def test_messages_never_contain_the_attempted_value() -> None:
    reasons = check_request(SCHEMA, [put("/age", "SECRETVALUE")], ALICE, False)
    assert "SECRETVALUE" not in reasons[0].message


def test_delete_needs_no_value_type_check() -> None:
    assert check_request(SCHEMA, [delete("/age")], ALICE, False) == []


def test_operations() -> None:
    assert codes(check_request(SCHEMA, [delete("/pin")], ALICE, False)) == [ErrorCode.OP_NOT_ALLOWED]
    assert codes(
        check_request(SCHEMA, [EntryInput(tier="user", path="/name", op="frob", value="x")], ALICE, False)
    ) == [  # type: ignore[arg-type]
        ErrorCode.OP_NOT_ALLOWED
    ]
    assert check_request(SCHEMA, [put("/pin", "1234")], ALICE, False) == []


def test_tiers() -> None:
    assert codes(check_request(SCHEMA, [put(tier="admin")], ALICE, False)) == [
        ErrorCode.TIER_NOT_PERMITTED
    ]  # not Alice's tier
    assert codes(check_request(SCHEMA, [put(tier="nobody")], ROOT, False)) == [
        ErrorCode.TIER_NOT_PERMITTED
    ]  # not in schema


def test_field_authority() -> None:
    assert codes(check_request(SCHEMA, [put("/plan", "pro")], ALICE, False)) == [ErrorCode.NOT_AUTHORIZED]
    assert check_request(SCHEMA, [put("/plan", "pro", tier="admin")], ROOT, False) == []
    by_role = Identity("svc", ("user",), roles=("admin",))
    assert check_request(SCHEMA, [put("/plan", "pro")], by_role, False) == []


def test_derived_fields_are_not_writable_even_with_the_flag() -> None:
    for path in ("/displayName", "/displayName/x"):
        for flag in (False, True):
            reasons = check_request(SCHEMA, [put(path, "HACKED", tier="backfill")], JOB, flag)
            assert codes(reasons) == [ErrorCode.FIELD_NOT_WRITABLE], (path, flag)


def test_every_reason_of_every_entry_is_listed_together() -> None:
    entries = [put("/nope"), put("/name"), put("/age", "x"), put("/plan", "p"), put("/displayName", "X")]
    reasons = check_request(SCHEMA, entries, ALICE, False)
    assert [(r.entry_index, r.code) for r in reasons] == [
        (0, ErrorCode.UNKNOWN_PATH),
        (2, ErrorCode.TYPE_MISMATCH),
        (3, ErrorCode.NOT_AUTHORIZED),
        (4, ErrorCode.FIELD_NOT_WRITABLE),
    ]


def test_one_entry_can_have_several_reasons() -> None:
    reasons = check_request(SCHEMA, [put("/age", "x", tier="admin")], ALICE, False)
    assert {r.code for r in reasons} == {ErrorCode.TIER_NOT_PERMITTED, ErrorCode.TYPE_MISMATCH}


def test_flag_permission() -> None:
    assert codes(check_request(SCHEMA, [put()], ALICE, True)) == [
        ErrorCode.LENIENT_NOT_PERMITTED
    ]  # valid data, still refused
    assert check_request(SCHEMA, [put(tier="backfill")], JOB, True) == []
    mixed = Identity("m", ("backfill", "user"))
    assert codes(check_request(SCHEMA, [put(tier="backfill"), put("/age", 1, tier="user")], mixed, True)) == [
        ErrorCode.LENIENT_NOT_PERMITTED
    ]
    listed = load_schema(schema_text(lenient="lenient: {allowed: [backfill], contributors: [alice]},"))
    assert check_request(listed, [put()], ALICE, True) == []


def test_repeated_idempotency_key_in_one_request() -> None:
    reasons = check_request(SCHEMA, [put(idem_key="k"), put("/age", 1, idem_key="k")], ALICE, False)
    assert codes(reasons) == [ErrorCode.DUPLICATE_IDEM_KEY]
    assert reasons[0].entry_index == 1


def test_schema_errors_only_at_this_stage() -> None:
    # A request with a schema error and a would-be rule violation lists only the schema error (scenario 6);
    # rule checks run only when this stage is clean, so check_request knows nothing about rules.
    assert codes(check_request(SCHEMA, [put("/nope"), put("/age", -5)], ALICE, False)) == [ErrorCode.UNKNOWN_PATH]


def test_extension_operations_are_not_admitted_even_if_a_schema_object_lists_them() -> None:
    """The loader refuses such a schema; this is the second line of defence for hand-built schemas."""
    import dataclasses

    base = load_schema(schema_text(fields='"/n": {type: int, merge: lww}'))
    spec = dataclasses.replace(base.fields[0], ops=("set", "delete", "increment", "append", "reset"))
    schema = dataclasses.replace(base, fields=(spec,))
    for op in ("increment", "append", "reset"):
        entry = EntryInput(tier="user", path="/n", op=op, value=5)  # type: ignore[arg-type]
        assert codes(check_request(schema, [entry], ALICE, False)) == [ErrorCode.OP_NOT_ALLOWED], op
    assert check_request(schema, [put("/n", 5)], ALICE, False) == []
    assert check_request(schema, [delete("/n")], ALICE, False) == []


def test_ops_that_exclude_delete_still_apply() -> None:
    schema = load_schema(schema_text(fields='"/n": {type: int, merge: lww, ops: [set]}'))
    assert codes(check_request(schema, [delete("/n")], ALICE, False)) == [ErrorCode.OP_NOT_ALLOWED]
