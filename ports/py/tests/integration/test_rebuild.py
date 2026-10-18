"""`rebuild` re-derives from the ledger and compares with the cache (conformance item 6)."""

import logging

import pytest

from aduku.ion_codec import to_canonical
from aduku.storage.base import Storage
from tests.integration.test_version_change import V7, V8, build, named
from tests.service_helpers import ALICE, put


def test_rebuild_reproduces_the_cached_view_bit_for_bit_with_attribution(backend: Storage) -> None:
    service = named(backend)
    service.schemas.activate(8)
    service.rebuild("d1")  # stores the view under version 8: an error attributed to a request, one to the version
    with backend.read() as tx:
        cached = tx.get_view("d1")
    report = service.rebuild("d1")
    assert report.identical and report.checked == ("d1",) and report.differing == ()
    with backend.read() as tx:
        assert tx.get_view("d1") == cached


def test_rebuild_with_no_document_checks_every_document(backend: Storage) -> None:
    service = build(backend, V7, V8, active=7)
    for doc in ("a", "b", "c"):
        service.append(doc, [put("/name", "x")], contributor=ALICE)
    assert service.rebuild().checked == ("a", "b", "c")


def test_rebuild_reports_and_repairs_a_cache_that_differs(backend: Storage) -> None:
    service = build(backend, V7, V8, active=7)
    service.append("d1", [put("/name", "x")], contributor=ALICE)
    with backend.read() as tx:
        good = tx.get_view("d1")
    assert good is not None
    with backend.transaction() as tx:
        tx.put_view(type(good)(**{**good.__dict__, "document": to_canonical({"name": "tampered"})}))
    report = service.rebuild("d1")
    assert not report.identical and report.differing == ("d1",)
    with backend.read() as tx:
        assert tx.get_view("d1") == good


def test_rebuild_treats_a_view_from_another_version_as_stale_not_as_different(backend: Storage) -> None:
    service = build(backend, V7, V8, active=7)
    service.append("d1", [put("/name", "Pat")], contributor=ALICE)
    service.schemas.activate(8)
    report = service.rebuild("d1")
    assert report.identical and report.refreshed == ("d1",)
    assert [e.rule_id for e in service.get("d1").model_errors] == ["max_len"]


def test_rebuild_of_a_document_with_no_cached_view_just_derives_it(backend: Storage) -> None:
    service = build(backend, V7, active=7)
    service.append("d1", [put("/name", "x")], contributor=ALICE)
    with backend.transaction() as tx:
        tx.drop_view("d1")
    report = service.rebuild("d1")
    assert report.identical and report.refreshed == ("d1",)


def test_rebuild_emits_one_structured_event(backend: Storage, caplog: pytest.LogCaptureFixture) -> None:
    service = build(backend, V7, active=7)
    service.append("d1", [put("/name", "x")], contributor=ALICE)
    with caplog.at_level(logging.INFO, logger="aduku"):
        service.rebuild()
    assert any('"event": "rebuild"' in r.getMessage() for r in caplog.records)


def test_a_document_that_does_not_exist_is_checked_and_not_refreshed(backend: Storage) -> None:
    report = build(backend, V7, active=7).rebuild("nothing")
    assert report.identical and report.checked == ("nothing",) and report.refreshed == ()


def test_one_document_that_cannot_be_derived_does_not_stop_the_others(backend: Storage) -> None:
    both = build(backend, V7, V8, active=7)
    both.append("a", [put("/name", "x")], contributor=ALICE)  # written under version 7
    only_new = build(backend, V8, active=8, prefix="n")  # this deployment no longer knows version 7
    only_new.append("b", [put("/name", "x")], contributor=ALICE)
    only_new.append("c", [put("/name", "y")], contributor=ALICE)
    report = only_new.rebuild()
    assert report.checked == ("a", "b", "c") and not report.identical
    assert [doc for doc, _ in report.failed] == ["a"] and "SCHEMA_INACTIVE" in report.failed[0][1]
    assert report.differing == ()


def test_reading_a_document_that_cannot_be_derived_is_a_typed_error(backend: Storage) -> None:
    from aduku.errors import AdukuError

    both = build(backend, V7, V8, active=7)
    both.append("a", [put("/name", "x")], contributor=ALICE)
    only_new = build(backend, V8, active=8, prefix="n")
    with pytest.raises(AdukuError):
        only_new.get("a")


def test_rebuilding_a_document_with_no_entries_derives_nothing(backend: Storage) -> None:
    service = build(backend, V7, active=7)
    before = service.derivations
    service.rebuild("nothing")
    assert service.derivations == before
