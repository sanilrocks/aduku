"""Feature 004, FR-009, FR-021: the loader checks every use of a rule against its registration, and every authority list."""

import pytest

from aduku.behaviors.registry import ArgSpec, BehaviorRegistry
from aduku.errors import SchemaError
from aduku.schema.loader import load_schema

A_FIELD = "{type: int, merge: lww}"
BASE = f'"/name": {{type: string, merge: lww}}, "/a": {A_FIELD}, "/b": {A_FIELD}'
DEFERRED = '"/score": {type: int, deferred: {hook: "h@1", depends: ["/name"], refresh: once, invalidate_by: [admin]}}'


def on_a(extra: str) -> str:
    """The fields, with `extra` (validate rules, authority, ...) added to `/a`."""
    return BASE.replace(f'"/a": {A_FIELD}', f'"/a": {{type: int, merge: lww, {extra}}}')


def validate(*uses: str) -> str:
    return "validate: [" + ", ".join(uses) + "]"


def behaviors() -> BehaviorRegistry:
    registry = BehaviorRegistry.with_builtins()
    registry.register_hook("h", 1, lambda inputs, timeout: 1)
    registry.register_validate("acme.field", 1, lambda s: [], args=(ArgSpec("n", "int", min=0),), applies="field")
    registry.register_validate(
        "acme.together", 1, lambda s: [], args=(ArgSpec("paths", "paths", min=2),), applies="document"
    )
    registry.register_validate(
        "acme.before", 1, lambda s: [], args=(ArgSpec("first", "path"), ArgSpec("second", "path")), applies="document"
    )
    registry.register_validate("acme.either", 1, lambda s: [], args=(ArgSpec("path", "path", required=False),))
    registry.register_validate("acme.with_path", 1, lambda s: [], args=(ArgSpec("other", "path"),), applies="field")
    return registry


def wrap(fields: str = BASE, rules: str = "") -> str:
    document = f"document_validate: [{rules}]," if rules else ""
    return f'schema::{{id: "t", version: 1, tiers: [user, admin], roles: [finance], {document} fields: {{{fields}}}}}'


def problems_of(text: str) -> list[str]:
    with pytest.raises(SchemaError) as caught:
        load_schema(text, behaviors())
    return list(caught.value.problems)


def codes_of(text: str) -> set[str]:
    return {p.split(":")[0] for p in problems_of(text)}


TOGETHER = """'acme.together'::{paths: ["/a", "/b"]}"""
BEFORE = """'acme.before'::{first: "/a", second: "/b"}"""


def test_a_correct_use_loads() -> None:
    fields = on_a(validate("""'acme.field'::{n: 3}"""))
    load_schema(wrap(fields, f"{TOGETHER}, {BEFORE}"), behaviors())


def test_a_document_rule_on_a_field_and_a_field_rule_in_the_document_list_are_in_the_wrong_place() -> None:
    assert "RULE_WRONG_PLACE" in codes_of(wrap(on_a(validate(TOGETHER))))
    assert "RULE_WRONG_PLACE" in codes_of(wrap(rules="""'acme.field'::{n: 1}"""))
    assert "RULE_WRONG_PLACE" in codes_of(wrap(rules="range::{min: 1}"))  # the original rules are field rules
    load_schema(wrap(rules='"acme.either"'), behaviors())  # a rule that applies either way is fine in both places
    load_schema(wrap(on_a(validate('"acme.either"'))), behaviors())


@pytest.mark.parametrize(
    "use, fragment",
    [
        ("""'acme.field'::{n: 1, extra: 2}""", "unknown argument 'extra'"),
        ('"acme.field"', "n is required"),
        ("""'acme.field'::{n: -1}""", "at least 0"),
        ("""'acme.field'::{n: "3"}""", "whole number"),
    ],
)
def test_argument_problems_are_reported_with_the_rule_named(use: str, fragment: str) -> None:
    found = [p for p in problems_of(wrap(on_a(validate(use)))) if p.startswith("BAD_BEHAVIOR_ARGS")]
    assert found and fragment in found[0] and "acme.field@1" in found[0] and "/a" in found[0]


def test_a_paths_list_shorter_than_the_rule_needs_is_refused() -> None:
    assert "BAD_BEHAVIOR_ARGS" in codes_of(wrap(rules="""'acme.together'::{paths: ["/a"]}"""))


