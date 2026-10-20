"""Feature 004, FR-004: on the shared subset the matcher agrees with Python's `re` (whole-value match)."""

import re

from hypothesis import given
from hypothesis import strategies as st

from aduku.behaviors.pattern import compile_pattern

LITERAL = st.sampled_from(list("abc"))
ESCAPED = st.sampled_from([r"\.", r"\*", r"\(", r"\[", r"\|", r"\\"])
CLASS = st.sampled_from(["[ab]", "[a-c]", "[^a]", "[^ab]", "[a-bc]", r"[\]a]"])
ATOM = st.one_of(LITERAL, ESCAPED, st.just("."), CLASS)


@st.composite
def counted(draw) -> str:  # type: ignore[no-untyped-def]
    low = draw(st.integers(0, 3))
    kind = draw(st.sampled_from(["exact", "open", "range"]))
    if kind == "exact":
        return "{" + str(low) + "}"
    if kind == "open":
        return "{" + str(low) + ",}"
    return "{" + str(low) + "," + str(low + draw(st.integers(0, 3))) + "}"


def patterns() -> st.SearchStrategy[str]:
    def extend(inner: st.SearchStrategy[str]) -> st.SearchStrategy[str]:
        return st.one_of(
            st.lists(inner, min_size=1, max_size=3).map("".join),  # concatenation
            st.lists(inner, min_size=2, max_size=3).map("|".join),  # alternation
            st.tuples(inner, st.sampled_from(["*", "+", "?"])).map(lambda t: f"({t[0]}){t[1]}"),
            st.tuples(inner, counted()).map(lambda t: f"({t[0]}){t[1]}"),
        )

    return st.recursive(ATOM, extend, max_leaves=8)


VALUES = st.text(alphabet=list("abc.*([|\\]\n"), max_size=12)


@given(patterns(), VALUES)
def test_the_matcher_agrees_with_re_on_the_shared_subset(pattern: str, value: str) -> None:
    expected = re.fullmatch(pattern, value) is not None
    assert compile_pattern(pattern).matches(value) is expected, (pattern, value)


@given(patterns(), st.text(alphabet=list("abc"), max_size=40))
def test_a_compiled_pattern_gives_the_same_answer_every_time(pattern: str, value: str) -> None:
    compiled = compile_pattern(pattern)
    assert compiled.matches(value) == compiled.matches(value) == compile_pattern(pattern).matches(value)
