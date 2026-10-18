"""Fixture behaviors that conformance cases name (conformance/README.md)."""

from __future__ import annotations

from aduku.behaviors.registry import BehaviorRegistry, Laws
from aduku.behaviors.validate import Subject
from aduku.ion_codec import ABSENT
from aduku.model import Violation


def max_spouses_by_country(subject: Subject) -> list[Violation]:
    """Document rule: at most one spouse when /country is "US"."""
    document = subject.document
    spouses = document.get("spouses")
    if document.get("country") == "US" and isinstance(spouses, tuple) and len(spouses) > 1:
        return [Violation("max_spouses_by_country", "/spouses", "at most 1 spouse is allowed in US")]
    return []


def subtract(path: object, old: object, new: object, ctx: object) -> object:
    """Fixture merge: old minus new. Not associative, and declared as such."""
    return new if old is ABSENT else old - new  # type: ignore[operator]


def longest(path: object, old: object, new: object, ctx: object) -> object:
    """Fixture merge: the longer string wins, ties go to the larger string. Associative, commutative, idempotent."""
    if old is ABSENT:
        return new
    return max(old, new, key=lambda text: (len(text), text))  # type: ignore[arg-type]


MERGES = {
    "acme.subtract@1": ("acme.subtract", 1, subtract, Laws()),
    "acme.longest@1": ("acme.longest", 1, longest, Laws(associative=True, commutative=True, idempotent=True)),
}


def register_fixtures(behaviors: BehaviorRegistry, names: list[str] | None = None) -> None:
    wanted = names if names is not None else ["acme.max_spouses_by_country@1", *MERGES]
    for key in wanted:
        if key in MERGES:
            name, version, combine, laws = MERGES[key]
            if behaviors.get(name, version) is None:
                behaviors.register_merge(name, version, combine, laws)
        elif behaviors.get("acme.max_spouses_by_country", 1) is None:
            behaviors.register_validate("acme.max_spouses_by_country", 1, max_spouses_by_country)
