"""Feature 002, US3 and SC-006: a custom macro sees only its parameters and has no ambient authority."""

import pytest

from aduku.behaviors.registry import PRESET_NAMES, BehaviorRegistry
from aduku.errors import SchemaError
from aduku.expr.evaluator import ExprFault


def registry() -> BehaviorRegistry:
    return BehaviorRegistry.with_builtins()


def test_a_registered_macro_runs_over_its_parameters_only() -> None:
    behaviors = registry()
    behaviors.register_macro("acme.label", 1, ["name", "city"], 'name + " (" + city + ")"')
    macro = behaviors.get("acme.label", 1)
    assert macro is not None and macro.kind == "macro" and macro.impl.params == ("name", "city")
    assert macro.impl.call(["Alice S.", "Paris"]) == "Alice S. (Paris)"
    assert macro.impl.accepts_missing is False


def test_accepts_missing_is_declared_at_registration() -> None:
    behaviors = registry()
    behaviors.register_macro("acme.pick", 1, ["a", "b"], "coalesce(a, b)", accepts_missing=True)
    macro = behaviors.get("acme.pick", 1)
    assert macro is not None and macro.impl.accepts_missing is True and macro.impl.call([None, "b"]) == "b"


@pytest.mark.parametrize(
    "expression",
    [
        "other + 1",  # a name that is not a parameter
        'open("/etc/passwd")',
        '__import__("os")',
        'eval("1")',
        "now()",
        "random()",
        "document.name",
        "ledger",
        "x.open()",
    ],
)
def test_an_expression_that_reaches_for_anything_else_is_refused_at_registration(expression: str) -> None:
    with pytest.raises(SchemaError):
        registry().register_macro("acme.bad", 1, ["x"], expression)


def test_an_attribute_of_the_host_language_is_not_reachable() -> None:
    behaviors = registry()
    behaviors.register_macro("acme.probe", 1, ["x"], "x.__class__")
    macro = behaviors.get("acme.probe", 1)
    assert macro is not None
    with pytest.raises(ExprFault):
        macro.impl.call(["text"])


def test_the_result_depends_on_the_inputs_alone() -> None:
    behaviors = registry()
    behaviors.register_macro("acme.twice", 1, ["x"], "x + x")
    macro = behaviors.get("acme.twice", 1)
    assert macro is not None
    assert [macro.impl.call(["ab"]) for _ in range(3)] == ["abab"] * 3


@pytest.mark.parametrize("name", sorted(PRESET_NAMES))
def test_a_preset_name_cannot_be_registered(name: str) -> None:
    with pytest.raises(SchemaError, match="RESERVED_MACRO_NAME"):
        registry().register_macro(name, 1, ["x"], "x")


def test_the_same_name_and_version_cannot_be_registered_twice() -> None:
    behaviors = registry()
    behaviors.register_macro("acme.a", 1, ["x"], "x")
    with pytest.raises(SchemaError):
        behaviors.register_macro("acme.a", 1, ["x"], "x")
    behaviors.register_macro("acme.a", 2, ["x"], "x")  # a new version is fine


def test_parameters_must_be_distinct_plain_names() -> None:
    for params in (["a", "a"], ["not valid"], ["upper"], ["true"], [""]):
        with pytest.raises(SchemaError):
            registry().register_macro("acme.p", 1, params, "1")


def test_limits_are_taken_at_registration() -> None:
    behaviors = registry()
    behaviors.register_macro("acme.tight", 1, [], " + ".join(["1"] * 100), limits={"steps": 20})
    macro = behaviors.get("acme.tight", 1)
    assert macro is not None
    with pytest.raises(ExprFault, match="step"):
        macro.impl.call([])
    behaviors.register_macro("acme.roomy", 1, [], " + ".join(["1"] * 100), limits={"steps": 50_000})
    with pytest.raises(SchemaError):
        behaviors.register_macro("acme.toomuch", 1, [], "1", limits={"steps": 100_001})
    with pytest.raises(SchemaError):
        behaviors.register_macro("acme.unknown", 1, [], "1", limits={"minutes": 5})


def test_an_expression_that_does_not_parse_is_refused() -> None:
    with pytest.raises(SchemaError):
        registry().register_macro("acme.broken", 1, ["x"], "x +")
