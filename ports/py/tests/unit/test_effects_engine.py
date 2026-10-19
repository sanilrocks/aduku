"""Feature 002, research R-9: fingerprints, the outcome in force, and which calls a derivation still needs."""

import hashlib
from datetime import UTC, datetime
from decimal import Decimal

from aduku.behaviors.registry import BehaviorRegistry
from aduku.engine.compute import compute_fields
from aduku.engine.effects import EffectView, fingerprint, view_from_records
from aduku.ion_codec import Annotated, FrozenStruct
from aduku.schema.loader import load_schema
from aduku.storage.base import EffectRecord
from tests.fixtures import register_fixtures

BEHAVIORS = BehaviorRegistry.with_builtins()
register_fixtures(BEHAVIORS)
T0 = datetime(2026, 10, 19, tzinfo=UTC)

IMPURE = (
    '{type: string, %s computed: {impure: {hook: "fx.echo@1", depends: ["%s"], reason: "r", timeout: "1s",'
    ' retry: {max: 1, backoff: "10ms"}, deadline: "3s", recompute_by: [a]}}}'
)


def schema_of(*decls: str):  # type: ignore[no-untyped-def]
    base = '"/country": {type: string, merge: lww}'
    fields = ", ".join([base, *decls])
    return load_schema(f'schema::{{id: "t", version: 1, tiers: [a], fields: {{{fields}}}}}', BEHAVIORS)


def rec(field: str, fp: str, seq: int, **kw: object) -> EffectRecord:
    values: dict[str, object] = dict(outcome="value", value='"tax-US"', error=None, attempts=1, recorded_at=T0)
    values.update(kw)
    return EffectRecord("d1", field, fp, "fx.echo@1", effect_seq=seq, **values)  # type: ignore[arg-type]


def test_a_fingerprint_is_the_sha256_of_the_canonical_text_of_the_hook_and_its_inputs() -> None:
    expected = hashlib.sha256(b'["fx.echo@1",[["/country","US"]]]').hexdigest()
    assert fingerprint("fx.echo@1", [("/country", "US")]) == expected
    assert (
        expected == "05d9d4e8a7eec796014b0c27586e922cdab0cf64e2d7ca52b4d91e47cca8fa9b"
    )  # pinned: every port must agree


def test_a_fingerprint_depends_on_everything_the_hook_sees() -> None:
    base = fingerprint("fx.echo@1", [("/country", "US"), ("/name", "Pat")])
    assert base == fingerprint("fx.echo@1", [("/country", "US"), ("/name", "Pat")])
    assert base != fingerprint("fx.echo@2", [("/country", "US"), ("/name", "Pat")])  # another hook version
    assert base != fingerprint("fx.other@1", [("/country", "US"), ("/name", "Pat")])
    assert base != fingerprint("fx.echo@1", [("/country", "CA"), ("/name", "Pat")])  # another value
    assert base != fingerprint("fx.echo@1", [("/name", "Pat"), ("/country", "US")])  # another declared order
    assert base != fingerprint("fx.echo@1", [("/country", "US")])


def test_the_type_of_a_value_is_part_of_the_fingerprint() -> None:
    assert fingerprint("h@1", [("/x", 1)]) != fingerprint("h@1", [("/x", "1")])
    assert fingerprint("h@1", [("/x", Decimal("1.0"))]) != fingerprint("h@1", [("/x", 1)])


def test_struct_inputs_do_not_depend_on_key_order() -> None:
    one = FrozenStruct({"a": 1, "b": 2})
    two = FrozenStruct({"b": 2, "a": 1})
    assert fingerprint("h@1", [("/s", one)]) == fingerprint("h@1", [("/s", two)])


def test_the_latest_record_per_key_is_the_outcome_in_force() -> None:
    view = view_from_records(
        [
            rec("/taxId", "f1", 1, value='"old"'),
            rec("/taxId", "f1", 4, value='"new"'),
            rec("/taxId", "f2", 2, outcome="failure", value=None, error="timeout", attempts=3),
        ]
    )
    won = view.lookup("/taxId", "f1", "fx.echo@1")
    assert won is not None and (won.kind, won.value, won.effect_seq) == ("value", "new", 4)
    failed = view.lookup("/taxId", "f2", "fx.echo@1")
    assert failed is not None and (failed.kind, failed.error, failed.attempts) == ("failure", "timeout", 3)
    assert view.lookup("/taxId", "other", "fx.echo@1") is None and view.lookup("/taxId", "f1", "fx.echo@2") is None


