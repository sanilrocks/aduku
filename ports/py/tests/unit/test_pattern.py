"""Feature 004, research R-2: the `pattern` subset and its linear-time matcher (FR-004, SC-008)."""

import time

import pytest

from aduku.behaviors.pattern import PatternError, compile_pattern


def matches(pattern: str, value: str) -> bool:
    return compile_pattern(pattern).matches(value)


@pytest.mark.parametrize(
    "pattern, accepted, refused",
    [
        ("abc", ["abc"], ["", "ab", "abcd", "xabc", "ABC"]),
        (r"a\.b", ["a.b"], ["axb", "a..b"]),
        (r"\\\(\)\[\]\|\*\+\?\{\}", ["\\()[]|*+?{}"], ["x"]),
        ("a.c", ["abc", "a-c", "a c"], ["a\nc", "ac", "abbc"]),
        ("[abc]", ["a", "b", "c"], ["", "d", "ab"]),
        ("[a-c]+", ["a", "abcabc", "cba"], ["", "abd", "d"]),
        ("[^0-9]+", ["abc", "a-b"], ["", "a1", "7"]),
        (r"[a\]b]", ["a", "]", "b"], ["c"]),
        ("[A-Za-z0-9_]{1,3}", ["a", "A_9", "zzz"], ["", "zzzz", "a-"]),
        ("(ab|cd)e", ["abe", "cde"], ["ab", "ade", "abcde"]),
        ("a|b|cd", ["a", "b", "cd"], ["", "c", "abcd"]),
        ("a|", ["a", ""], ["b", "aa"]),
        ("", [""], ["a"]),
        ("a*", ["", "a", "aaaa"], ["b", "ab"]),
        ("a+", ["a", "aaa"], ["", "b"]),
        ("ab?c", ["ac", "abc"], ["abbc", "a"]),
        ("a{3}", ["aaa"], ["aa", "aaaa", ""]),
        ("a{2,}", ["aa", "aaaaaa"], ["a", ""]),
        ("a{2,4}", ["aa", "aaa", "aaaa"], ["a", "aaaaa"]),
        ("a{0}", [""], ["a"]),
        ("a{0,2}b", ["b", "ab", "aab"], ["aaab"]),
        ("(ab){2}", ["abab"], ["ab", "ababab", "aabb"]),
        ("((a|b)c)*d", ["d", "acd", "bcacd"], ["ac", "cd", "abd"]),
        ("[A-Z]{2}", ["US", "DE"], ["us", "USA", "U", "U1"]),
        ("é.", ["éa", "éé"], ["a", "éaa"]),
        ("(a|b)*abb", ["abb", "aabb", "babb", "abababb"], ["ab", "abba", "bb"]),
    ],
)
def test_the_subset_means_what_the_contract_says(pattern: str, accepted: list[str], refused: list[str]) -> None:
    for value in accepted:
        assert matches(pattern, value), (pattern, value)
    for value in refused:
        assert not matches(pattern, value), (pattern, value)


def test_a_pattern_matches_the_whole_value_not_a_part_of_it() -> None:
    assert not matches("b", "abc") and not matches("a", "abc") and matches("a.c", "abc")


@pytest.mark.parametrize(
    "pattern",
    [
        "(a", "a)", "[abc", "abc]", "[]", "[^]", "a{2,1}", "a{", "a{x}", "a{1,2,3}", "a{,2}", "a}", "*a", "+a", "?a", "a**",
        "a+*", "(*)", "[z-a]", "a\\", "(|", "()?{2}{3}", "a{256}", "a{1,256}", "a{0,300}", "x" * 501,
        "((a{255}){255}){255}", "(" * 150 + "a" + ")" * 150,
    ],
)  # fmt: skip
def test_a_pattern_outside_the_subset_or_over_a_limit_is_refused(pattern: str) -> None:
    with pytest.raises(PatternError):
        compile_pattern(pattern)


def test_the_largest_allowed_pattern_still_compiles() -> None:
    assert compile_pattern("a{255}").matches("a" * 255) and not compile_pattern("a{255}").matches("a" * 254)
    assert compile_pattern("x" * 500).matches("x" * 500)


def test_compiling_the_same_pattern_twice_gives_the_same_matcher() -> None:
    assert compile_pattern("[a-z]+") is compile_pattern("[a-z]+")


@pytest.mark.parametrize(
    "pattern", ["(a|aa)*b", "(a*)*b", "(a|a)*b", "(a+)+b", "(a?){30}a{30}", "(.*)*x", "((a|b)*)*c"]
)
def test_the_worst_shapes_finish_in_linear_time_on_a_long_value(pattern: str) -> None:
    compiled = compile_pattern(pattern)
    value = "a" * 10_000
    best = min(_time(compiled.matches, value) for _ in range(3))
    assert best < 0.1, (pattern, best)  # SC-008: under 100 ms
    assert compiled.matches(value + "b") is (pattern.endswith("b") and not pattern.startswith("(a?)"))


def _time(call, value: str) -> float:  # type: ignore[no-untyped-def]
    started = time.perf_counter()
    call(value)
    return time.perf_counter() - started


@pytest.mark.parametrize("pattern", ["a{²}", "a{٣}", "a{1,٣}", "a{١,}", "a{１}"])
def test_a_repeat_count_is_ascii_digits_only(pattern: str) -> None:
    """Other ports must read a count the same way: a non-ASCII digit is refused, not read as a number."""
    with pytest.raises(PatternError):
        compile_pattern(pattern)
