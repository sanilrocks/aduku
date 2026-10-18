"""Paths and schema field patterns (SPEC 3.2).

path    = "/" [ segment *( "/" segment ) ]
segment = field-name [ "[" key-expr "]" ]
pattern segments may also be "*" (exactly one segment) or "**" (one or more segments)
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass

__all__ = [
    "Path",
    "PathError",
    "Segment",
    "format_path",
    "is_field_name",
    "matches",
    "most_specific",
    "parse_path",
    "parse_pattern",
]

_FIELD = re.compile(r"[A-Za-z0-9_\-]+\Z")


def is_field_name(text: str) -> bool:
    """Whether `text` can be one path segment (and so a key of a struct that is spread over child paths)."""
    return _FIELD.match(text) is not None


class PathError(ValueError):
    """A path or pattern is not valid or not normalized."""


@dataclass(frozen=True)
class Segment:
    name: str
    key: str | None = None


Path = tuple[Segment, ...]


def parse_path(text: str) -> Path:
    """Parse a concrete path. Rejects empty, `.` and `..` segments, trailing slashes and wildcards."""
    return _parse(text, allow_wildcards=False)


def parse_pattern(text: str) -> Path:
    """Parse a schema field pattern; `*` and `**` are allowed as whole segments."""
    return _parse(text, allow_wildcards=True)


def _parse(text: str, *, allow_wildcards: bool) -> Path:
    if not text.startswith("/"):
        raise PathError(f"path must start with '/': {text!r}")
    if text == "/":
        return ()
    segments: list[Segment] = []
    pos = 1
    length = len(text)
    while True:
        start = pos
        while pos < length and text[pos] not in "/[":
            pos += 1
        name = text[start:pos]
        if allow_wildcards and name in ("*", "**"):
            pass
        elif not _FIELD.match(name):
            raise PathError(f"invalid segment {name!r} in {text!r}")
        key: str | None = None
        if pos < length and text[pos] == "[":
            if name in ("*", "**"):
                raise PathError(f"wildcard segments cannot carry a key: {text!r}")
            pos += 1
            key_start = pos
            quote: str | None = None
            while pos < length and (quote or text[pos] != "]"):
                if text[pos] in "\"'":
                    quote = None if quote == text[pos] else (quote or text[pos])
                pos += 1
            if pos >= length:
                raise PathError(f"unclosed key in {text!r}")
            key = text[key_start:pos]
            if not key:
                raise PathError(f"empty key in {text!r}")
            pos += 1
        segments.append(Segment(name, key))
        if pos >= length:
            return tuple(segments)
        if text[pos] != "/":
            raise PathError(f"unexpected {text[pos]!r} in {text!r}")
        pos += 1
        if pos >= length:
            raise PathError(f"trailing slash in {text!r}")


def format_path(path: Sequence[Segment]) -> str:
    if not path:
        return "/"
    return "".join("/" + seg.name + (f"[{seg.key}]" if seg.key is not None else "") for seg in path)


def matches(pattern: Sequence[Segment], path: Sequence[Segment]) -> bool:
    """True if `pattern` covers `path`. The root path is matched only by the root pattern."""
    if not pattern or not path:
        return not pattern and not path
    head, rest = pattern[0], pattern[1:]
    if head.name == "**":
        # one or more segments
        return any(matches(rest, path[take:]) for take in range(1, len(path) + 1)) if rest else True
    if head.name == "*":
        return matches(rest, path[1:])
    first = path[0]
    if head.name != first.name or (head.key is not None and head.key != first.key):
        return False
    return matches(rest, path[1:])


def most_specific(patterns: Sequence[Path], path: Path) -> Path | None:
    """The best matching pattern: fewest `**`, then most literal segments, then fewest `*`."""
    best: Path | None = None
    best_rank: tuple[int, int, int] | None = None
    for pattern in patterns:
        if not matches(pattern, path):
            continue
        deep = sum(1 for seg in pattern if seg.name == "**")
        one = sum(1 for seg in pattern if seg.name == "*")
        # a literal segment with a key predicate is more specific than a plain literal segment
        literal = sum(2 if seg.key is not None else 1 for seg in pattern if seg.name not in ("*", "**"))
        rank = (deep, -literal, one)
        if best_rank is None or rank < best_rank:
            best, best_rank = pattern, rank
    return best
