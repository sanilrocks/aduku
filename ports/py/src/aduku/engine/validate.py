"""P6: validate a document against the field rules and document rules of a schema version. Pure.

Deferred values are never inputs (they are not part of the document here). A validator that raises, or that does not return
a list of violations, is a fault: it is never read as "no violations" (SPEC 10.1).
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from aduku.behaviors.registry import BehaviorRegistry
from aduku.behaviors.validate import Subject
from aduku.errors import AdukuError, SchemaError
from aduku.ion_codec import FrozenStruct
from aduku.model import Violation
from aduku.paths import Path, PathError, Segment, format_path, parse_path
from aduku.schema.model import BehaviorRef, Schema

__all__ = ["ValidatorFault", "validate_document"]


# A field rule is given its value and nothing else (Constitution VIII); only a document rule sees the document.
NO_DOCUMENT = FrozenStruct()


class ValidatorFault(AdukuError):
    """A validator failed to run (as opposed to reporting a violation)."""

    def __init__(self, rule: str, reason: str) -> None:
        self.rule = rule
        super().__init__(f"validator {rule} faulted: {reason}")


def validate_document(schema: Schema, behaviors: BehaviorRegistry, document: FrozenStruct) -> list[Violation]:
    """Every violation, once each, sorted by (rule, path scope)."""
    found: dict[tuple[str, str], Violation] = {}
    for path, value in _nodes(document, ()):
        field = schema.field_for(path)
        if field is None:
            continue
        for ref in field.validate:
            for violation in _run(behaviors, ref, Subject(NO_DOCUMENT, format_path(path), value, ref.args)):
                found.setdefault((violation.rule_id, violation.path_scope), violation)
    for ref in schema.document_validate:
        for violation in _run(behaviors, ref, Subject(document, None, document, ref.args)):
            found.setdefault((violation.rule_id, violation.path_scope), violation)
    return [found[key] for key in sorted(found)]


def _nodes(value: FrozenStruct, prefix: Path) -> Iterator[tuple[Path, Any]]:
    for key in sorted(value):
        path = (*prefix, _segment(key))
        item = value[key]
        yield path, item
        if isinstance(item, FrozenStruct):
            yield from _nodes(item, path)


def _segment(key: str) -> Segment:
    """A key of the assembled document back to a path segment (`items[a]` is the segment `items` with key `a`)."""
    if "[" not in key:
        return Segment(key)
    try:
        parsed = parse_path("/" + key)
    except PathError:
        return Segment(key)
    return parsed[0] if len(parsed) == 1 else Segment(key)


def _run(behaviors: BehaviorRegistry, ref: BehaviorRef, subject: Subject) -> list[Violation]:
    behavior = behaviors.get(ref.name, ref.version)
    if behavior is None or behavior.kind != "validate":
        raise SchemaError(f"validator {ref.name}@{ref.version} is not registered in this service")
    try:
        result = behavior.impl(subject)
    except Exception as error:
        raise ValidatorFault(ref.name, f"{type(error).__name__}: {error}") from error
    if not isinstance(result, list) or not all(isinstance(item, Violation) for item in result):
        raise ValidatorFault(ref.name, "it did not return a list of violations")
    return result
