"""Validation rules (SPEC 10): pure behaviors over a document, reported as violations with (rule, path scope)."""

from decimal import Decimal

import pytest

from aduku.behaviors.registry import BehaviorRegistry
from aduku.behaviors.validate import Subject
from aduku.engine.validate import ValidatorFault, validate_document
from aduku.errors import SchemaError
from aduku.ion_codec import FrozenStruct, parse
from aduku.model import Violation
from aduku.schema.loader import load_schema
from tests.fixtures import max_spouses_by_country, register_fixtures
from tests.service_helpers import schema_text

BEHAVIORS = BehaviorRegistry.with_builtins()
register_fixtures(BEHAVIORS)


def run(name: str, value: object, **args: object) -> list[Violation]:
    behavior = BEHAVIORS.get(name, 1)
    assert behavior is not None and behavior.kind == "validate"
    return behavior.impl(Subject(FrozenStruct(), "/f", value, FrozenStruct(args)))  # type: ignore[no-any-return]


def test_the_presets_are_registered() -> None:
    assert {b.name for b in BEHAVIORS.all() if b.kind == "validate"} >= {"non_empty", "range", "max_len"}


def test_non_empty() -> None:
    for empty in ("", (), FrozenStruct()):
        assert [(v.rule_id, v.path_scope) for v in run("non_empty", empty)] == [("non_empty", "/f")]
    for fine in ("x", ("a",), FrozenStruct({"a": 1}), 0, False):
        assert run("non_empty", fine) == []


def test_range() -> None:
    for bad in (-1, 151, Decimal("150.01"), -0.5):
        assert [v.rule_id for v in run("range", bad, min=0, max=150)] == ["range"], bad
    for good in (0, 150, 75, Decimal("0.0"), 149.9):
        assert run("range", good, min=0, max=150) == [], good
    assert run("range", -100, max=5) == [] and run("range", 100, min=5) == []  # either bound may be left out
    assert [v.rule_id for v in run("range", 6, max=5)] == ["range"]
    assert (
        run("range", "text", min=0, max=1) == [] and run("range", True, min=5, max=6) == []
    )  # only numbers are ranged


def test_max_len() -> None:
    assert [v.rule_id for v in run("max_len", "abcd", n=3)] == ["max_len"]
    assert [v.rule_id for v in run("max_len", ("a", "b", "c", "d"), n=3)] == ["max_len"]
    assert run("max_len", "abc", n=3) == [] and run("max_len", (), n=0) == []


def test_messages_describe_the_rule_and_never_repeat_the_value() -> None:
    for violation in [*run("max_len", "SECRETSECRET", n=3), *run("range", 99999, min=0, max=5), *run("non_empty", "")]:
        assert "SECRET" not in violation.message and "99999" not in violation.message and violation.message


def test_the_fixture_document_rule() -> None:
    def check(text: str) -> list[Violation]:
        document = parse(text)
        return max_spouses_by_country(Subject(document, None, document, FrozenStruct()))  # type: ignore[arg-type]

    assert [(v.rule_id, v.path_scope) for v in check('{country:"US",spouses:["A","B"]}')] == [
        ("max_spouses_by_country", "/spouses")
    ]
    assert check('{country:"US",spouses:["A"]}') == [] and check('{country:"FR",spouses:["A","B"]}') == []


# -- validating a whole document --------------------------------------------------------------------------------------


SCHEMA = load_schema(
    schema_text(
        fields="""
  "/name": {type: string, merge: lww, validate: [non_empty, max_len::{n: 3}]},
  "/age": {type: int, merge: lww, validate: [range::{min: 0, max: 150}]},
  "/country": {type: string, merge: lww},
  "/spouses": {type: "list<string>", merge: lww},
  "/address": {type: struct, merge: struct},
  "/address/zip": {type: string, merge: lww, validate: [max_len::{n: 5}]},
  "/notes": {type: struct, merge: struct},
  "/notes/*": {type: string, merge: lww, validate: [non_empty]}
"""
    ).replace("fields:", 'document_validate: ["acme.max_spouses_by_country@1"], fields:'),
    BEHAVIORS,
)


def violations(text: str) -> list[tuple[str, str]]:
    return [(v.rule_id, v.path_scope) for v in validate_document(SCHEMA, BEHAVIORS, parse(text))]  # type: ignore[arg-type]


