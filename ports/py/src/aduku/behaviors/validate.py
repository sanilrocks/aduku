"""Validation rules (SPEC 10): pure behaviors that look at a document and report violations.

Every validator takes one `Subject` and returns a list of `Violation`. A field rule sees the value at one path; a document
rule sees the whole document. Rules describe the constraint in their messages and never repeat the value.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from aduku.behaviors.pattern import compile_pattern
from aduku.behaviors.registry import ArgSpec, BehaviorRegistry
from aduku.ion_codec import FrozenStruct, Timestamp, timestamp_sort_key, to_canonical
from aduku.model import Violation
from aduku.paths import parse_path

__all__ = [
    "Subject",
    "check_all_or_none",
    "check_at_least_one",
    "check_at_most_one",
    "check_max_len",
    "check_min_len",
    "check_non_empty",
    "check_one_of",
    "check_ordered",
    "check_pattern",
    "check_range",
    "check_unique_items",
    "register_validators",
]


@dataclass(frozen=True)
class Subject:
    """What a validator is asked about. `path` and `value` are None for a document rule."""

    document: FrozenStruct
    path: str | None
    value: Any
    args: FrozenStruct


def _violation(rule: str, subject: Subject, message: str) -> list[Violation]:
    return [Violation(rule, subject.path or "/", message)]


def check_non_empty(subject: Subject) -> list[Violation]:
    value = subject.value
    if isinstance(value, str | tuple | FrozenStruct) and len(value) == 0:
        return _violation("non_empty", subject, "must not be empty")
    return []


def check_range(subject: Subject) -> list[Violation]:
    value = subject.value
    if isinstance(value, bool) or not isinstance(value, int | float | Decimal):
        return []  # only numbers have a range
    low, high = subject.args.get("min"), subject.args.get("max")
    if low is not None and value < low:
        return _violation("range", subject, f"must be at least {low}")
    if high is not None and value > high:
        return _violation("range", subject, f"must be at most {high}")
    return []


def check_max_len(subject: Subject) -> list[Violation]:
    value, limit = subject.value, subject.args.get("n")
    if isinstance(value, str | tuple) and limit is not None and len(value) > limit:
        return _violation("max_len", subject, f"must have at most {limit} items or characters")
    return []


def check_min_len(subject: Subject) -> list[Violation]:
    value, limit = subject.value, subject.args.get("n")
    if isinstance(value, str | tuple) and limit is not None and len(value) < limit:
        return _violation("min_len", subject, f"must have at least {limit} items or characters")
    return []


def check_pattern(subject: Subject) -> list[Violation]:
    value, regex = subject.value, subject.args.get("regex")
    if isinstance(value, str) and isinstance(regex, str) and not compile_pattern(regex).matches(value):
        return _violation("pattern", subject, "must match the pattern")
    return []


def check_one_of(subject: Subject) -> list[Violation]:
    allowed = subject.args.get("values")
    if isinstance(allowed, tuple) and to_canonical(subject.value) not in {to_canonical(item) for item in allowed}:
        return _violation("one_of", subject, "must be one of the allowed values")
    return []


def check_unique_items(subject: Subject) -> list[Violation]:
    value = subject.value
    if isinstance(value, tuple):
        seen: set[str] = set()
        for item in value:
            text = to_canonical(item)
            if text in seen:
                return _violation("unique_items", subject, "must not repeat an item")
            seen.add(text)
    return []


def _value_at(document: FrozenStruct, path_text: str) -> Any:
    """The value at a path of the merged document, or None when there is none (absent and null are the same here)."""
    node: Any = document
    for segment in parse_path(path_text):
        key = segment.name if segment.key is None else f"{segment.name}[{segment.key}]"
        if not isinstance(node, FrozenStruct) or key not in node:
            return None
        node = node[key]
    return node


def _has_value(document: FrozenStruct, path_text: str) -> bool:
    """A path has a value if it is present and not null (feature 004, clarification 3)."""
    return _value_at(document, path_text) is not None


def _doc_violation(rule: str, paths: tuple[str, ...], message: str) -> list[Violation]:
    return [Violation(rule, paths[0], message)]  # the scope is the first path, so `explain` of that path lists it


def check_all_or_none(subject: Subject) -> list[Violation]:
    paths = subject.args.get("paths", ())
    given = sum(_has_value(subject.document, p) for p in paths)
    if 0 < given < len(paths):
        return _doc_violation("all_or_none", paths, "these fields go together: all or none")
    return []


def check_at_least_one(subject: Subject) -> list[Violation]:
    paths = subject.args.get("paths", ())
    if paths and not any(_has_value(subject.document, p) for p in paths):
        return _doc_violation("at_least_one", paths, "at least one of these fields is needed")
    return []


def check_at_most_one(subject: Subject) -> list[Violation]:
    paths = subject.args.get("paths", ())
    if sum(_has_value(subject.document, p) for p in paths) > 1:
        return _doc_violation("at_most_one", paths, "at most one of these fields may be given")
    return []


def _order(a: Any, b: Any) -> int | None:
    """-1, 0 or 1 for two values of the same comparable kind (numbers, text, timestamps), else None."""
    if _number(a) and _number(b):
        return int(a > b) - int(a < b)
    if isinstance(a, str) and isinstance(b, str):
        return int(a > b) - int(a < b)
    if isinstance(a, Timestamp) and isinstance(b, Timestamp):
        left, right = timestamp_sort_key(a), timestamp_sort_key(b)
        return (left > right) - (left < right)
    return None


def check_ordered(subject: Subject) -> list[Violation]:
    first, second = subject.args.get("first"), subject.args.get("second")
    if first is None or second is None:
        return []
    low, high = _value_at(subject.document, first), _value_at(subject.document, second)
    if low is None or high is None:
        return []  # nothing to compare
    order = _order(low, high)
    if order is None:
        return _doc_violation("ordered", (first,), "these fields cannot be compared")
    if order > 0:
        return _doc_violation("ordered", (first,), "must not be after the other field")
    return []


def _number(value: Any) -> bool:
    return isinstance(value, int | float | Decimal) and not isinstance(value, bool)


def range_args(args: Any) -> list[str]:
    """What a declaration of the arguments cannot say: `min` must not be above `max`."""
    low, high = args.get("min"), args.get("max")
    if low is not None and high is not None and low > high:
        return ["min is greater than max"]
    return []


def register_validators(registry: BehaviorRegistry) -> None:
    registry.register_validate("non_empty", 1, check_non_empty, args=(), applies="field")
    registry.register_validate(
        "range",
        1,
        check_range,
        range_args,
        args=(ArgSpec("min", "number", required=False), ArgSpec("max", "number", required=False)),
        applies="field",
    )
    registry.register_validate("max_len", 1, check_max_len, args=(ArgSpec("n", "int", min=0),), applies="field")
    registry.register_validate("min_len", 1, check_min_len, args=(ArgSpec("n", "int", min=0),), applies="field")
    registry.register_validate("pattern", 1, check_pattern, args=(ArgSpec("regex", "pattern"),), applies="field")
    registry.register_validate("one_of", 1, check_one_of, args=(ArgSpec("values", "values", min=1),), applies="field")
    registry.register_validate("unique_items", 1, check_unique_items, args=(), applies="field")
    registry.register_validate(
        "all_or_none", 1, check_all_or_none, args=(ArgSpec("paths", "paths", min=2),), applies="document"
    )
    registry.register_validate(
        "at_least_one", 1, check_at_least_one, args=(ArgSpec("paths", "paths", min=1),), applies="document"
    )
    registry.register_validate(
        "at_most_one", 1, check_at_most_one, args=(ArgSpec("paths", "paths", min=2),), applies="document"
    )
    registry.register_validate(
        "ordered",
        1,
        check_ordered,
        args=(ArgSpec("first", "path"), ArgSpec("second", "path")),
        applies="document",
    )
