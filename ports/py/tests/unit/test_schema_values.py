"""Feature 006: the values of the change list, the dry run, the history and the shadow reasons."""

from datetime import UTC, datetime

import pytest

from aduku.model import (
    ActivationReport,
    Change,
    ChangeList,
    DocumentChange,
    DryRunReport,
    HistoryRecord,
    MigrationReport,
    Shadowed,
)


def change(path: str, kind: str, cls: str, classified: bool = True) -> Change:
    return Change(kind, path, f"{kind} at {path}", cls, classified)  # type: ignore[arg-type]


def test_a_change_is_frozen_and_remembers_whether_it_was_classified() -> None:
    one = change("/a", "field_added", "compatible")
    assert one.classified is True and one.detail == ()
    with pytest.raises(AttributeError):
        one.kind = "x"  # type: ignore[misc]
    assert change("/a", "other", "visible", classified=False).classified is False


def test_a_change_list_is_sorted_by_path_and_kind_and_its_overall_class_is_the_worst() -> None:
    items = [
        change("/b", "merge_changed", "breaking"),
        change("/a", "field_added", "compatible"),
        change("/a", "default_changed", "visible"),
    ]
    listed = ChangeList.of(1, 2, items)
    assert [(c.path, c.kind) for c in listed.changes] == [
        ("/a", "default_changed"),
        ("/a", "field_added"),
        ("/b", "merge_changed"),
    ]
    assert listed.overall == "breaking" and (listed.old_version, listed.new_version) == (1, 2)
    assert ChangeList.of(1, 2, items[1:]).overall == "visible"
    assert ChangeList.of(1, 2, items[1:2]).overall == "compatible"


def test_an_empty_change_list_is_compatible() -> None:
    assert ChangeList.of(1, 1, []).overall == "compatible" and ChangeList.of(1, 1, []).changes == ()


def test_the_same_changes_in_any_order_make_the_same_list() -> None:
    items = [change("/b", "merge_changed", "breaking"), change("/a", "field_added", "compatible")]
    assert ChangeList.of(1, 2, items) == ChangeList.of(1, 2, list(reversed(items)))


def test_the_other_values_have_the_fields_of_the_data_model() -> None:
    now = datetime(2026, 10, 20, tzinfo=UTC)
    assert Shadowed(3, "e3", "user", "/age", "now_derived", "computed").reason == "now_derived"
    record = HistoryRecord(1, now, "alice", "activate", None, 1, 2, "visible", "{}")
    assert (record.doc, record.change_class) == (None, "visible")
    one = DocumentChange("d1", True, (), (), (), (3,), (("now_derived", 1),), False, None)
    assert one.document_changed and one.failure_code is None
    report = DryRunReport(ChangeList.of(1, 2, []), 2, 5, 1, 0, 1, 0, 0, 0, (one,))
    assert (report.examined, report.left_out, report.changed, report.generation) == (1, 0, 1, 5)
    assert ActivationReport(2, 1, "visible", False, None).rollback is False
    assert MigrationReport((), (), (), (), 0).batches == 0
