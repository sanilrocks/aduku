"""The `pattern` rule's language and its matcher (feature 004, research R-2). Pure.

A small subset of regular expressions that every port can implement the same way: literals (a backslash makes any one
character literal), `.` (any character but a newline), classes `[abc]`, `[a-z]`, `[^...]`, groups, alternation and the
repeaters `*` `+` `?` `{m}` `{m,}` `{m,n}`. There are no anchors (a pattern always matches the whole value), no
back-references, no look-around, no shortcuts and no flags.

Matching never backtracks: the pattern is compiled to a state machine and the set of states the value can be in is carried
from character to character, so the time is proportional to (length of the value) x (number of states). Limits keep the
compile bounded too: a pattern of at most 500 characters, repeat counts of at most 255, at most 5,000 states.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache

__all__ = ["MAX_PATTERN_LENGTH", "MAX_REPEAT", "MAX_STATES", "Pattern", "PatternError", "compile_pattern"]

MAX_PATTERN_LENGTH = 500
MAX_REPEAT = 255
MAX_STATES = 5_000
_MAX_DEPTH = 50  # groups nested in groups
_SPECIAL = set("\\.[]()|*+?{}")


class PatternError(ValueError):
    """A pattern is outside the subset or over a limit; the message says what, and never repeats a value."""


# -- syntax tree ---------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class _Char:
    char: str


@dataclass(frozen=True)
class _Any:
    pass


@dataclass(frozen=True)
class _Class:
    negated: bool
    ranges: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class _Cat:
    parts: tuple[object, ...]


@dataclass(frozen=True)
class _Alt:
    branches: tuple[object, ...]


@dataclass(frozen=True)
class _Rep:
    body: object
    low: int
    high: int | None  # None: no upper bound


def _digits(text: str) -> bool:
    """Plain ASCII digits only: `str.isdigit` also accepts superscripts and other scripts, which other ports would not read."""
    return text.isascii() and text.isdigit()


class _Parser:
    def __init__(self, text: str) -> None:
        self.text = text
        self.at = 0

    def parse(self) -> object:
        node = self._alternation(0)
        if self.at != len(self.text):
            raise PatternError("a closing parenthesis has no opening one")
        return node

    def _alternation(self, depth: int) -> object:
        if depth > _MAX_DEPTH:
            raise PatternError("groups are nested too deeply")
        branches = [self._branch(depth)]
        while self._peek() == "|":
            self.at += 1
            branches.append(self._branch(depth))
        return branches[0] if len(branches) == 1 else _Alt(tuple(branches))

    def _branch(self, depth: int) -> object:
        parts: list[object] = []
        while self.at < len(self.text) and self.text[self.at] not in "|)":
            parts.append(self._piece(depth))
        return _Cat(tuple(parts))

    def _piece(self, depth: int) -> object:
        atom = self._atom(depth)
        mark = self._peek()
        if mark == "*":
            self.at += 1
            node: object = _Rep(atom, 0, None)
        elif mark == "+":
            self.at += 1
            node = _Rep(atom, 1, None)
        elif mark == "?":
            self.at += 1
            node = _Rep(atom, 0, 1)
        elif mark == "{":
            node = self._counted(atom)
        else:
            return atom
        if self._peek() in ("*", "+", "?", "{"):
            raise PatternError("a repeater follows another repeater")
        return node

    def _counted(self, atom: object) -> object:
        close = self.text.find("}", self.at)
        if close < 0:
            raise PatternError("a repeat count is not closed")
        body = self.text[self.at + 1 : close]
        self.at = close + 1
        low_text, comma, high_text = body.partition(",")
        if not _digits(low_text) or (comma and high_text and not _digits(high_text)) or "," in high_text:
            raise PatternError("a repeat count is {m}, {m,} or {m,n} with whole numbers")
        low = int(low_text)
        high: int | None = (int(high_text) if high_text else None) if comma else low
        if low > MAX_REPEAT or (high is not None and high > MAX_REPEAT):
            raise PatternError(f"a repeat count is at most {MAX_REPEAT}")
        if high is not None and low > high:
            raise PatternError("a repeat count runs from a smaller to a larger number")
        return _Rep(atom, low, high)

    def _atom(self, depth: int) -> object:
        char = self.text[self.at]
        if char == "(":
            self.at += 1
            inner = self._alternation(depth + 1)
            if self._peek() != ")":
                raise PatternError("a parenthesis is not closed")
            self.at += 1
            return inner
        if char == "[":
            return self._class()
        if char == ".":
            self.at += 1
            return _Any()
        if char == "\\":
            return _Char(self._escaped())
        if char in _SPECIAL:
            raise PatternError(f"{char!r} must be escaped or has nothing to apply to")
        self.at += 1
        return _Char(char)

    def _escaped(self) -> str:
        if self.at + 1 >= len(self.text):
            raise PatternError("a backslash ends the pattern")
        char = self.text[self.at + 1]
        self.at += 2
        return char

    def _class(self) -> object:
        self.at += 1  # [
        negated = self._peek() == "^"
        if negated:
            self.at += 1
        ranges: list[tuple[str, str]] = []
        while True:
            if self.at >= len(self.text):
                raise PatternError("a class is not closed")
            char = self.text[self.at]
            if char == "]":
                self.at += 1
                break
            low = self._escaped() if char == "\\" else self._plain(char)
            if self._peek() == "-" and self.text[self.at + 1 : self.at + 2] not in ("", "]"):
                self.at += 1
                char = self.text[self.at]
                high = self._escaped() if char == "\\" else self._plain(char)
                if low > high:
                    raise PatternError("a class range runs from a smaller to a larger character")
                ranges.append((low, high))
            else:
                ranges.append((low, low))
        if not ranges:
            raise PatternError("a class is empty")
        return _Class(negated, tuple(ranges))

    def _plain(self, char: str) -> str:
        if char in "[]":
            raise PatternError("a bracket inside a class must be escaped")
        self.at += 1
        return char

    def _peek(self) -> str:
        return self.text[self.at] if self.at < len(self.text) else ""


# -- the state machine ---------------------------------------------------------------------------------------------

_CHAR, _SPLIT, _MATCH = 0, 1, 2


class _Builder:
    """States are built backwards: `emit(node, next)` returns the state that matches `node` and then goes to `next`."""

    def __init__(self) -> None:
        self.kind: list[int] = []
        self.test: list[object] = []  # for a character state, a predicate on one character
        self.first: list[int] = []  # the next state, or the first branch of a split
        self.second: list[int] = []  # the second branch of a split

    def _state(self, kind: int, test: object, first: int, second: int) -> int:
        if len(self.kind) >= MAX_STATES:
            raise PatternError(f"the pattern needs more than {MAX_STATES} states")
        self.kind.append(kind)
        self.test.append(test)
        self.first.append(first)
        self.second.append(second)
        return len(self.kind) - 1

    def split(self, a: int, b: int) -> int:
        return self._state(_SPLIT, None, a, b)

    def emit(self, node: object, follow: int) -> int:
        if isinstance(node, _Char):
            char = node.char
            return self._state(_CHAR, lambda c: c == char, follow, -1)
        if isinstance(node, _Any):
            return self._state(_CHAR, lambda c: c != "\n", follow, -1)
        if isinstance(node, _Class):
            ranges, negated = node.ranges, node.negated
            return self._state(_CHAR, lambda c: negated != any(low <= c <= high for low, high in ranges), follow, -1)
        if isinstance(node, _Cat):
            for part in reversed(node.parts):
                follow = self.emit(part, follow)
            return follow
        if isinstance(node, _Alt):
            entries = [self.emit(branch, follow) for branch in node.branches]
            entry = entries[-1]
            for other in reversed(entries[:-1]):
                entry = self.split(other, entry)
            return entry
        assert isinstance(node, _Rep)
        return self._repeat(node, follow)

    def _repeat(self, node: _Rep, follow: int) -> int:
        tail = follow
        if node.high is None:  # body{low,} is body repeated low times and then body*
            loop = self.split(-1, follow)
            self.first[loop] = self.emit(node.body, loop)
            tail = loop
        else:  # body{low,high}: low copies, then high-low copies that may each be skipped
            for _ in range(node.high - node.low):
                entry = self.emit(node.body, tail)
                tail = self.split(entry, follow)
        for _ in range(node.low):
            tail = self.emit(node.body, tail)
        return tail


class Pattern:
    """A compiled pattern. Matching carries the set of states the value can be in; it never backtracks."""

    def __init__(self, builder: _Builder, start: int, match: int) -> None:
        self._kind = builder.kind
        self._test = builder.test
        self._first = builder.first
        self._second = builder.second
        self._start = start
        self._match = match

    def matches(self, value: str) -> bool:
        """True if the whole of `value` matches."""
        current = self._closure([self._start])
        for char in value:
            moved = [
                self._first[state]
                for state in current
                if self._kind[state] == _CHAR and self._test[state](char)  # type: ignore[operator]
            ]
            if not moved:
                return False
            current = self._closure(moved)
        return self._match in current

    def _closure(self, states: list[int]) -> set[int]:
        """The states reached from `states` without reading a character."""
        seen: set[int] = set()
        stack = list(states)
        while stack:
            state = stack.pop()
            if state in seen:
                continue
            seen.add(state)
            if self._kind[state] == _SPLIT:
                stack.append(self._first[state])
                stack.append(self._second[state])
        return seen


@lru_cache(maxsize=512)
def compile_pattern(text: str) -> Pattern:
    """Compile a pattern of the subset. Raises PatternError for anything outside it or over a limit."""
    if len(text) > MAX_PATTERN_LENGTH:
        raise PatternError(f"a pattern is at most {MAX_PATTERN_LENGTH} characters")
    tree = _Parser(text).parse()
    builder = _Builder()
    match = builder._state(_MATCH, None, -1, -1)
    start = builder.emit(tree, match)
    return Pattern(builder, start, match)
