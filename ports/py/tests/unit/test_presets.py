"""Feature 002, research R-4: the preset macros. Pure functions with a signature and an `accepts_missing` flag."""

import hashlib
from decimal import Decimal

import pytest

from aduku.behaviors.compute import PRESETS, PresetError
from aduku.behaviors.registry import PRESET_NAMES
from aduku.ion_codec import FrozenStruct, parse


def call(name: str, *args: object) -> object:
    return PRESETS[name].call(list(args))


def test_every_reserved_name_is_a_preset_and_the_reverse() -> None:
    assert set(PRESETS) == set(PRESET_NAMES)


def test_only_four_presets_accept_missing_inputs() -> None:
    assert {n for n, p in PRESETS.items() if p.accepts_missing} == {"coalesce", "if", "eq", "join"}


@pytest.mark.parametrize(
    "name, fewest, most",
    [
        ("upper", 1, 1), ("lower", 1, 1), ("trim", 1, 1), ("len", 1, 1), ("hash", 1, 1),
        ("round", 2, 2), ("format_ts", 2, 2), ("join", 2, 2), ("eq", 2, 2), ("if", 3, 3),
        ("concat", 1, None), ("coalesce", 1, None), ("sum", 1, None), ("min", 1, None), ("max", 1, None),
    ],
)  # fmt: skip
def test_arity(name: str, fewest: int, most: int | None) -> None:
    preset = PRESETS[name]
    assert (preset.fewest_args, preset.most_args) == (fewest, most)


def test_fixed_result_types() -> None:
    fixed = {n: p.result_type for n, p in PRESETS.items()}
    for name in ("upper", "lower", "trim", "concat", "format_ts", "hash", "join"):
        assert fixed[name] == "string"
    assert fixed["len"] == "int" and fixed["eq"] == "bool"
    for name in ("coalesce", "sum", "min", "max", "round", "if"):
        assert fixed[name] is None


@pytest.mark.parametrize(
    "name, args, expected",
    [
        ("upper", ("Alice S.",), "ALICE S."),
        ("lower", ("Alice S.",), "alice s."),
        ("trim", ("  a b \n",), "a b"),
        ("concat", ("a", "b"), "ab"),
        ("concat", ("a", 1, Decimal("2.50"), True), "a12.50true"),
        ("coalesce", (None, "x"), "x"),
        ("coalesce", ("a", "b"), "a"),
        ("coalesce", (None, None), None),
        ("len", ("héllo",), 5),
        ("len", (("a", "b", "c"),), 3),
        ("len", (FrozenStruct({"a": 1}),), 1),
        ("sum", (1, 2), 3),
        ("sum", (Decimal("1.1"), Decimal("2.2")), Decimal("3.3")),
        ("sum", (1, 2.5), 3.5),
        ("sum", ((1, 2), 3), 6),
        ("sum", ((),), 0),
        ("min", (3, 1, 2), 1),
        ("max", (3, 1, 2), 3),
        ("min", ((3, 1, 2),), 1),
        ("max", ("a", "b"), "b"),
        ("max", (1, Decimal("2.5")), Decimal("2.5")),
        ("round", (Decimal("2.5"), 0), Decimal("2")),  # half to even
        ("round", (Decimal("3.5"), 0), Decimal("4")),
        ("round", (Decimal("1.234"), 2), Decimal("1.23")),
        ("round", (7, 2), 7),
        ("round", (2.675, 2), round(2.675, 2)),
        ("join", (("a", "b"), ", "), "a, b"),
        ("join", ((1, 2), "-"), "1-2"),
        ("join", (None, ", "), ""),
        ("if", (True, "a", "b"), "a"),
        ("if", (False, "a", "b"), "b"),
        ("if", (None, "a", "b"), "b"),
        ("eq", (1, 1), True),
        ("eq", (1, Decimal("1.0")), True),
        ("eq", (1, "1"), False),
        ("eq", (True, 1), False),
        ("eq", (None, None), True),
        ("eq", (("a",), ("a",)), True),
        ("eq", (FrozenStruct({"a": 1}), FrozenStruct({"a": 1})), True),
    ],
)  # fmt: skip
def test_results(name: str, args: tuple[object, ...], expected: object) -> None:
    got = call(name, *args)
    assert got == expected and type(got) is type(expected)


def test_hash_is_the_sha256_of_the_canonical_text() -> None:
    assert call("hash", "abc") == hashlib.sha256(b'"abc"').hexdigest()
    assert call("hash", FrozenStruct({"b": 1, "a": 2})) == hashlib.sha256(b"{a:2,b:1}").hexdigest()


def test_format_ts_formats_the_utc_instant_with_a_closed_set_of_tokens() -> None:
    stamp = parse("2026-01-02T03:04:05Z")
    assert call("format_ts", stamp, "%Y-%m-%d") == "2026-01-02"
    assert call("format_ts", stamp, "%H:%M:%S %%") == "03:04:05 %"
    shifted = parse("2026-01-02T03:04:05+02:00")  # 01:04:05 UTC
    assert call("format_ts", shifted, "%H") == "01"


@pytest.mark.parametrize(
    "name, args",
    [
        ("upper", (1,)), ("lower", (None,)), ("trim", (("a",),)),
        ("concat", (("a",),)), ("concat", ("a", None)),
        ("len", (5,)), ("sum", ("a",)), ("sum", (True,)), ("sum", (Decimal("1"), 1.5)),
        ("min", (1, "a")), ("max", ((),)), ("min", (True, 2)),
        ("round", ("x", 2)), ("round", (1.5, "x")), ("round", (1.5, 2.5)),
        ("format_ts", ("not a timestamp", "%Y")), ("format_ts", (parse("2026-01-02T03:04:05Z"), "%A")),
        ("join", (("a", ("b",)), ",")), ("join", (("a",), 1)),
        ("if", ("yes", 1, 2)),
    ],
)  # fmt: skip
def test_a_wrong_type_is_an_error_never_a_coerced_value(name: str, args: tuple[object, ...]) -> None:
    with pytest.raises(PresetError):
        call(name, *args)


def test_the_wrong_number_of_arguments_is_an_error() -> None:
    with pytest.raises(PresetError):
        call("upper", "a", "b")
    with pytest.raises(PresetError):
        call("concat")
    with pytest.raises(PresetError):
        call("if", True, 1)


def test_round_that_cannot_give_the_digits_is_a_preset_error() -> None:
    with pytest.raises(PresetError):
        call("round", Decimal("1" + "0" * 40 + ".5"), 2)  # more digits than a decimal here holds
    assert call("round", Decimal("2.675"), 2) == Decimal("2.68")


def test_sum_keeps_all_38_digits_whatever_the_ambient_decimal_context() -> None:
    import decimal

    thirty = Decimal("1" * 30)
    with decimal.localcontext() as ambient:
        ambient.prec = 5
        assert call("sum", thirty, Decimal(1)) == Decimal("1" * 29 + "2")
    with pytest.raises(PresetError):
        call("sum", Decimal("9e999999"), Decimal("9e999999"))