def test_a_path_argument_that_no_field_declares_is_refused() -> None:
    fields = f'"/a": {A_FIELD}, "/b": {A_FIELD}'
    found = problems_of(wrap(fields, """'acme.together'::{paths: ["/a", "/nope"]}"""))
    assert any(p.startswith("UNKNOWN_RULE_PATH") and "/nope" in p for p in found)
    load_schema(
        wrap(fields + ', "/s": {type: struct, merge: struct}', """'acme.together'::{paths: ["/a", "/s"]}"""),
        behaviors(),
    )
    assert "UNKNOWN_RULE_PATH" in codes_of(wrap(fields, """'acme.before'::{first: "/a", second: "/zzz"}"""))


def test_a_rule_that_reads_a_deferred_field_is_refused_through_any_path_argument() -> None:
    fields = f"{BASE}, {DEFERRED}"
    assert "DEPENDS_ON_DEFERRED" in codes_of(wrap(fields, """'acme.together'::{paths: ["/a", "/score"]}"""))
    assert "DEPENDS_ON_DEFERRED" in codes_of(wrap(fields, """'acme.before'::{first: "/score", second: "/a"}"""))
    use = """'acme.with_path'::{other: "/score"}"""
    assert "DEPENDS_ON_DEFERRED" in codes_of(wrap(f"{on_a(validate(use))}, {DEFERRED}"))


def test_two_uses_of_a_document_rule_with_the_same_first_path_are_the_same_error() -> None:
    same = """'acme.together'::{paths: ["/a", "/b"]}, 'acme.together'::{paths: ["/a", "/name"]}"""
    found = problems_of(wrap(rules=same))
    assert any(p.startswith("DUPLICATE_RULE_SCOPE") and "/a" in p for p in found)
    load_schema(
        wrap(rules="""'acme.together'::{paths: ["/a", "/b"]}, 'acme.together'::{paths: ["/b", "/a"]}"""), behaviors()
    )
    load_schema(
        wrap(rules=f"{TOGETHER}, {BEFORE}"), behaviors()
    )  # different rules over the same paths: separate errors


def test_an_unregistered_rule_or_version_is_unknown_behavior() -> None:
    assert "UNKNOWN_BEHAVIOR" in codes_of(wrap(rules='"acme.nobody"'))
    assert "UNKNOWN_BEHAVIOR" in codes_of(wrap(rules="""'acme.together@2'::{paths: ["/a", "/b"]}"""))


def test_an_empty_authority_list_and_an_unknown_name_are_refused() -> None:
    assert "EMPTY_AUTHORITY" in codes_of(wrap(on_a("authority: []")))
    assert "UNKNOWN_TIER" in codes_of(wrap(on_a("authority: [wizard]")))
    load_schema(wrap(on_a("authority: [admin, finance]")), behaviors())


def test_every_problem_is_reported_together() -> None:
    found = codes_of(
        wrap(
            on_a(f"authority: [], {validate(chr(34) + 'acme.field' + chr(34))}"),
            """'acme.together'::{paths: ["/a"]}, "acme.nobody\"""",
        )
    )
    assert {"EMPTY_AUTHORITY", "BAD_BEHAVIOR_ARGS", "UNKNOWN_BEHAVIOR"} <= found


# -- review fixes ----------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "use",
    [
        """'acme.together'::{paths: 7}""",
        """'acme.together'::{paths: [1, 2]}""",
        """'acme.together'::{paths: "/a"}""",
        """'acme.together'::{paths: null}""",
        """'acme.before'::{first: 5, second: "/b"}""",
        """'acme.before'::{first: null, second: "/b"}""",
        """'acme.before'::{first: ["/a"], second: "/b"}""",
    ],
)
def test_a_malformed_path_argument_is_a_reported_problem_not_a_crash(use: str) -> None:
    found = problems_of(wrap(rules=use))
    assert any(p.startswith("BAD_BEHAVIOR_ARGS") for p in found)


def test_a_paths_list_may_not_name_a_path_twice() -> None:
    found = problems_of(wrap(rules="""'acme.together'::{paths: ["/a", "/b", "/a"]}"""))
    assert any(p.startswith("BAD_BEHAVIOR_ARGS") and "/a" in p and "twice" in p for p in found)
