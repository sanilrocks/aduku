"""Feature 002, contracts/expression-language.md: the grammar and the meaning of the sandboxed expression language."""

from decimal import Decimal

import pytest

from aduku.expr.evaluator import ExprError, ExprFault, compile_expression
from aduku.ion_codec import FrozenStruct


def run(text: str, **variables: object) -> object:
    return compile_expression(text, sorted(variables)).run([variables[name] for name in sorted(variables)])


@pytest.mark.parametrize(
    "text, expected",
    [
        ("1 + 2 * 3", 7),
        ("(1 + 2) * 3", 9),
        ("10 - 3 - 2", 5),
        ("2 * 3 % 4", 2),
        ("-7 / 2", -3),  # integer division truncates toward zero
        ("-7 % 3", -1),
        ("7 % 3", 1),
        ("-(2 + 3)", -5),
        ('"a" + "b"', "ab"),
        ("[1, 2] + [3]", (1, 2, 3)),
        ("2.5d + 1d", Decimal("3.5")),
        ("1d / 4d", Decimal("0.25")),
        ("1 + 1.5d", Decimal("2.5")),
        ('"a\\nb"', "a\nb"),
        ('"\\u00e9"', "é"),
        ('"q\\"q"', 'q"q'),
        ("true", True),
        ("null", None),
        ("[]", ()),
        ('{"a": 1}["a"]', 1),
        ('{"a": 1}.a', 1),
        ("[10, 20][1]", 20),
        ("1 < 2 && \"a\" < \"b\"", True),
        ("1 < 2d", True),
        ("!true", False),
        ("false || true", True),
        ("null == null", True),
        ("1 == 1d", True),
        ('"1" == 1', False),
        ("[1, 2] == [1, 2]", True),
        ("1 != 2", True),
        ("2 in [1, 2]", True),
        ('"k" in {"k": 1}', True),
        ('"z" in {"k": 1}', False),
        ('1 > 2 ? "big" : "small"', "small"),
        ("true ? 1 : 2 + 3", 1),
        ('has({"k": 1}.k)', True),
        ('has({"k": 1}.z)', False),
        ('size("héllo")', 5),
        ("size([1, 2])", 2),
        ('upper("a")', "A"),
        ('"a".upper()', "A"),
        ('concat("a", 1)', "a1"),
        ("coalesce(null, 3)", 3),
        ('join(["a", "b"], "-")', "a-b"),
        ("sum([1, 2, 3])", 6),
        ("max([1, 5, 2])", 5),
        ("if(1 < 2, \"y\", \"n\")", "y"),
    ],
)  # fmt: skip
def test_expressions(text: str, expected: object) -> None:
    got = run(text)
    assert got == expected and type(got) is type(expected)


def test_parameters_are_the_only_variables() -> None:
    assert run('name + " (" + city + ")"', name="Alice S.", city="Paris") == "Alice S. (Paris)"
    assert run("x > 1 ? 1 : 0", x=2) == 1


def test_a_parameter_bound_to_null_is_null() -> None:
    assert run('coalesce(nickname, "none")', nickname=None) == "none"
    assert run("x == null", x=None) is True


def test_a_map_parameter_is_read_with_dots_and_brackets() -> None:
    assert run('p.city + p["zip"]', p=FrozenStruct({"city": "Paris", "zip": "75"})) == "Paris75"


def test_the_logical_operators_and_the_conditional_are_lazy() -> None:
    assert run("false && 1 / 0 == 1") is False
    assert run("true || 1 / 0 == 1") is True
    assert run("true ? 1 : 1 / 0") == 1


@pytest.mark.parametrize(
    "text",
    [
        "1 / 0", "5 % 0", "1d / 0d", "9223372036854775807 + 1", "-9223372036854775807 - 2", "9223372036854775807 * 2",
        '1 < "a"', "[1][5]", "[1][-1]", '{"a": 1}.b', '{"a": 1}["b"]', "!1", "1 && true", "true && 1", "-true",
        '"a" - "b"', '"a" * 2', "1 + null", "[1] + 1", 'x.__class__', "1 ? 2 : 3", "1 in 2", "upper(1)", "size(1)",
    ],
)  # fmt: skip
def test_a_runtime_error_is_a_fault_that_never_repeats_a_value(text: str) -> None:
    with pytest.raises(ExprFault) as caught:
        compile_expression(text, ["x"]).run(["SECRETVALUE"])
    assert "SECRETVALUE" not in str(caught.value) and str(caught.value)


@pytest.mark.parametrize(
    "text",
    [
        "1 +", "(1", "1)", '"unterminated', "a b", "1.5", "1e3", "x y", "", "   ", "[1,", "{1: 2}", "{ 'a': 1 }",
        "zzz", "open(\"/etc/passwd\")", "__import__(\"os\")", "eval(\"1\")", "now()", "random()", "x.open()",
        "1 ? : 2", "@", "a = 1", "x[ ]", "upper()", '"\\q"',
    ],
)  # fmt: skip
def test_a_malformed_expression_or_an_unknown_name_is_refused_when_compiled(text: str) -> None:
    with pytest.raises(ExprError):
        compile_expression(text, ["x"])


def test_a_parameter_may_not_shadow_a_function_or_a_literal() -> None:
    for name in ("upper", "size", "has", "true", "null", "in"):
        with pytest.raises(ExprError):
            compile_expression("1", [name])
    with pytest.raises(ExprError):
        compile_expression("1", ["a", "a"])
    with pytest.raises(ExprError):
        compile_expression("1", ["not valid"])


def test_negating_the_lowest_integer_overflows_instead_of_dropping_the_sign() -> None:
    assert run("-9223372036854775808") == -(2**63)
    for text in ("-(-9223372036854775808)", "- -9223372036854775808"):
        with pytest.raises(ExprFault, match="overflow"):
            run(text)


def test_a_long_chain_of_operators_is_refused_when_compiled_not_by_the_stack() -> None:
    from aduku.expr.limits import MAXIMUM

    for text in ("+".join(["a"] * 1200), "a" + ".size()" * 1000, "a" + "[0]" * 1000):
        with pytest.raises(ExprError, match="chains"):
            compile_expression(text, ["a"], MAXIMUM)
    longest = compile_expression("+".join(["a"] * 150), ["a"], MAXIMUM)  # a long sum is fine below the cap
    assert longest.run([2]) == 300


def test_a_decimal_beyond_the_range_is_a_fault_not_a_crash() -> None:
    with pytest.raises(ExprFault):
        run("a * a", a=Decimal("9e500000"))
