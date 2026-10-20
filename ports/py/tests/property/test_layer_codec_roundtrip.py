"""Feature 005: any layer survives encoding and the text is stable (research R-2)."""

from hypothesis import given
from hypothesis import strategies as st

from aduku.behaviors.merge import Recreated
from aduku.engine.layer_codec import decode_layer, encode_layer
from aduku.ion_codec import TOMBSTONE, FrozenStruct, to_canonical
from aduku.paths import parse_path

scalars = st.one_of(
    st.none(),
    st.booleans(),
    st.integers(-(10**6), 10**6),
    st.decimals(min_value=-1000, max_value=1000, places=2, allow_nan=False),
    st.text(max_size=8),
)
values = st.one_of(scalars, st.lists(scalars, max_size=3).map(tuple))
states = st.one_of(values, st.just(TOMBSTONE), values.map(Recreated), st.just(FrozenStruct()))
names = st.sampled_from(["/a", "/b", "/a/x", "/a/y", "/tags", "/m[k]", "/n"])


@given(st.dictionaries(names.map(parse_path), states, max_size=7))
def test_encode_decode_encode_is_stable(layer: dict) -> None:
    text = encode_layer(layer)
    back = decode_layer(text)
    assert encode_layer(back) == text
    assert set(back) == set(layer)
    for path, state in layer.items():
        if isinstance(state, Recreated):
            assert isinstance(back[path], Recreated)
            assert to_canonical(back[path].value) == to_canonical(state.value)
        elif state is TOMBSTONE:
            assert back[path] is TOMBSTONE
        else:
            assert to_canonical(back[path]) == to_canonical(state)


@given(
    st.dictionaries(
        names.map(parse_path), st.decimals(min_value=-1000, max_value=1000, places=2, allow_nan=False), max_size=5
    )
)
def test_decimals_keep_their_exact_digits(layer: dict) -> None:
    back = decode_layer(encode_layer(layer))
    assert {p: str(v) for p, v in back.items()} == {p: str(v) for p, v in layer.items()}
