"""Feature 002, contracts/schema-format.md: parsing of computed and impure declarations."""

import pytest

from aduku.behaviors.registry import BehaviorRegistry
from aduku.errors import SchemaError
from aduku.paths import parse_path
from aduku.schema.loader import load_schema

IMPURE = """{ type: string, on_missing: absent,
  computed: { impure: { hook: "tax.lookup@2", depends: ["/country", "/name"],
                        reason: "issued by the registry", timeout: "2s", retry: { max: 3, backoff: "200ms" },
                        deadline: "6s", recompute_by: [admin]%s } } }"""


def wrap(fields: str, top: str = "") -> str:
    return (
        f'schema::{{id: "t", version: 1, tiers: [backfill, user, admin], {top} fields: '
        f'{{"/name": {{type: string, merge: lww}}, "/country": {{type: string, merge: lww}}, {fields}}}}}'
    )


def hooks() -> BehaviorRegistry:
    behaviors = BehaviorRegistry.with_builtins()
    behaviors.register_hook("tax.lookup", 2, lambda inputs, timeout: "x")
    return behaviors


def field(text: str, path: str):  # type: ignore[no-untyped-def]
    return load_schema(text, hooks()).field_for(parse_path(path))


def problems_of(text: str) -> tuple[str, ...]:
    with pytest.raises(SchemaError) as caught:
        load_schema(text, hooks())
    return caught.value.problems


def test_a_preset_declaration_has_name_version_and_path_arguments() -> None:
    spec = field(wrap('"/up": {type: string, computed: {macro: upper, args: ["/name"]}}'), "/up")
    assert spec is not None and spec.computed is not None
    assert (spec.computed.macro, spec.computed.version) == ("upper", 1)
    assert [(a.path, a.is_literal) for a in spec.computed.args] == [("/name", False)]
    assert spec.derived == "computed" and spec.on_missing == "null"


def test_a_versioned_custom_macro_name_is_split() -> None:
    from aduku.behaviors.registry import BehaviorRegistry

    behaviors = BehaviorRegistry.with_builtins()
    behaviors.register_macro("acme.label", 2, ["name", "city"], 'name + " (" + city + ")"')
    text = wrap('"/l": {type: string, computed: {macro: "acme.label@2", args: ["/name", "/country"]}}')
    spec = load_schema(text, behaviors).field_for(parse_path("/l"))
    assert spec is not None and spec.computed is not None
    assert (spec.computed.macro, spec.computed.version) == ("acme.label", 2)


def test_literal_arguments_are_marked_with_lit() -> None:
    spec = field(
        wrap(
            '"/g": {type: string, computed: {macro: concat, args: [lit::"Hello, ", "/name", lit::2, lit::"/not a path"]}}'
        ),
        "/g",
    )
    assert spec is not None and spec.computed is not None
    args = spec.computed.args
    assert [a.is_literal for a in args] == [True, False, True, True]
    assert [a.path for a in args] == [None, "/name", None, None]
    assert [args[0].literal, args[2].literal, args[3].literal] == ["Hello, ", 2, "/not a path"]


@pytest.mark.parametrize("setting", ["null", "absent", "error"])
def test_on_missing_values(setting: str) -> None:
    spec = field(
        wrap(f'"/up": {{type: string, on_missing: {setting}, computed: {{macro: upper, args: ["/name"]}}}}'), "/up"
    )
    assert spec is not None and spec.on_missing == setting


def test_on_missing_must_be_one_of_the_three() -> None:
    found = problems_of(wrap('"/up": {type: string, on_missing: explode, computed: {macro: upper, args: ["/name"]}}'))
    assert any("on_missing" in p for p in found)


def test_the_impure_declaration_is_parsed_with_durations_in_seconds() -> None:
    spec = field(wrap('"/taxId": ' + IMPURE % ", failure_mode: model_error_on_failure"), "/taxId")
    assert spec is not None and spec.impure is not None
    impure = spec.impure
    assert (impure.hook, impure.version) == ("tax.lookup", 2)
    assert impure.depends == ("/country", "/name") and impure.reason == "issued by the registry"
    assert (impure.timeout, impure.retry_max, impure.backoff, impure.deadline) == (2.0, 3, 0.2, 6.0)
    assert impure.recompute_by == ("admin",) and impure.failure_mode == "model_error_on_failure"
    assert spec.derived == "computed" and spec.on_missing == "absent"


def test_failure_mode_defaults_to_none_so_the_schema_default_applies() -> None:
    spec = field(wrap('"/taxId": ' + IMPURE % ""), "/taxId")
    assert spec is not None and spec.impure is not None and spec.impure.failure_mode is None


@pytest.mark.parametrize("key", ["hook", "depends", "reason", "timeout", "retry", "deadline", "recompute_by"])
def test_every_required_impure_key_is_reported_when_missing(key: str) -> None:
    text = IMPURE % ""
    # drop the key from the declaration
    pieces = {
        "hook": 'hook: "tax.lookup@2", ',
        "depends": 'depends: ["/country", "/name"],\n                        ',
        "reason": 'reason: "issued by the registry", ',
        "timeout": 'timeout: "2s", ',
        "retry": 'retry: { max: 3, backoff: "200ms" },\n                        ',
        "deadline": 'deadline: "6s", ',
        "recompute_by": "recompute_by: [admin]",
    }
    text = text.replace(pieces[key], "")
    found = problems_of(wrap('"/taxId": ' + text))
    assert any(p.startswith("IMPURE_DECLARATION_INCOMPLETE") and key in p for p in found), found


@pytest.mark.parametrize(
    "change",
    [
        ('timeout: "2s"', 'timeout: "31s"'),
        ("max: 3", "max: 6"),
        ('deadline: "6s"', 'deadline: "61s"'),
        ('deadline: "6s"', 'deadline: "1s"'),  # below the per-attempt timeout
        ('timeout: "2s"', 'timeout: "0s"'),
    ],
)
def test_platform_bounds_on_timeout_retry_and_deadline(change: tuple[str, str]) -> None:
    text = (IMPURE % "").replace(*change)
    assert problems_of(wrap('"/taxId": ' + text))


def test_recompute_by_uses_the_authority_vocabulary() -> None:
    bad = (IMPURE % "").replace("recompute_by: [admin]", "recompute_by: [nobody]")
    assert any(p.startswith("UNKNOWN_TIER") for p in problems_of(wrap('"/taxId": ' + bad)))
    ok = (IMPURE % "").replace("recompute_by: [admin]", "recompute_by: [auditor]")
    assert field(wrap('"/taxId": ' + ok, top="roles: [auditor],"), "/taxId") is not None


def test_failure_mode_values_are_checked() -> None:
    assert problems_of(wrap('"/taxId": ' + IMPURE % ", failure_mode: sometimes"))


def test_a_computed_field_needs_a_macro_or_an_impure_declaration_but_not_both() -> None:
    assert problems_of(wrap('"/x": {type: string, computed: {}}'))
    both = IMPURE % ""
    both = both.replace("computed: { impure:", 'computed: { macro: upper, args: ["/name"], impure:')
    assert problems_of(wrap('"/x": ' + both))


def test_the_existing_derived_checks_still_apply() -> None:
    found = problems_of(wrap('"/up": {type: string, merge: lww, computed: {macro: upper, args: ["/name"]}}'))
    assert any(p.startswith("DERIVED_HAS_MERGE_OR_DEFAULT") for p in found)
