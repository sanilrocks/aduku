"""Feature 003, FR-027: `explain` on a deferred path reads the derived store and never calls a source."""

from aduku.storage.memory import MemoryStorage
from tests.deferred_helpers import START, build, deferred
from tests.service_helpers import ALICE, put


def test_explain_names_the_source_the_inputs_and_when_the_value_is_from() -> None:
    built = build(MemoryStorage(), f'"/score": {deferred()}')
    built.service.append("d1", [put("/name", "Alice")], contributor=ALICE)
    built.service.get("d1")
    explained = built.service.explain("d1", "/score")
    assert (explained.winner, explained.rule, explained.value) == ("deferred", "fx.score@1", 712)
    assert explained.dependencies == (("/name", "Alice"),) and explained.effect_seq is None
    assert (explained.status, explained.as_of) == ("ok", START)


def test_explain_never_calls_a_source_and_says_so_when_there_is_no_value_yet() -> None:
    built = build(MemoryStorage(), f'"/score": {deferred()}')
    built.service.append("d1", [put("/name", "Alice")], contributor=ALICE)
    explained = built.service.explain("d1", "/score")
    assert built.calls("fx.score@1") == 0
    assert (explained.winner, explained.value, explained.status, explained.as_of) == ("deferred", None, "pending", None)


def test_explain_reports_a_failed_field_as_an_error() -> None:
    built = build(MemoryStorage(), f'"/score": {deferred(hook="fx.down@1")}')
    built.service.append("d1", [put("/name", "Alice")], contributor=ALICE)
    built.service.get("d1")
    explained = built.service.explain("d1", "/score")
    assert (explained.status, explained.value, explained.winner) == ("error", None, "deferred")


def test_explain_of_a_field_waiting_on_its_inputs_has_no_dependencies() -> None:
    built = build(MemoryStorage(), f'"/score": {deferred()}')
    built.service.append("d1", [put("/country", "US")], contributor=ALICE)
    explained = built.service.explain("d1", "/score")
    assert (explained.status, explained.dependencies) == ("pending", ())
