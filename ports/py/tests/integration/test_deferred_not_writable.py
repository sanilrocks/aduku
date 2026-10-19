"""US7: no contribution can write a deferred field, whatever its refresh policy, and nothing in the core can read one
(FR-021, FR-022, SC-009)."""

import pytest

from aduku.behaviors.registry import BehaviorRegistry
from aduku.errors import ErrorCode, RejectedError, SchemaError
from aduku.schema.loader import load_schema
from aduku.storage.base import Storage
from tests.deferred_helpers import build, deferred
from tests.service_helpers import ALICE, ROOT, delete, put

POLICIES = {
    "once": "once",
    "per_fetch": "per_fetch",
    "scheduled": "scheduled::{every: " + chr(34) + "1h" + chr(34) + "}",
    "notification": "notification::{topic: " + chr(34) + "t" + chr(34) + "}",
}


@pytest.mark.parametrize("name", POLICIES)
def test_an_entry_on_a_deferred_path_is_rejected_whatever_the_policy(name: str, backend: Storage) -> None:
    extra = {"ttl": chr(34) + "5m" + chr(34)} if name == "per_fetch" else {}
    built = build(backend, f'"/score": {deferred(refresh=POLICIES[name], **extra)}')
    built.service.append("d1", [put("/name", "Alice")], contributor=ALICE)
    for entry in (put("/score", 1, tier="admin"), delete("/score", tier="admin"), put("/score/part", 1, tier="admin")):
        with pytest.raises(RejectedError) as caught:
            built.service.append("d1", [entry], contributor=ROOT)
        assert caught.value.code in (ErrorCode.FIELD_NOT_WRITABLE, ErrorCode.UNKNOWN_PATH)
    assert len(built.service.list_entries("d1")) == 1 and built.calls("fx.score@1") == 0


def test_the_lenient_flag_does_not_open_a_deferred_path(backend: Storage) -> None:
    built = build(backend, f'"/score": {deferred()}')
    from tests.service_helpers import JOB

    with pytest.raises(RejectedError) as caught:
        built.service.append("d1", [put("/score", 1, tier="backfill")], contributor=JOB, lenient=True)
    assert caught.value.code is ErrorCode.FIELD_NOT_WRITABLE


def test_a_mixed_request_with_one_deferred_entry_records_nothing(backend: Storage) -> None:
    built = build(backend, f'"/score": {deferred()}')
    with pytest.raises(RejectedError):
        built.service.append("d1", [put("/name", "Alice"), put("/score", 1, tier="admin")], contributor=ROOT)
    assert built.service.list_entries("d1") == []


def behaviors() -> BehaviorRegistry:
    registry = BehaviorRegistry.with_builtins()
    registry.register_hook("fx.score", 1, lambda inputs, timeout: 1)
    registry.register_validate("acme.rule", 1, lambda subject: [])
    return registry


WRAP = (
    'schema::{id: "t", version: 1, tiers: [user, admin], %s fields: {"/name": {type: string, merge: lww}, '
    '"/score": %s, %s}}'
)
SCORE = '{type: int, deferred: {hook: "fx.score@1", depends: ["/name"], refresh: once, invalidate_by: [admin]}}'


@pytest.mark.parametrize(
    "top, other",
    [
        ("", '"/label": {type: string, computed: {macro: concat, args: ["/score"]}}'),
        ("", '"/age": {type: int, merge: max, validate: [range::{min: 0, depends: ["/score"]}]}'),
        (
            'document_validate: [rule::{depends: ["/score"]}],',
            '"/x": {type: int, merge: lww}',
        ),
        ("", '"/d": {type: int, merge: lww, default: {macro: copy, args: ["/score"]}}'),
    ],
)
def test_a_computed_field_validator_document_rule_or_default_reading_a_deferred_field_is_refused(
    top: str, other: str
) -> None:
    with pytest.raises(SchemaError) as caught:
        load_schema(WRAP % (top, SCORE, other), behaviors())
    assert "DEPENDS_ON_DEFERRED" in caught.value.codes
