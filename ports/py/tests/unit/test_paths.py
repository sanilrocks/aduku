"""SPEC 3.2: paths, normalization, pattern matching."""

import pytest

from aduku.paths import PathError, Segment, format_path, matches, most_specific, parse_path, parse_pattern


def test_root_and_simple_paths() -> None:
    assert parse_path("/") == ()
    assert parse_path("/name") == (Segment("name"),)
    assert parse_path("/address/city") == (Segment("address"), Segment("city"))


def test_keyed_segment_is_parsed_not_evaluated() -> None:
    assert parse_path('/items[sku="A1"]/qty') == (Segment("items", 'sku="A1"'), Segment("qty"))


@pytest.mark.parametrize("text", ["/", "/name", "/address/city", '/items[sku="A1"]/qty', "/a-b/c_d/e1"])
def test_format_round_trip(text: str) -> None:
    assert format_path(parse_path(text)) == text


@pytest.mark.parametrize(
    "text",
    ["", "name", "//a", "/a//b", "/a/", "/./a", "/a/..", "/a b", "/a/*", "/a/**", "/a[", "/a[]", "/a/b c"],
)
def test_invalid_paths_are_rejected(text: str) -> None:
    with pytest.raises(PathError):
        parse_path(text)


def test_single_star_matches_exactly_one_segment() -> None:
    pattern = parse_pattern("/*")
    assert matches(pattern, parse_path("/a"))
    assert not matches(pattern, parse_path("/a/b"))
    assert not matches(pattern, parse_path("/"))


def test_double_star_matches_one_or_more_segments() -> None:
    pattern = parse_pattern("/**")
    assert matches(pattern, parse_path("/a"))
    assert matches(pattern, parse_path("/a/b/c"))
    assert not matches(pattern, parse_path("/"))


def test_prefix_with_wildcards() -> None:
    assert matches(parse_pattern("/address/**"), parse_path("/address/city"))
    assert matches(parse_pattern("/address/**"), parse_path("/address/geo/lat"))
    assert not matches(parse_pattern("/address/**"), parse_path("/address"))
    assert matches(parse_pattern("/address/*"), parse_path("/address/zip"))
    assert not matches(parse_pattern("/address/*"), parse_path("/address/geo/lat"))


def test_exact_pattern() -> None:
    assert matches(parse_pattern("/a/b"), parse_path("/a/b"))
    assert not matches(parse_pattern("/a/b"), parse_path("/a/c"))


@pytest.mark.parametrize(
    ("path", "winner"),
    [
        ("/address/city", "/address/city"),
        ("/address/zip", "/address/*"),
        ("/name", "/*"),
        ("/x/y/z", "/**"),
        ("/address", "/address"),
    ],
)
def test_most_specific_pattern_wins(path: str, winner: str) -> None:
    patterns = [parse_pattern(p) for p in ["/**", "/*", "/address", "/address/city", "/address/*"]]
    chosen = most_specific(patterns, parse_path(path))
    assert chosen is not None and format_path(chosen) == winner


def test_no_matching_pattern() -> None:
    assert most_specific([parse_pattern("/a")], parse_path("/b")) is None


def test_keyed_pattern_is_more_specific_than_plain() -> None:
    patterns = [parse_pattern("/items"), parse_pattern('/items[sku="A1"]')]
    keyed = most_specific(patterns, parse_path('/items[sku="A1"]'))
    assert keyed is not None and format_path(keyed) == '/items[sku="A1"]'
    other = most_specific(patterns, parse_path('/items[sku="B2"]'))
    assert other is not None and format_path(other) == "/items"
