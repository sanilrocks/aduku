"""Validation rules (SPEC 10): pure behaviors that look at a document and report violations.

Every validator takes one `Subject` and returns a list of `Violation`. A field rule sees the value at one path; a document
rule sees the whole document. Rules describe the constraint in their messages and never repeat the value.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from aduku.behaviors.registry import BehaviorRegistry
from aduku.ion_codec import FrozenStruct
from aduku.model import Violation

__all__ = ["Subject", "check_max_len", "check_non_empty", "check_range", "register_validators"]


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


def _number(value: Any) -> bool:
    return isinstance(value, int | float | Decimal) and not isinstance(value, bool)


def range_args(args: Any) -> list[str]:
    problems = [f"unknown argument {key!r}" for key in args if key not in ("min", "max")]
    bounds = [args[key] for key in ("min", "max") if key in args]
    problems += [f"{key} must be a number" for key in ("min", "max") if key in args and not _number(args[key])]
    if not problems and len(bounds) == 2 and bounds[0] > bounds[1]:
        problems.append("min is greater than max")
    return problems


def max_len_args(args: Any) -> list[str]:
    problems = [f"unknown argument {key!r}" for key in args if key != "n"]
    n = args.get("n")
    if "n" not in args:
        problems.append("n is required")
    elif isinstance(n, bool) or not isinstance(n, int) or n < 0:
        problems.append("n must be a whole number, zero or more")
    return problems


def register_validators(registry: BehaviorRegistry) -> None:
    registry.register_validate("non_empty", 1, check_non_empty)
    registry.register_validate("range", 1, check_range, range_args)
    registry.register_validate("max_len", 1, check_max_len, max_len_args)
