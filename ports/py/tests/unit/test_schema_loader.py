"""Schema loading (SPEC 5, contracts/schema-format.md). Behavior-level rules (FR-011, FR-021) arrive with US3."""

import pathlib
from decimal import Decimal

import pytest

from aduku.behaviors.registry import BehaviorRegistry, Laws
from aduku.errors import SchemaError
from aduku.ion_codec import FrozenStruct
from aduku.paths import parse_path
from aduku.schema.loader import load_schema
from aduku.schema.model import conforms, parse_type

ROOT = pathlib.Path(__file__).resolve().parents[4]
CUSTOMER = (ROOT / "conformance" / "customer.schema.ion").read_text()


def test_loads_customer_schema() -> None:
    schema = load_schema(CUSTOMER)
    assert schema.id == "customer"
    assert schema.version == 7
    assert schema.tiers == ("backfill", "user", "admin")
    assert schema.tier_rank("backfill") == 0 and schema.tier_rank("admin") == 2
    assert schema.lenient_tiers == ("backfill",) and schema.lenient_contributors == ()
    audit = schema.rejection_audit
    assert audit.enabled and audit.mode == "full"
    assert audit.max_entries == 1000 and audit.max_age_seconds == 90 * 86400
    assert schema.document_validate == ()


def test_field_lookup_prefers_most_specific_pattern() -> None:
    schema = load_schema(CUSTOMER)
    assert schema.field_for(parse_path("/address/city")).pattern == "/address/city"  # type: ignore[union-attr]
    assert schema.field_for(parse_path("/phone")).type == "string"  # type: ignore[union-attr]
    fallback = schema.field_for(parse_path("/unlisted"))
    assert fallback is not None and fallback.pattern == "/*" and fallback.type is None
    assert schema.field_for(parse_path("/a/b/c")) is None


def test_field_details() -> None:
    schema = load_schema(CUSTOMER)
    name = schema.field_for(parse_path("/name"))
    assert name is not None
    assert name.merge is not None and name.merge.name == "lww" and name.merge.version == 1
    assert [ref.name for ref in name.validate] == ["non_empty", "max_len"]
    assert name.validate[1].args == FrozenStruct({"n": 120})
    currency = schema.field_for(parse_path("/currency"))
    assert currency is not None and currency.default == "USD" and currency.on_delete == "absent"
    plan = schema.field_for(parse_path("/plan"))
    assert plan is not None and plan.authority == ("admin",)
    derived = schema.field_for(parse_path("/displayName"))
    assert derived is not None and derived.derived == "computed" and derived.merge is None


def test_custom_behavior_reference_with_version() -> None:
    registry = BehaviorRegistry.with_builtins()
    registry.register_merge("acme.merge", 3, lambda path, old, new, ctx: new, Laws(associative=True))
    text = 'schema::{id:"x",version:1,tiers:[a],fields:{"/f":{type:string,merge:custom::"acme.merge@3"}}}'
    field = load_schema(text, registry).field_for(parse_path("/f"))
    assert field is not None and field.merge is not None
    assert (field.merge.name, field.merge.version, field.merge.custom) == ("acme.merge", 3, True)


def test_defaults_to_audit_off_and_absent_on_delete() -> None:
    schema = load_schema('schema::{id:"x",version:1,tiers:[a],fields:{"/f":{type:string,default:"d"}}}')
    assert not schema.rejection_audit.enabled and schema.rejection_audit.mode == "full"
    field = schema.field_for(parse_path("/f"))
    assert field is not None and field.effective_on_delete == "absent" and field.on_delete is None


def test_problems_are_reported_together() -> None:
    text = """schema::{
        version: "one",
        tiers: [a, a],
        rejection_audit: { enabled: true, mode: sideways, retention: { max_age: "ninety days" } },
        fields: { "/f": { type: "list<>", on_delete: sometimes, bogus: 1 } },
        surprise: true
    }"""
    with pytest.raises(SchemaError) as caught:
        load_schema(text)
    joined = " | ".join(caught.value.problems)
    for needle in ("id", "version", "duplicate tier", "mode", "max_age", "list<>", "on_delete", "bogus", "surprise"):
        assert needle in joined, needle
    assert len(caught.value.problems) >= 8


