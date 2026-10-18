"""Research R-2: amazon.ion keeps `deleted::null` and decimals exact; the codec maps them to internal values."""

from decimal import Decimal

import amazon.ion.simpleion as ion
import pytest

from aduku.ion_codec import TOMBSTONE, Annotated, FrozenStruct, Symbol, parse, to_canonical


def test_library_keeps_tombstone_annotation() -> None:
    value = ion.loads("deleted::null")
    assert [token.text for token in value.ion_annotations] == ["deleted"]


def test_library_keeps_decimal_digits() -> None:
    assert ion.loads("1.50").as_tuple().digits == (1, 5, 0)


def test_tombstone_is_distinct_from_null_and_absent() -> None:
    assert parse("deleted::null") is TOMBSTONE
    assert parse("null") is None
    assert TOMBSTONE is not None
    struct = parse("{a:null, b:deleted::null}")
    assert isinstance(struct, FrozenStruct)
    assert "a" in struct and struct["a"] is None
    assert struct["b"] is TOMBSTONE
    assert "c" not in struct


def test_other_annotations_are_kept() -> None:
    assert parse('computed::"X"') == Annotated(("computed",), "X")
    assert parse("deleted::1") == Annotated(("deleted",), 1)
    assert parse("a::b::1") == Annotated(("a", "b"), 1)


def test_decimals_are_exact() -> None:
    value = parse("1.50")
    assert isinstance(value, Decimal)
    assert value.as_tuple().digits == (1, 5, 0)
    assert to_canonical(value) == "1.50"


def test_ints_are_not_decimals_or_bools() -> None:
    assert parse("5") == 5 and type(parse("5")) is int
    assert parse("true") is True


def test_symbols_and_strings_differ() -> None:
    struct = parse('{s:"text", y:sym}')
    assert type(struct["s"]) is str
    assert isinstance(struct["y"], Symbol)


@pytest.mark.parametrize(
    "text",
    [
        "null",
        "true",
        "-5",
        "1.50",
        "1d3",
        '"hello"',
        "sym",
        "[1,2,[3]]",
        '{a:1,b:"x",c:[1,2],d:{e:null}}',
        "deleted::null",
        'computed::"X"',
        "{phone:deleted::null}",
        "2020-01-01T00:00:00Z",
        "2020-01-01T10:20:30.5+01:00",
        "2020T",
    ],
)
def test_round_trip_is_stable(text: str) -> None:
    value = parse(text)
    canonical = to_canonical(value)
    assert parse(canonical) == value
    assert to_canonical(parse(canonical)) == canonical
