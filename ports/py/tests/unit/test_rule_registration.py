"""Feature 004, research R-1: a rule is registered with typed arguments, and a use is checked against them (FR-009, FR-013)."""

from decimal import Decimal

import pytest

from aduku.behaviors.registry import ArgSpec, BehaviorRegistry, arg_problems
from aduku.errors import SchemaError
from aduku.ion_codec import FrozenStruct


def rule(*specs: ArgSpec, applies: str = "either", check_args=None):  # type: ignore[no-untyped-def]
    registry = BehaviorRegistry.with_builtins()
    return registry.register_validate("acme.rule", 1, lambda subject: [], check_args, args=specs, applies=applies)  # type: ignore[arg-type]


def problems(behavior, **given: object) -> list[str]:  # type: ignore[no-untyped-def]
    return arg_problems(behavior, FrozenStruct(given))


def test_a_registration_without_arguments_is_unchanged() -> None:
    behavior = BehaviorRegistry.with_builtins().register_validate("acme.old", 1, lambda subject: [])
    assert (behavior.args, behavior.applies) == (None, "either") and arg_problems(
        behavior, FrozenStruct({"x": 1})
    ) == []


def test_unknown_missing_and_optional_arguments() -> None:
    behavior = rule(ArgSpec("n", "int"), ArgSpec("note", "text", required=False))
    assert problems(behavior, n=3) == [] and problems(behavior, n=3, note="x") == []
    assert any("unknown argument 'extra'" in p for p in problems(behavior, n=3, extra=1))
    assert any("n is required" in p for p in problems(behavior))


@pytest.mark.parametrize(
    "kind, good, bad",
    [
        ("int", [0, 7], ["7", 1.5, Decimal("1"), True, None]),
        ("number", [1, 2.5, Decimal("3.5")], ["1", True, None]),
        ("text", ["a", ""], [1, None, ("a",)]),
        ("bool", [True, False], [1, "true", None]),
        ("path", ["/a", "/a/b"], ["a", "", 3, "/a//b"]),
        ("paths", [("/a",), ("/a", "/b")], ["/a", ("a",), (1,), None]),
        ("values", [("a", 1, True, None, Decimal("1.5")), ()], ["a", (("x",),), (FrozenStruct({"a": 1}),)]),
        ("pattern", ["[A-Z]{2}", "a|b"], ["(a", "a**", 5]),
    ],
)  # fmt: skip
def test_each_kind_accepts_its_values_and_refuses_the_others(kind: str, good: list[object], bad: list[object]) -> None:
    behavior = rule(ArgSpec("v", kind))
    for value in good:
        assert problems(behavior, v=value) == [], (kind, value)
    for value in bad:
        assert problems(behavior, v=value), (kind, value)


def test_bounds_choices_and_the_least_number_of_items() -> None:
    behavior = rule(
        ArgSpec("n", "int", min=0, max=10),
        ArgSpec("mode", "text", required=False, choices=("a", "b")),
        ArgSpec("paths", "paths", min=2),
        ArgSpec("one_of", "values", min=1, required=False),
    )
    ok = {"n": 5, "paths": ("/a", "/b")}
    assert problems(behavior, **ok) == []
    assert problems(behavior, **{**ok, "n": -1}) and problems(behavior, **{**ok, "n": 11})
    assert problems(behavior, **{**ok, "mode": "c"}) and problems(behavior, **{**ok, "mode": "a"}) == []
    assert problems(behavior, **{**ok, "paths": ("/a",)}) and problems(behavior, **{**ok, "one_of": ()})


def test_a_bad_pattern_is_reported_with_the_reason_and_no_value() -> None:
    behavior = rule(ArgSpec("regex", "pattern"))
    (found,) = problems(behavior, regex="(a|b")
    assert "regex" in found and "parenthesis" in found


def test_check_args_still_runs_after_the_declared_checks() -> None:
    behavior = rule(
        ArgSpec("lo", "int"), ArgSpec("hi", "int"), check_args=lambda a: ["lo is above hi"] if a["lo"] > a["hi"] else []
    )
    assert problems(behavior, lo=1, hi=2) == [] and problems(behavior, lo=3, hi=2) == ["lo is above hi"]
    assert len(problems(behavior, lo="x", hi=2)) == 1  # the declared check failed, so the combination is not tried


def test_applies_is_stored_and_must_be_known() -> None:
    assert rule(applies="field").applies == "field" and rule(applies="document").applies == "document"
    with pytest.raises(SchemaError):
        rule(applies="everywhere")


@pytest.mark.parametrize(
    "specs",
    [
        (ArgSpec("v", "colour"),),
        (ArgSpec("v", "int"), ArgSpec("v", "text")),
        (ArgSpec("v", "text", choices=(1,)),),
        (ArgSpec("v", "int", min=5, max=1),),
    ],
)
def test_a_faulty_declaration_is_refused_at_registration(specs: tuple[ArgSpec, ...]) -> None:
    with pytest.raises(SchemaError):
        rule(*specs)


def test_the_three_original_rules_are_declared_field_rules_with_the_same_arguments() -> None:
    registry = BehaviorRegistry.with_builtins()
    for name in ("non_empty", "range", "max_len"):
        behavior = registry.get(name, 1)
        assert behavior is not None and behavior.applies == "field" and behavior.args is not None
    assert [s.name for s in registry.get("range", 1).args] == ["min", "max"]  # type: ignore[union-attr]
    assert arg_problems(registry.get("range", 1), FrozenStruct({"min": 5, "max": 1})) == ["min is greater than max"]  # type: ignore[arg-type]
    assert any("n is required" in p for p in arg_problems(registry.get("max_len", 1), FrozenStruct()))  # type: ignore[arg-type]
