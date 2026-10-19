"""US5: a notification marks values out of date without a call; the next scheduler run recomputes exactly those (FR-011, SC-007)."""

from aduku.storage.base import Storage
from tests.deferred_helpers import Built, build, deferred, deferred_value
from tests.service_helpers import ALICE, ROOT, put

TOPIC = "sanctions.updated"
FLAG = f'"/flag": {deferred(hook="fx.pair@1", depends=chr(34) + "/country" + chr(34), type_="string", refresh="notification::{topic: " + chr(34) + TOPIC + chr(34) + "}")}'


def setup(backend: Storage) -> Built:
    built = build(backend, FLAG, sources=["fx.pair@1"])
    for doc, country in (("d1", "US"), ("d2", "DE")):
        built.service.append(doc, [put("/country", country)], contributor=ALICE)
    built.service.scheduler_run("fx.pair@1", caller=ROOT)  # the first fetch is the scheduler's
    return built


def pair_calls(built: Built) -> list[int]:
    return list(built.behaviors.get("fx.pair", 1).impl.batches)  # type: ignore[union-attr]


def marked(built: Built) -> dict[str, bool]:
    with built.storage.read() as tx:
        return {r.doc: r.invalidated for r in tx.list_derived(["/flag"])}


def test_a_notification_marks_only_the_documents_it_names_and_makes_no_call(backend: Storage) -> None:
    built = setup(backend)
    assert built.service.notify(TOPIC, ["d1"]) == 1
    assert marked(built) == {"d1": True, "d2": False} and pair_calls(built) == [2]


def test_a_notification_with_no_documents_marks_every_document_holding_a_value(backend: Storage) -> None:
    built = setup(backend)
    assert built.service.notify(TOPIC) == 2 and marked(built) == {"d1": True, "d2": True}


def test_another_topic_changes_nothing(backend: Storage) -> None:
    built = setup(backend)
    assert built.service.notify("something.else", ["d1"]) == 0 and marked(built) == {"d1": False, "d2": False}


def test_a_read_between_the_notification_and_the_run_serves_the_stored_value_and_calls_nothing(
    backend: Storage,
) -> None:
    built = setup(backend)
    built.service.notify(TOPIC, ["d1"])
    result = built.service.get("d1")
    assert (
        deferred_value(result, "flag") == "band-US" and result.deferred[0].status == "ok" and pair_calls(built) == [2]
    )


def test_the_next_run_recomputes_exactly_the_marked_documents_and_clears_the_mark(backend: Storage) -> None:
    built = setup(backend)
    built.service.notify(TOPIC, ["d2"])
    report = built.service.scheduler_run("fx.pair@1", caller=ROOT)
    assert (report.attempted, report.calls) == (1, 1) and pair_calls(built) == [2, 1]
    assert marked(built) == {"d1": False, "d2": False}
    assert built.service.scheduler_run("fx.pair@1", caller=ROOT).attempted == 0


def test_delivering_a_notification_twice_leaves_the_same_state_as_once(backend: Storage) -> None:
    built = setup(backend)
    built.service.notify(TOPIC, ["d1"])
    built.service.notify(TOPIC, ["d1"])
    assert marked(built) == {"d1": True, "d2": False}
    built.service.scheduler_run("fx.pair@1", caller=ROOT)
    assert pair_calls(built) == [2, 1]  # one recompute, not two


def test_a_document_that_does_not_exist_or_holds_no_value_is_ignored(backend: Storage) -> None:
    built = setup(backend)
    assert built.service.notify(TOPIC, ["nobody"]) == 0
    built.service.append("d3", [put("/country", "FR")], contributor=ALICE)  # no stored value yet
    assert built.service.notify(TOPIC, ["d3"]) == 0
