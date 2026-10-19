"""US2: a `per_fetch` value is fetched on read within a ttl, again when its inputs change or a fresh read is asked for,
and a read never waits longer than the fetch deadline (FR-007, FR-008, FR-018, SC-003)."""

import time
from decimal import Decimal

from aduku.storage.base import Storage
from tests.deferred_helpers import build, deferred, deferred_value, delayed
from tests.service_helpers import ALICE, put

RATE = f'"/fxRate": {deferred(hook="fx.rate2@1", depends=chr(34) + "/currency" + chr(34), refresh="per_fetch", type_="decimal", ttl=chr(34) + "5m" + chr(34))}'


def rate(built, doc: str = "d1"):  # type: ignore[no-untyped-def]
    return deferred_value(built.service.get(doc), "fxRate")


def test_a_value_is_reused_inside_the_ttl_and_fetched_again_after_it(backend: Storage) -> None:
    built = build(backend, RATE)
    built.service.append("d1", [put("/currency", "EUR")], contributor=ALICE)
    assert rate(built) == Decimal("0.92") and built.calls("fx.rate2@1") == 1
    built.clock.advance(299)
    assert rate(built) == Decimal("0.92") and built.calls("fx.rate2@1") == 1
    built.clock.advance(1)
    assert rate(built) == Decimal("0.93") and built.calls("fx.rate2@1") == 2  # the new answer replaces the stored one
    assert rate(built) == Decimal("0.93") and built.calls("fx.rate2@1") == 2


def test_a_changed_input_is_fetched_again_inside_the_ttl(backend: Storage) -> None:
    built = build(backend, RATE)
    built.service.append("d1", [put("/currency", "EUR")], contributor=ALICE)
    rate(built)
    built.clock.advance(10)
    built.service.append("d1", [put("/currency", "GBP")], contributor=ALICE)
    assert rate(built) == Decimal("0.92") and built.calls("fx.rate2@1") == 2  # a value for EUR does not answer for GBP
    assert rate(built) == Decimal("0.92") and built.calls("fx.rate2@1") == 2  # the first answer for GBP


def test_a_fresh_read_ignores_the_ttl_once_and_the_next_normal_read_reuses_it(backend: Storage) -> None:
    built = build(backend, RATE)
    built.service.append("d1", [put("/currency", "EUR")], contributor=ALICE)
    rate(built)
    built.clock.advance(1)
    assert deferred_value(built.service.get("d1", consistency="fresh"), "fxRate") == Decimal("0.93")
    assert built.calls("fx.rate2@1") == 2
    assert rate(built) == Decimal("0.93") and built.calls("fx.rate2@1") == 2  # the fresh read stored its answer


def test_the_default_currency_is_an_input_like_any_other(backend: Storage) -> None:
    built = build(backend, RATE)
    built.service.append("d1", [put("/name", "Alice")], contributor=ALICE)  # /currency has its default, USD
    assert rate(built) == Decimal("0.92") and built.stored()["/fxRate"].input_fingerprint


def test_a_slow_source_ends_the_read_at_the_fetch_deadline_and_the_field_follows_its_policy(backend: Storage) -> None:
    fields = f'"/fxRate": {deferred(hook="fx.never@1", refresh="per_fetch", ttl=chr(34) + "5m" + chr(34))}'
    built = build(backend, fields, extra=delayed("fx.never", None), real_time=True, fetch_deadline=0.2)
    built.service.append("d1", [put("/name", "Alice")], contributor=ALICE)
    started = time.monotonic()
    result = built.service.get("d1")
    assert time.monotonic() - started < 0.2 + 0.4
    assert [(d.path, d.status) for d in result.deferred] == [("/fxRate", "error")] and deferred_value(
        result, "fxRate"
    ) is None
    assert built.stored()["/fxRate"].error in ("timeout", "deadline")


def test_several_slow_fields_share_one_deadline_instead_of_adding_up(backend: Storage) -> None:
    fields = ", ".join(
        f'"/f{n}": {deferred(hook="fx.never@1", refresh="per_fetch", ttl=chr(34) + "5m" + chr(34))}' for n in range(3)
    )
    built = build(backend, fields, extra=delayed("fx.never", None), real_time=True, fetch_deadline=0.2)
    built.service.append("d1", [put("/name", "Alice")], contributor=ALICE)
    started = time.monotonic()
    result = built.service.get("d1")
    assert time.monotonic() - started < 0.2 + 0.4  # three calls side by side, not three deadlines in a row
    assert [d.status for d in result.deferred] == ["error"] * 3


