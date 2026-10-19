"""Feature 002, research R-5 to R-7: P4 evaluates computed fields over a merged document, in dependency order."""

from decimal import Decimal

import pytest
from aduku.engine.compute import compute_fields

from aduku.behaviors.registry import BehaviorRegistry
from aduku.errors import ComputeFault
from aduku.ion_codec import ABSENT, Annotated, FrozenStruct, to_canonical
from aduku.schema.loader import load_schema

BEHAVIORS = BehaviorRegistry.with_builtins()


def schema_of(*decls: str):  # type: ignore[no-untyped-def]
    base = (
        '"/name": {type: string, merge: lww}, "/nickname": {type: string, merge: lww}, '
        '"/age": {type: int, merge: lww}, "/address": {type: struct, merge: struct}, '
        '"/address/city": {type: string, merge: lww}'
    )
    fields = ", ".join([base, *decls])
    return load_schema(f'schema::{{id: "t", version: 1, tiers: [a], fields: {{{fields}}}}}', BEHAVIORS)


def doc(**values: object) -> FrozenStruct:
    return FrozenStruct(values)


def computed(value: object) -> Annotated:
    return Annotated(("computed",), value)


UPPER = '"/displayName": {type: string, computed: {macro: upper, args: ["/name"]}}'


def test_a_computed_value_is_added_with_the_computed_annotation() -> None:
    result = compute_fields(schema_of(UPPER), BEHAVIORS, doc(name="Alice S."))
    assert result.document["displayName"] == computed("ALICE S.")
    assert result.document["name"] == "Alice S."  # the rest of the document is untouched
    assert to_canonical(result.document) == '{displayName:computed::"ALICE S.",name:"Alice S."}'


def test_a_chain_is_evaluated_in_dependency_order() -> None:
    schema = schema_of(
        '"/z": {type: string, computed: {macro: concat, args: ["/y", lit::"!"]}}',
        '"/y": {type: string, computed: {macro: concat, args: ["/displayName", lit::"?"]}}',
        UPPER,
    )
    result = compute_fields(schema, BEHAVIORS, doc(name="al"))
    assert [result.document[k] for k in ("displayName", "y", "z")] == [
        computed("AL"),
        computed("AL?"),
        computed("AL?!"),
    ]


def test_literals_and_several_inputs() -> None:
    schema = schema_of('"/g": {type: string, computed: {macro: concat, args: [lit::"Hello, ", "/name", lit::"!"]}}')
    assert compute_fields(schema, BEHAVIORS, doc(name="Pat")).document["g"] == computed("Hello, Pat!")


def test_a_computed_field_beneath_a_struct_goes_into_that_struct() -> None:
    schema = schema_of('"/address/shout": {type: string, computed: {macro: upper, args: ["/address/city"]}}')
    result = compute_fields(schema, BEHAVIORS, doc(address=doc(city="Paris")))
    assert result.document["address"] == doc(city="Paris", shout=computed("PARIS"))


def test_a_missing_input_stops_a_macro_that_does_not_accept_it_and_the_field_is_null() -> None:
    result = compute_fields(schema_of(UPPER), BEHAVIORS, doc())
    assert result.document["displayName"] == computed(None)
    assert result.results["/displayName"].status == "null"


def test_a_macro_that_accepts_missing_inputs_runs() -> None:
    schema = schema_of('"/who": {type: string, computed: {macro: coalesce, args: ["/nickname", "/name"]}}')
    assert compute_fields(schema, BEHAVIORS, doc(name="Pat")).document["who"] == computed("Pat")
    assert compute_fields(schema, BEHAVIORS, doc(nickname="P", name="Pat")).document["who"] == computed("P")
    assert compute_fields(schema, BEHAVIORS, doc()).document["who"] == computed(None)


def test_a_missing_computed_dependency_counts_as_missing() -> None:
    schema = schema_of(UPPER, '"/loud": {type: string, computed: {macro: upper, args: ["/displayName"]}}')
    assert compute_fields(schema, BEHAVIORS, doc()).document["loud"] == computed(None)


def test_the_result_records_its_source_and_the_values_it_was_computed_from() -> None:
    result = compute_fields(schema_of(UPPER), BEHAVIORS, doc(name="Alice S."))
    got = result.results["/displayName"]
    assert (got.status, got.value, got.source) == ("value", "ALICE S.", "upper@1")
    assert got.dependencies == (("/name", "Alice S."),)
    missing = compute_fields(schema_of(UPPER), BEHAVIORS, doc()).results["/displayName"]
    assert missing.dependencies == (("/name", ABSENT),)


def test_numbers_flow_through() -> None:
    schema = schema_of('"/double": {type: int, computed: {macro: sum, args: ["/age", "/age"]}}')
    assert compute_fields(schema, BEHAVIORS, doc(age=21)).document["double"] == computed(42)
    decimals = schema_of(
        '"/r": {type: decimal, computed: {macro: round, args: ["/d", lit::1]}}, "/d": {type: decimal, merge: lww}'
    )
    assert compute_fields(decimals, BEHAVIORS, doc(d=Decimal("2.25"))).document["r"] == computed(Decimal("2.2"))


def test_a_wrong_input_type_is_a_fault_naming_the_macro_never_null() -> None:
    schema = schema_of('"/bad": {type: string, computed: {macro: upper, args: ["/age"]}}')
    with pytest.raises(ComputeFault) as caught:
        compute_fields(schema, BEHAVIORS, doc(age=5))
    assert caught.value.macro == "upper@1" and "5" not in str(caught.value)  # the reason never repeats a value


def test_a_result_that_does_not_fit_the_declared_type_is_a_fault() -> None:
    schema = schema_of('"/n": {type: int, computed: {macro: coalesce, args: ["/name"]}}')
    with pytest.raises(ComputeFault):
        compute_fields(schema, BEHAVIORS, doc(name="text"))


def test_the_same_document_gives_the_same_result_every_time() -> None:
    schema = schema_of(UPPER, '"/who": {type: string, computed: {macro: coalesce, args: ["/nickname", "/name"]}}')
    first = compute_fields(schema, BEHAVIORS, doc(name="Pat"))
    second = compute_fields(schema, BEHAVIORS, doc(name="Pat"))
    assert first.document == second.document and to_canonical(first.document) == to_canonical(second.document)


def test_a_schema_without_computed_fields_returns_the_document_unchanged() -> None:
    schema = schema_of()
    original = doc(name="Pat")
    assert compute_fields(schema, BEHAVIORS, original).document is original


def test_a_null_result_counts_as_a_missing_input_downstream() -> None:
    """Null means no value: a macro that does not accept missing inputs is skipped, not faulted."""
    schema = schema_of(
        '"/who": {type: string, computed: {macro: coalesce, args: ["/nickname", "/name"]}}',
        '"/loud": {type: string, computed: {macro: upper, args: ["/who"]}}',
    )
    result = compute_fields(schema, BEHAVIORS, doc())
    assert result.document["who"] == computed(None) and result.document["loud"] == computed(None)
