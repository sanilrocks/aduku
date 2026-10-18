"""Research R-3 and contracts/conformance-vectors.md, "Canonical Ion text (normative)"."""

import pytest

from aduku.ion_codec import TOMBSTONE, FrozenStruct, Symbol, parse, to_canonical


def canon(text: str) -> str:
    return to_canonical(parse(text))


def test_struct_fields_sorted_by_code_point() -> None:
    assert canon("{b:2, a:1}") == "{a:1,b:2}"
    assert canon("{b:3, a:2, B:1}") == "{B:1,a:2,b:3}"


def test_nested_structs_sorted_and_lists_keep_order() -> None:
    assert canon("{z:[3,1,2], a:{y:1, x:2}}") == "{a:{x:2,y:1},z:[3,1,2]}"


def test_compact_whitespace_independent() -> None:
    assert canon("{ a : 1 ,\n b : [ 1 , 2 ] }") == canon("{a:1,b:[1,2]}") == "{a:1,b:[1,2]}"


def test_insertion_order_does_not_change_bytes() -> None:
    one = to_canonical(FrozenStruct({"a": 1, "b": 2}))
    two = to_canonical(FrozenStruct({"b": 2, "a": 1}))
    assert one == two
    assert one.encode("utf-8") == two.encode("utf-8")


def test_annotations_come_first_in_order() -> None:
    assert canon("foo::{a:1}") == "foo::{a:1}"
    assert canon("a::b::1") == "a::b::1"
    assert canon('computed::"ALICE"') == 'computed::"ALICE"'


def test_tombstone() -> None:
    assert to_canonical(TOMBSTONE) == "deleted::null"
    assert canon("{phone:deleted::null}") == "{phone:deleted::null}"


def test_nulls_and_bools() -> None:
    assert canon("null") == "null"
    assert canon("null.string") == "null"
    assert canon("true") == "true"
    assert canon("false") == "false"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("5", "5"),
        ("-5", "-5"),
        ("0", "0"),
        ("1.50", "1.50"),
        ("0.0", "0.0"),
        ("-2.5", "-2.5"),
        ("100.", "100."),
        ("1d3", "1d3"),
        ("0.001", "0.001"),
        ("12d-1", "1.2"),
    ],
)
def test_numbers(text: str, expected: str) -> None:
    assert canon(text) == expected


def test_float_uses_exponent_form() -> None:
    assert to_canonical(1.5) == "1.5e0"
    assert canon("1e0") == "1e0"


def test_string_escapes() -> None:
    assert to_canonical('say "hi"') == '"say \\"hi\\""'
    assert to_canonical("back\\slash") == '"back\\\\slash"'
    assert to_canonical("line\nbreak\ttab") == '"line\\nbreak\\ttab"'
    assert to_canonical("\x01") == '"\\x01"'
    assert to_canonical("café") == '"café"'


def test_symbols_and_field_names_quote_only_when_needed() -> None:
    assert to_canonical(Symbol("backfill")) == "backfill"
    assert to_canonical(Symbol("has space")) == "'has space'"
    assert to_canonical(Symbol("null")) == "'null'"
    assert canon("{'quoted key':1}") == "{'quoted key':1}"
    assert canon("{'null':1}") == "{'null':1}"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("2020-01-01T00:00:00Z", "2020-01-01T00:00:00Z"),
        ("2020-01-01T10:20:30.5+01:00", "2020-01-01T09:20:30.5Z"),
        ("2020-01-01T10:20+00:00", "2020-01-01T10:20Z"),
        ("2020-02-03", "2020-02-03T"),
        ("2020-02T", "2020-02T"),
        ("2020T", "2020T"),
    ],
)
def test_timestamps_are_utc_with_written_precision(text: str, expected: str) -> None:
    assert canon(text) == expected


def test_blobs_are_outside_the_vector_set() -> None:
    with pytest.raises(TypeError):
        to_canonical(b"raw")


@pytest.mark.parametrize(
    ("number", "expected"),
    [(1e16, "1e16"), (1.5e-7, "1.5e-7"), (1e-5, "1e-5"), (-2.5e22, "-2.5e22"), (123.456, "123.456e0"), (0.5, "0.5e0")],
)
def test_float_exponents_have_no_plus_sign_or_leading_zeros(number: float, expected: str) -> None:
    assert to_canonical(number) == expected


def test_symbol_and_string_are_not_equal_in_repr() -> None:
    assert repr(Symbol("a")) != repr("a")
