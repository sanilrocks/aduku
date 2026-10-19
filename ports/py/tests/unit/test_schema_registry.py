"""Principle IX: schema versions are immutable; activation and per-document pinning."""

import pytest

from aduku.errors import ErrorCode, RejectedError, SchemaError
from aduku.schema.loader import load_schema
from aduku.schema.registry import SchemaRegistry


def schema(version: int, extra: str = "") -> object:
    return load_schema(
        f'schema::{{id:"customer",version:{version},tiers:[a,b],fields:{{"/f":{{type:string,merge:lww{extra}}}}}}}'
    )


def test_put_get_and_activate() -> None:
    registry = SchemaRegistry()
    registry.put(schema(1))  # type: ignore[arg-type]
    registry.put(schema(2))  # type: ignore[arg-type]
    assert registry.get(1).version == 1
    registry.activate(2)
    assert registry.active().version == 2


def test_putting_identical_content_again_is_a_no_op() -> None:
    registry = SchemaRegistry()
    registry.put(schema(1))  # type: ignore[arg-type]
    registry.put(schema(1))  # type: ignore[arg-type]


def test_versions_are_immutable() -> None:
    registry = SchemaRegistry()
    registry.put(schema(1))  # type: ignore[arg-type]
    with pytest.raises(SchemaError):
        registry.put(schema(1, ", default:'x'"))  # type: ignore[arg-type]


def test_registry_holds_one_schema_id() -> None:
    registry = SchemaRegistry()
    registry.put(schema(1))  # type: ignore[arg-type]
    other = load_schema('schema::{id:"other",version:1,tiers:[a],fields:{}}')
    with pytest.raises(SchemaError):
        registry.put(other)


def test_unknown_or_inactive_version_is_schema_inactive() -> None:
    registry = SchemaRegistry()
    with pytest.raises(RejectedError) as caught:
        registry.active()
    assert caught.value.code is ErrorCode.SCHEMA_INACTIVE
    registry.put(schema(1))  # type: ignore[arg-type]
    with pytest.raises(RejectedError) as caught:
        registry.activate(9)
    assert caught.value.code is ErrorCode.SCHEMA_INACTIVE
    with pytest.raises(RejectedError):
        registry.get(9)


def test_pin_a_document_to_a_version() -> None:
    registry = SchemaRegistry()
    registry.put(schema(1))  # type: ignore[arg-type]
    registry.put(schema(2))  # type: ignore[arg-type]
    registry.activate(2)
    assert registry.version_for("d1").version == 2
    registry.pin("d1", 1)
    assert registry.version_for("d1").version == 1
    assert registry.version_for("d2").version == 2
    registry.pin("d1", None)
    assert registry.version_for("d1").version == 2
    with pytest.raises(RejectedError):
        registry.pin("d1", 7)


def _with_default(literal: str) -> object:
    return load_schema(f'schema::{{id:"customer",version:1,tiers:[a],fields:{{"/f":{{type:any,default:{literal}}}}}}}')


@pytest.mark.parametrize(("first", "second"), [("true", "1"), ("1", "1.0"), ("1", "1."), ('"a"', "a"), ("1.5", "1.50")])
def test_equality_is_type_aware(first: str, second: str) -> None:
    registry = SchemaRegistry()
    registry.put(_with_default(first))  # type: ignore[arg-type]
    with pytest.raises(SchemaError):
        registry.put(_with_default(second))  # type: ignore[arg-type]


def test_identical_content_in_a_different_field_order_is_the_same_version() -> None:
    one = load_schema('schema::{id:"c",version:1,tiers:[a],fields:{"/f":{type:any,default:{x:1,y:2}}}}')
    two = load_schema('schema::{id:"c",version:1,tiers:[a],fields:{"/f":{type:any,default:{y:2,x:1}}}}')
    registry = SchemaRegistry()
    registry.put(one)
    registry.put(two)


def test_re_registering_identical_content_keeps_the_original_object() -> None:
    registry = SchemaRegistry()
    first = schema(1)
    registry.put(first)  # type: ignore[arg-type]
    registry.put(schema(1))  # type: ignore[arg-type]
    assert registry.get(1) is first


SCHEMA_TEXT = 'schema::{id:"customer",version:3,tiers:[a,b],fields:{"/f":{type:string,merge:lww}}}'


def test_load_reads_schema_text_without_registering_it() -> None:
    registry = SchemaRegistry()
    loaded = registry.load(SCHEMA_TEXT)
    assert (loaded.id, loaded.version) == ("customer", 3) and registry.versions() == ()


def test_load_reads_a_file_by_path(tmp_path) -> None:  # type: ignore[no-untyped-def]
    file = tmp_path / "customer.schema.ion"
    file.write_text(SCHEMA_TEXT)
    registry = SchemaRegistry()
    assert registry.load(file).version == 3 and registry.load(str(file)).version == 3


def test_load_fails_closed_listing_every_problem() -> None:
    bad = 'schema::{id:"t",version:1,tiers:[a,a],fields:{"/x":{type:int,merge:telepathy}}}'
    with pytest.raises(SchemaError) as caught:
        SchemaRegistry().load(bad)
    assert {"DUPLICATE_TIER", "UNKNOWN_BEHAVIOR"} <= set(caught.value.codes)


def test_load_of_a_missing_file_is_a_schema_error(tmp_path) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(SchemaError):
        SchemaRegistry().load(tmp_path / "nope.ion")


def test_a_loaded_schema_can_then_be_registered() -> None:
    registry = SchemaRegistry()
    registry.put(registry.load(SCHEMA_TEXT))
    registry.activate(3)
    assert registry.active().version == 3
