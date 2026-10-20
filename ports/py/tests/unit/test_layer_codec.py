"""Feature 005 (research R-2): a layer's canonical text round-trips and is byte-stable."""

from decimal import Decimal

import pytest

from aduku.behaviors.merge import Recreated
from aduku.engine.layer_codec import decode_layer, encode_layer
from aduku.ion_codec import ABSENT, TOMBSTONE, Annotated, FrozenStruct, IonError, Timestamp, parse
from aduku.paths import parse_path


def layer(**by_path: object) -> dict:
    return {parse_path(path): state for path, state in by_path.items()}


def same(a: object, b: object) -> bool:
    from aduku.ion_codec import to_canonical

    return to_canonical(a) == to_canonical(b)


def test_plain_values_tombstones_resets_and_containers_round_trip() -> None:
    original = {
        parse_path("/name"): "Ann",
        parse_path("/age"): 7,
        parse_path("/price"): Decimal("1.50"),
        parse_path("/tags"): ("a", "b"),
        parse_path("/gone"): TOMBSTONE,
        parse_path("/back"): Recreated("x"),
        parse_path("/addr"): FrozenStruct(),
        parse_path("/nothing"): None,
        parse_path("/never"): ABSENT,
        parse_path("/seen"): Timestamp(2026, 1, 2, 3, 4, 5),
    }
    back = decode_layer(encode_layer(original))
    assert set(back) == set(original)
    for path, state in original.items():
        if isinstance(state, Recreated):
            assert isinstance(back[path], Recreated) and same(back[path].value, state.value)
        elif state is TOMBSTONE or state is ABSENT:
            assert back[path] is state
        else:
            assert same(back[path], state)


def test_the_text_is_sorted_by_path_and_does_not_depend_on_insertion_order() -> None:
    a = {parse_path("/b"): 1, parse_path("/a"): 2, parse_path("/a/c"): 3}
    b = dict(reversed(list(a.items())))
    assert encode_layer(a) == encode_layer(b)
    paths = [row["p"] for row in parse(encode_layer(a))]
    assert paths == sorted(paths)


def test_an_annotated_value_is_not_read_as_a_reset_mark() -> None:
    original = {parse_path("/x"): Annotated(("recreated",), "v")}
    back = decode_layer(encode_layer(original))
    assert isinstance(back[parse_path("/x")], Annotated)


@pytest.mark.parametrize("text", ["1", "[1]", "[{s: 1}]", '[{p: "no slash"}]'])
def test_text_that_is_not_a_layer_is_refused(text: str) -> None:
    with pytest.raises(IonError):
        decode_layer(text)


def test_an_empty_layer() -> None:
    assert decode_layer(encode_layer({})) == {}
