"""Feature 006 (research R-5): two derivations of one document compare to what a reader would see change. Pure."""

from __future__ import annotations

from aduku.engine.compaction import Observed
from aduku.engine.dryrun import compare, failed
from aduku.model import ModelError, Shadowed


def seen(
    document: str = "{a:1}",
    errors: tuple[ModelError, ...] = (),
    skipped: tuple[int, ...] = (),
    conflicts: tuple[str, ...] = (),
) -> Observed:
    return Observed(document, errors, skipped, conflicts)


def error(rule: str, path: str, message: str = "m") -> ModelError:
    return ModelError(rule, path, message)


def test_identical_derivations_are_unchanged() -> None:
    change = compare("d", seen(), seen(), [], False)
    assert (change.doc, change.document_changed, change.changed, change.failure_code) == ("d", False, False, None)
    assert change.errors_added == change.errors_resolved == () and change.shadowed_by_reason == ()


def test_a_different_document_is_reported_changed_without_holding_either_document() -> None:
    change = compare("d", seen("{a:1}"), seen("{a:2}"), [], False)
    assert change.document_changed and change.changed
    assert "a:1" not in repr(change) and "a:2" not in repr(change)


def test_model_errors_are_compared_by_identity_not_by_message() -> None:
    before = seen(errors=(error("r1", "/a", "old text"), error("r2", "/b")))
    after = seen(errors=(error("r1", "/a", "new text"), error("r3", "/c")))
    change = compare("d", before, after, [], False)
    assert change.errors_added == ("r3@/c",) and change.errors_resolved == ("r2@/b",) and change.changed
    assert not compare("d", before, seen(errors=(error("r1", "/a", "other"), error("r2", "/b"))), [], False).changed


def test_errors_are_listed_in_a_fixed_order() -> None:
    after = seen(errors=(error("z", "/b"), error("a", "/b"), error("a", "/a")))
    assert compare("d", seen(), after, [], False).errors_added == ("a@/a", "a@/b", "z@/b")


def test_skipped_before_and_after_are_reported_and_a_difference_is_a_change() -> None:
    change = compare("d", seen(skipped=(2,)), seen(skipped=(2, 5)), [], False)
    assert (change.skipped_before, change.skipped_after, change.changed) == ((2,), (2, 5), True)


def test_shadowed_entries_are_counted_per_reason_in_reason_order() -> None:
    shadowed = [
        Shadowed(5, "e5", "user", "/a", "now_derived"),
        Shadowed(2, "e2", "user", "/b", "no_longer_conforms"),
        Shadowed(3, "e3", "user", "/c", "now_derived"),
    ]
    change = compare("d", seen(), seen(skipped=(2, 3, 5)), shadowed, False)
    assert change.shadowed_by_reason == (("no_longer_conforms", 1), ("now_derived", 2))


def test_a_conflict_that_appears_or_goes_is_a_change() -> None:
    assert compare("d", seen(), seen(conflicts=("/a",)), [], False).changed
    assert not compare("d", seen(conflicts=("/a",)), seen(conflicts=("/a",)), [], False).changed


def test_would_call_hook_is_carried() -> None:
    assert compare("d", seen(), seen(), [], True).would_call_hook is True


def test_a_document_that_cannot_be_derived_is_failed_with_a_code_and_is_not_a_change() -> None:
    result = failed("d", "HISTORY_COMPACTED")
    assert (result.doc, result.failure_code, result.changed) == ("d", "HISTORY_COMPACTED", False)
    assert result.failed and not compare("d", seen(), seen(), [], False).failed