def test_a_recorded_value_is_read_back_as_an_ion_value() -> None:
    view = view_from_records([rec("/x", "f", 1, value="{a:1,b:[2,3]}")])
    got = view.lookup("/x", "f", "fx.echo@1")
    assert got is not None and got.value == FrozenStruct({"a": 1, "b": (2, 3)})


def test_with_outcomes_adds_to_a_view_without_changing_it() -> None:
    view = EffectView()
    more = view_from_records([rec("/x", "f", 1)])
    combined = view.with_outcomes(more)
    assert view.lookup("/x", "f", "fx.echo@1") is None and combined.lookup("/x", "f", "fx.echo@1") is not None


# -- which calls does a derivation still need? ---------------------------------------------------------------------


def test_an_impure_field_with_no_outcome_is_a_pending_call_and_is_left_out() -> None:
    schema = schema_of('"/taxId": ' + IMPURE % ("", "/country"))
    result = compute_fields(schema, BEHAVIORS, FrozenStruct({"country": "US"}), effects=EffectView())
    (call,) = result.pending
    assert (call.field, call.hook, call.inputs) == ("/taxId", "fx.echo@1", (("/country", "US"),))
    assert call.fingerprint == fingerprint("fx.echo@1", [("/country", "US")])
    assert "taxId" not in result.document


def test_a_recorded_value_is_used_and_nothing_is_pending() -> None:
    schema = schema_of('"/taxId": ' + IMPURE % ("", "/country"))
    fp = fingerprint("fx.echo@1", [("/country", "US")])
    view = view_from_records([rec("/taxId", fp, 7)])
    result = compute_fields(schema, BEHAVIORS, FrozenStruct({"country": "US"}), effects=view)
    assert result.pending == () and result.document["taxId"] == Annotated(("computed",), "tax-US")
    assert result.results["/taxId"].effect_seq == 7 and result.results["/taxId"].source == "fx.echo@1"


def test_another_input_value_needs_another_call() -> None:
    schema = schema_of('"/taxId": ' + IMPURE % ("", "/country"))
    view = view_from_records([rec("/taxId", fingerprint("fx.echo@1", [("/country", "US")]), 1)])
    result = compute_fields(schema, BEHAVIORS, FrozenStruct({"country": "CA"}), effects=view)
    assert [c.inputs for c in result.pending] == [(("/country", "CA"),)]


def test_a_missing_input_means_no_call_and_the_field_follows_on_missing() -> None:
    schema = schema_of('"/taxId": ' + IMPURE % ("on_missing: absent,", "/country"))
    result = compute_fields(schema, BEHAVIORS, FrozenStruct(), effects=EffectView())
    assert result.pending == () and "taxId" not in result.document
    default = schema_of('"/taxId": ' + IMPURE % ("", "/country"))
    assert compute_fields(default, BEHAVIORS, FrozenStruct(), effects=EffectView()).document["taxId"] == Annotated(
        ("computed",), None
    )


def test_an_impure_field_may_depend_on_a_pure_computed_field_and_the_reverse() -> None:
    schema = schema_of(
        '"/key": {type: string, computed: {macro: upper, args: ["/country"]}}',
        '"/taxId": ' + IMPURE % ("", "/key"),
        '"/label": {type: string, computed: {macro: concat, args: [lit::"T:", "/taxId"]}}',
    )
    first = compute_fields(schema, BEHAVIORS, FrozenStruct({"country": "us"}), effects=EffectView())
    assert [c.inputs for c in first.pending] == [(("/key", "US"),)]  # the hook sees the computed value, plain
    assert first.document["label"] == Annotated(("computed",), None)  # its input is missing for now
    fp = fingerprint("fx.echo@1", [("/key", "US")])
    second = compute_fields(
        schema, BEHAVIORS, FrozenStruct({"country": "us"}), effects=view_from_records([rec("/taxId", fp, 1)])
    )
    assert second.pending == () and second.document["label"] == Annotated(("computed",), "T:tax-US")