def test_a_late_valid_answer_is_stored_for_the_next_read(backend: Storage) -> None:
    fields = f'"/fxRate": {deferred(hook="fx.late@1", refresh="per_fetch", ttl=chr(34) + "5m" + chr(34))}'
    built = build(backend, fields, extra=delayed("fx.late", 0.4, 7), real_time=True, fetch_deadline=0.1)
    built.service.append("d1", [put("/name", "Alice")], contributor=ALICE)
    assert deferred_value(built.service.get("d1"), "fxRate") is None  # this read gave up
    time.sleep(0.6)  # the source answers after the read has returned
    result = built.service.get("d1")
    assert (
        deferred_value(result, "fxRate") == 7 and built.calls("fx.late@1") == 1
    )  # the answer was kept; no second call


def test_a_read_never_waits_for_a_rate_limit_the_excess_follows_the_failure_policy(backend: Storage) -> None:
    from aduku.behaviors.registry import BehaviorRegistry
    from tests.fixtures import CountingHook

    def register(behaviors: BehaviorRegistry) -> None:
        behaviors.register_hook("fx.limited", 1, CountingHook("fx.limited", lambda i, n: 5), rate_limit=1.0)

    ttl = chr(34) + "5m" + chr(34)
    fields = ", ".join(f'"/{name}": {deferred(hook="fx.limited@1", refresh="per_fetch", ttl=ttl)}' for name in "ab")
    built = build(backend, fields, extra=register)
    built.service.append("d1", [put("/name", "Alice")], contributor=ALICE)
    started = time.monotonic()
    result = built.service.get("d1")
    assert time.monotonic() - started < 0.5  # no waiting
    assert built.calls("fx.limited@1") == 1 and sorted(d.status for d in result.deferred) == ["error", "ok"]
    assert [r.error for r in built.stored().values() if r.status == "error"] == ["rate_limited"]
    built.clock.advance(1)  # the limiter hands out one call a second: one more field gets its turn
    built.service.get("d1", consistency="fresh")
    assert built.calls("fx.limited@1") == 2


class _ByName:
    """A source whose answer depends on the name: Alice's call is slow (and finishes late), anyone else's is quick."""

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, inputs: dict[str, object], timeout: float) -> object:
        self.calls += 1
        if inputs["/name"] == "Alice":
            time.sleep(0.4)
            return "from-alice"
        return "from-bob"


def _by_name(backend: Storage):  # type: ignore[no-untyped-def]
    from aduku.behaviors.registry import BehaviorRegistry

    def register(behaviors: BehaviorRegistry) -> None:
        behaviors.register_hook("fx.byname", 1, _ByName())

    ttl = chr(34) + "5m" + chr(34)
    fields = f'"/note": {deferred(hook="fx.byname@1", type_="string", refresh="per_fetch", ttl=ttl, retry="{max: 0}")}'
    built = build(backend, fields, extra=register, sources=[], real_time=True, fetch_deadline=0.1)
    built.service.append("d1", [put("/name", "Alice")], contributor=ALICE)
    return built


def test_a_late_answer_for_old_inputs_does_not_replace_a_value_for_the_current_ones(backend: Storage) -> None:
    built = _by_name(backend)
    built.service.get("d1")  # asks about Alice, gives up after 0.1 s
    built.service.append("d1", [put("/name", "Bob")], contributor=ALICE)
    assert deferred_value(built.service.get("d1"), "note") == "from-bob"
    time.sleep(0.6)  # Alice's answer arrives now
    assert deferred_value(built.service.get("d1"), "note") == "from-bob"
    assert built.stored()["/note"].value == '"from-bob"'
    assert built.calls("fx.byname@1") == 2  # Alice's, Bob's: the late answer cost no third call


def test_a_late_answer_keeps_a_mark_made_after_the_call_started(backend: Storage) -> None:
    from tests.service_helpers import ROOT

    built = _by_name(backend)
    built.service.get("d1")  # gives up; an error record is stored
    built.service.invalidate("d1", "/note", caller=ROOT)
    time.sleep(0.6)  # the late answer is kept, but it may predate the mark
    record = built.stored()["/note"]
    assert record.value == '"from-alice"' and record.invalidated is True
