"""The comparison of two derivations of one document (feature 006, research R-5). Pure.

A dry run derives a document under the version now in force and under the target, over the same entries and the same recorded
outcomes, and keeps only what differs. The result never holds the document itself or any value: a document is reported as
changed or not, its model errors by identity (`rule@path`), its skipped entries by seq and its shadowed entries by reason.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable

from aduku.engine.compaction import Observed
from aduku.model import DocumentChange, ModelError, Shadowed

__all__ = ["compare", "failed"]


def _names(errors: Iterable[ModelError]) -> set[str]:
    return {f"{item.rule_id}@{item.path_scope}" for item in errors}


def compare(
    doc: str, before: Observed, after: Observed, shadowed: Iterable[Shadowed], would_call_hook: bool
) -> DocumentChange:
    """What changes in `doc` when `after` is read instead of `before`; `shadowed` are the entries left out under `after`."""
    was, now = _names(before.errors), _names(after.errors)
    reasons = Counter(item.reason for item in shadowed)
    return DocumentChange(
        doc=doc,
        document_changed=before.document != after.document,
        errors_added=tuple(sorted(now - was)),
        errors_resolved=tuple(sorted(was - now)),
        skipped_before=tuple(before.skipped),
        skipped_after=tuple(after.skipped),
        shadowed_by_reason=tuple(sorted(reasons.items())),
        would_call_hook=would_call_hook,
        conflicts_changed=tuple(before.conflicts) != tuple(after.conflicts),
    )


def failed(doc: str, code: str) -> DocumentChange:
    """A document that cannot be derived under the target: it is reported with the code, not counted as changed."""
    return DocumentChange(
        doc=doc,
        document_changed=False,
        errors_added=(),
        errors_resolved=(),
        skipped_before=(),
        skipped_after=(),
        shadowed_by_reason=(),
        would_call_hook=False,
        failure_code=code,
    )