def test_a_valid_document_has_no_violations_and_absent_fields_are_not_validated() -> None:
    assert violations("{}") == []
    assert violations('{name:"Pat",age:30,address:{zip:"78701"}}') == []


def test_field_rules_run_on_the_value_at_each_present_path() -> None:
    assert violations('{name:"Patrick",age:-5}') == [("max_len", "/name"), ("range", "/age")]
    assert violations('{address:{zip:"1234567"}}') == [("max_len", "/address/zip")]


def test_a_wildcard_field_rule_applies_to_every_matching_path() -> None:
    assert violations('{notes:{a:"",b:"fine",c:""}}') == [("non_empty", "/notes/a"), ("non_empty", "/notes/c")]


def test_document_rules_see_the_whole_document() -> None:
    assert violations('{country:"US",spouses:["A","B"]}') == [("max_spouses_by_country", "/spouses")]


def test_violations_are_sorted_and_not_repeated() -> None:
    found = violations('{name:"",age:200,country:"US",spouses:["A","B"],notes:{z:"",a:""}}')
    assert found == sorted(set(found)) and len(found) == len(set(found))


def test_a_validator_that_faults_is_a_fault_never_no_violations() -> None:
    behaviors = BehaviorRegistry.with_builtins()
    behaviors.register_validate("boom", 1, lambda subject: 1 / 0)
    behaviors.register_validate("junk", 1, lambda subject: "not a list")
    for name in ("boom", "junk"):
        schema = load_schema(schema_text(fields=f'"/f": {{type: int, merge: lww, validate: [{name}]}}'), behaviors)
        with pytest.raises(ValidatorFault):
            validate_document(schema, behaviors, parse("{f: 1}"))  # type: ignore[arg-type]


def test_a_validator_the_service_does_not_have_is_a_schema_error() -> None:
    other = BehaviorRegistry.with_builtins()
    register_fixtures(other)
    schema = load_schema(schema_text(fields='"/f": {type: int, merge: lww, validate: [range::{min: 0}]}'), other)
    with pytest.raises(SchemaError):
        validate_document(schema, BehaviorRegistry(), parse("{f: 1}"))  # type: ignore[arg-type]


def test_a_validator_on_a_keyed_path_runs_on_the_keyed_value() -> None:
    schema = load_schema(
        schema_text(
            fields='"/items[a]": {type: int, merge: lww, validate: [range::{min: 10}]}, "/m": {type: int, merge: lww}'
        ),
        BEHAVIORS,
    )
    found = validate_document(schema, BEHAVIORS, FrozenStruct({"items[a]": 1, "m": 1}))
    assert [(v.rule_id, v.path_scope) for v in found] == [("range", "/items[a]")]


def test_a_field_validator_sees_only_its_own_value_not_the_rest_of_the_document() -> None:
    """Constitution VIII: a pure behavior sees only its declared dependencies."""
    seen: list[object] = []

    def spy(subject: Subject) -> list[Violation]:
        seen.append((subject.path, dict(subject.document)))
        return []

    behaviors = BehaviorRegistry.with_builtins()
    behaviors.register_validate("acme.spy", 1, spy)
    schema = load_schema(
        schema_text(fields='"/a": {type: int, merge: lww, validate: ["acme.spy@1"]}, "/b": {type: int, merge: lww}'),
        behaviors,
    )
    validate_document(schema, behaviors, FrozenStruct({"a": 1, "b": 2}))
    assert seen == [("/a", {})]  # the spy was called once, for /a, and the document it got was empty


def test_a_document_rule_still_sees_the_whole_document() -> None:
    seen: list[object] = []

    def spy(subject: Subject) -> list[Violation]:
        seen.append(dict(subject.document))
        return []

    behaviors = BehaviorRegistry.with_builtins()
    behaviors.register_validate("acme.docspy", 1, spy)
    schema = load_schema(
        schema_text(fields='"/a": {type: int, merge: lww}').replace(
            "fields:", 'document_validate: ["acme.docspy@1"], fields:'
        ),
        behaviors,
    )
    validate_document(schema, behaviors, FrozenStruct({"a": 1}))
    assert seen == [{"a": 1}]