def test_not_a_schema_document() -> None:
    with pytest.raises(SchemaError):
        load_schema("{id: 1}")
    with pytest.raises(SchemaError):
        load_schema("this is not ion {")


def test_type_parsing_and_conformance() -> None:
    assert parse_type("string") == ("string", None)
    assert parse_type("list<string>") == ("list", "string")
    with pytest.raises(ValueError):
        parse_type("list<>")
    with pytest.raises(ValueError):
        parse_type("blob")
    assert conforms("string", "x") and not conforms("string", 1)
    assert conforms("int", 5) and not conforms("int", True) and not conforms("int", Decimal("5"))
    assert conforms("decimal", Decimal("1.5")) and not conforms("decimal", 1.5)
    assert conforms("bool", True) and not conforms("bool", 1)
    assert conforms("list<string>", ("a", "b")) and not conforms("list<string>", ("a", 1))
    assert conforms("list", (1, "a"))
    assert conforms("struct", FrozenStruct({"a": 1})) and not conforms("struct", ("a",))
    assert not conforms("string", None)
    assert conforms("any", None) and conforms("any", 3)


def test_ops_redact_and_failure_mode_are_kept() -> None:
    text = (
        'schema::{id:"x",version:1,tiers:[a],fields:{"/ssn":{type:string,ops:[set],redact:true},'
        '"/n":{type:string,computed:{impure:{hook:"h",depends:["/ssn"],reason:"r",timeout:"1s",retry:{max:1,backoff:"10ms"},deadline:"3s",recompute_by:[a]}},failure_mode:model_error_on_failure}}}'
    )
    behaviors = BehaviorRegistry.with_builtins()
    behaviors.register_hook("h", 1, lambda inputs, timeout: "x")
    schema = load_schema(text, behaviors)
    ssn = schema.field_for(parse_path("/ssn"))
    assert ssn is not None and ssn.ops == ("set",) and ssn.redact is True
    other = schema.field_for(parse_path("/n"))
    assert other is not None and other.ops is None and other.failure_mode == "model_error_on_failure"


def test_unknown_op_and_failure_mode_are_problems() -> None:
    text = 'schema::{id:"x",version:1,tiers:[a],fields:{"/f":{type:string,ops:[set,explode],failure_mode:explode}}}'
    with pytest.raises(SchemaError) as caught:
        load_schema(text)
    joined = " | ".join(caught.value.problems)
    assert "explode" in joined and "ops" in joined and "failure_mode" in joined


def test_values_that_cannot_be_stored_do_not_conform() -> None:
    assert not conforms("any", b"raw")
    assert not conforms("struct", FrozenStruct({"a": b"raw"}))
    assert not conforms("list", (b"raw",))
    assert not conforms("list<any>", (b"raw",))
    assert conforms("any", FrozenStruct({"a": (1, "x", None)}))


def test_field_lookup_parses_patterns_once(monkeypatch: pytest.MonkeyPatch) -> None:
    import aduku.schema.model as model

    schema = load_schema(CUSTOMER)
    assert schema.field_for(parse_path("/name")) is not None  # warm any cache
    calls: list[str] = []
    real = model.parse_pattern
    monkeypatch.setattr(model, "parse_pattern", lambda text: calls.append(text) or real(text))
    for _ in range(50):
        schema.field_for(parse_path("/address/city"))
    assert calls == []


def test_on_delete_remembers_whether_it_was_declared() -> None:
    schema = load_schema(
        'schema::{id:"x",version:1,tiers:[a],fields:{"/p":{type:string,default:"d"},'
        '"/q":{type:string,default:"d",on_delete:absent},"/r":{type:string,default:"d",on_delete:default}}}'
    )
    p, q, r = (schema.field_for(parse_path(x)) for x in ("/p", "/q", "/r"))
    assert (p.on_delete, q.on_delete, r.on_delete) == (None, "absent", "default")  # type: ignore[union-attr]
    assert p.effective_on_delete == "absent"  # type: ignore[union-attr]
