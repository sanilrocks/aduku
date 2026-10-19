"""Feature 002, research R-2: deterministic limits stop an expression, and a fault never repeats a value."""

import pytest

from aduku.errors import SchemaError
from aduku.expr.evaluator import ExprError, ExprFault, compile_expression
from aduku.expr.limits import DEFAULTS, MAXIMUM, Limits


def compiled(text: str, params: list[str] | None = None, **limits: int):  # type: ignore[no-untyped-def]
    return compile_expression(text, params or [], Limits.for_macro(**limits) if limits else DEFAULTS)


def test_a_small_expression_is_well_within_the_defaults() -> None:
    assert compiled("1 + 2").run([]) == 3


def test_the_step_budget_stops_a_long_expression() -> None:
    long = " + ".join(["1"] * 200)
    assert compiled(long).run([]) == 200
    with pytest.raises(ExprFault) as caught:
        compiled(long, steps=50).run([])
    assert "step" in str(caught.value)


def test_work_inside_a_function_is_charged_by_the_items_it_processes() -> None:
    items = "[" + ", ".join(["1"] * 400) + "]"
    assert compiled(f"sum({items})").run([]) == 400
    with pytest.raises(ExprFault, match="step"):
        compiled(f"sum({items})", steps=300).run([])


def test_the_same_expression_gives_the_same_fault_every_time() -> None:
    program = compiled(" + ".join(["1"] * 200), steps=50)
    messages = set()
    for _ in range(3):
        with pytest.raises(ExprFault) as caught:
            program.run([])
        messages.add(str(caught.value))
    assert len(messages) == 1


def test_the_size_of_values_built_is_limited() -> None:
    program = compiled("s + s + s + s", ["s"], value_bytes=15_000)
    assert program.run(["x" * 100]) == "x" * 400
    with pytest.raises(ExprFault, match="value size"):
        program.run(["x" * 10_000])


def test_the_size_of_the_inputs_is_limited() -> None:
    program = compiled("size(s)", ["s"], input_bytes=1_000)
    assert program.run(["x" * 500]) == 500
    with pytest.raises(ExprFault, match="input size"):
        program.run(["x" * 1_500])


def test_the_length_of_the_expression_is_limited_at_compile_time() -> None:
    compiled("1 + 1", expression_chars=10)
    with pytest.raises(ExprError, match="length"):
        compiled("1 + 1 + 1 + 1 + 1", expression_chars=10)


def test_nesting_depth_is_limited_at_compile_time() -> None:
    deep = "(" * 20 + "1" + ")" * 20
    assert compiled(deep, depth=32).run([]) == 1
    with pytest.raises(ExprError, match="depth"):
        compiled(deep, depth=10)
    with pytest.raises(ExprError, match="depth"):  # also at the platform maximum
        compile_expression("(" * 70 + "1" + ")" * 70, [], MAXIMUM)


def test_a_fault_names_the_limit_and_never_a_value() -> None:
    program = compiled("s + s", ["s"], value_bytes=2_000)
    with pytest.raises(ExprFault) as caught:
        program.run(["SECRETVALUE" * 200])
    assert "SECRETVALUE" not in str(caught.value) and "value size" in str(caught.value)


def test_limits_above_the_maximum_cannot_be_asked_for() -> None:
    with pytest.raises(SchemaError):
        Limits.for_macro(steps=MAXIMUM.steps + 1)
