"""US8: `invalidate` marks a stored value out of date for the callers the schema lists (FR-020, FR-025, FR-028)."""

import json
import logging

import pytest

from aduku.errors import ErrorCode, RejectedError
from aduku.model import Identity
from aduku.storage.base import Storage
from tests.deferred_helpers import build, deferred, deferred_value
from tests.service_helpers import ALICE, ROOT, put

SCORE = f'"/creditScore": {deferred(invalidate_by="[admin]")}'


def test_a_listed_caller_marks_the_value_and_the_next_read_fetches_again(backend: Storage) -> None:
    built = build(backend, SCORE)
    built.service.append("d1", [put("/name", "Alice")], contributor=ALICE)
    built.service.get("d1")
    assert built.service.invalidate("d1", "/creditScore", caller=ROOT) == 1
    assert built.stored()["/creditScore"].invalidated is True
    assert built.calls("fx.score@1") == 1  # marking calls nothing
    assert deferred_value(built.service.get("d1"), "creditScore") == 712 and built.calls("fx.score@1") == 2
    assert built.stored()["/creditScore"].invalidated is False  # a successful call clears the mark


def test_a_caller_outside_the_list_is_refused_and_nothing_changes(backend: Storage) -> None:
    built = build(backend, SCORE)
    built.service.append("d1", [put("/name", "Alice")], contributor=ALICE)
    built.service.get("d1")
    with pytest.raises(RejectedError) as caught:
        built.service.invalidate("d1", "/creditScore", caller=Identity("eve", ("user",)))
    assert caught.value.code is ErrorCode.NOT_AUTHORIZED
    assert built.stored()["/creditScore"].invalidated is False and built.calls("fx.score@1") == 1


def test_a_role_in_the_list_is_enough(backend: Storage) -> None:
    built = build(backend, f'"/creditScore": {deferred(invalidate_by="[ops]")}', top="roles: [ops],")
    built.service.append("d1", [put("/name", "Alice")], contributor=ALICE)
    built.service.get("d1")
    assert built.service.invalidate("d1", "/creditScore", caller=Identity("op", ("backfill",), ("ops",))) == 1


def test_no_document_marks_every_document_that_holds_a_value(backend: Storage) -> None:
    built = build(backend, SCORE)
    for doc in ("d1", "d2", "d3"):
        built.service.append(doc, [put("/name", doc)], contributor=ALICE)
    built.service.get("d1")
    built.service.get("d2")
    assert built.service.invalidate(None, "/creditScore", caller=ROOT) == 2


def test_an_unknown_field_is_an_unknown_path(backend: Storage) -> None:
    built = build(backend, SCORE)
    for field in ("/name", "/nothing"):
        with pytest.raises(RejectedError) as caught:
            built.service.invalidate("d1", field, caller=ROOT)
        assert caught.value.code is ErrorCode.UNKNOWN_PATH


def test_a_failed_call_keeps_the_mark_and_a_later_success_clears_it(backend: Storage) -> None:
    built = build(
        backend,
        f'"/creditScore": {deferred(hook="fx.flaky@1", depends=chr(34) + "/country" + chr(34), type_="string", retry="{max: 0}")}',
    )
    built.service.append("d1", [put("/name", "Alice"), put("/country", "US")], contributor=ALICE)
    built.service.invalidate("d1", "/creditScore", caller=ROOT)  # no record yet: nothing to mark
    assert built.stored() == {}
    built.service.get("d1")  # flaky fails its first call for this input
    assert built.stored()["/creditScore"].status == "error"
    built.service.invalidate("d1", "/creditScore", caller=ROOT)
    built.service.get("d1")  # flaky answers now
    record = built.stored()["/creditScore"]
    assert (record.status, record.invalidated) == ("ok", False)


def test_the_event_names_who_what_and_where_and_carries_no_value(
    backend: Storage, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="aduku")
    built = build(backend, SCORE)
    built.service.append("d1", [put("/name", "Alice")], contributor=ALICE)
    built.service.get("d1")
    built.service.invalidate("d1", "/creditScore", caller=ROOT)
    seen = [json.loads(r.getMessage()) for r in caplog.records if r.name == "aduku"]
    event = next(e for e in seen if e["event"] == "invalidate")
    assert (event["caller"], event["doc"], event["field"]) == ("root", "d1", "/creditScore")
    assert "712" not in json.dumps(event)


def test_an_invalidate_that_arrives_while_a_fetch_is_in_flight_is_not_lost(backend: Storage) -> None:
    from aduku.behaviors.registry import BehaviorRegistry

    class Racing:
        """A source that, while `race` is set, has the value marked out of date during its own call."""

        def __init__(self) -> None:
            self.race = False
            self.calls = 0
            self.built = None

        def __call__(self, inputs: dict[str, object], timeout: float) -> object:
            self.calls += 1
            if self.race:
                self.built.service.invalidate("d1", "/creditScore", caller=ROOT)  # type: ignore[attr-defined]
            return 700 + self.calls

    source = Racing()

    def register(behaviors: BehaviorRegistry) -> None:
        behaviors.register_hook("fx.mid", 1, source)

    ttl = chr(34) + "5m" + chr(34)
    field = f'"/creditScore": {deferred(hook="fx.mid@1", refresh="per_fetch", ttl=ttl)}'
    built = build(backend, field, extra=register, sources=[])
    source.built = built  # type: ignore[assignment]
    built.service.append("d1", [put("/name", "Alice")], contributor=ALICE)
    built.service.get("d1")  # a first fetch, which stores a clean record
    built.clock.advance(301)  # past the ttl: the next read fetches, and the value is marked out of date during the call
    source.race = True
    built.service.get("d1")
    assert built.stored()["/creditScore"].invalidated is True  # the mark made during the call survives its answer
    source.race = False
    built.service.get("d1")
    assert source.calls == 3 and built.stored()["/creditScore"].invalidated is False  # asked again, and now it is clear
