"""Feature 003, data-model.md: the value types of the deferred stage."""

from datetime import UTC, datetime

import pytest

from aduku.model import (
    DeferredInfo,
    DeferredStatus,
    DerivedRecord,
    DocumentResult,
    SchedulerReport,
    SchedulerRun,
)

NOW = datetime(2026, 10, 19, 12, 0, tzinfo=UTC)


def test_a_document_result_has_no_deferred_statuses_by_default() -> None:
    assert DocumentResult(document=None).deferred == ()


def test_a_derived_record_holds_the_last_good_value_and_how_it_got_there() -> None:
    record = DerivedRecord(
        doc="d1",
        field="/creditScore",
        value="712",
        has_value=True,
        as_of=NOW,
        input_fingerprint="f" * 64,
        hook="fx.score@1",
        shape="s" * 64,
        schema_version=1,
        status="ok",
    )
    assert (record.error, record.attempts, record.invalidated, record.last_attempt) == (None, 1, False, None)
    with pytest.raises(AttributeError):
        record.status = "error"  # type: ignore[misc]


def test_a_deferred_status_defaults_to_nothing_waited_for() -> None:
    status = DeferredStatus("/creditScore", "pending")
    assert (status.as_of, status.error, status.attempts, status.waiting_for, status.reason) == (
        None,
        None,
        None,
        (),
        None,
    )


def test_info_and_scheduler_types_compare_by_value() -> None:
    info = DeferredInfo("/fxRate", "stale", NOW, 2, False)
    assert info == DeferredInfo("/fxRate", "stale", NOW, 2, False)
    run = SchedulerRun("r1", "fx.rate@1", NOW, NOW, attempted=3, succeeded=2, failed=1, skipped=0, calls=1)
    report = SchedulerReport(run.run_id, run.hook, NOW, NOW, 3, 2, 1, 0, 1)
    assert report.reason is None and report.items == ()
