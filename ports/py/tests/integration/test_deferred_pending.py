"""US8: what is pending and why (`include=("pending",)`), and the stored state of each field without its value (FR-015, FR-026)."""

from aduku.behaviors.registry import BehaviorRegistry
from aduku.storage.base import Storage
from tests.deferred_helpers import build, deferred
from tests.fixtures import CountingHook
from tests.service_helpers import ALICE, put

SCORE_UP = f'"/creditScore": {deferred(hook="fx.score@1")}'
LIMIT = f'"/creditLimit": {deferred(hook="fx.limit@1", depends=chr(34) + "/creditScore" + chr(34) + ", " + chr(34) + "/country" + chr(34))}'


def by_path(result):  # type: ignore[no-untyped-def]
    return {d.path: d for d in result.deferred}


def test_pending_state_says_what_each_field_waits_for_and_only_when_asked(backend: Storage) -> None:
    built = build(
        backend,
        f"{SCORE_UP}, {LIMIT}",
        sources=["fx.limit@1"],
        extra=lambda b: b.register_hook("fx.score", 1, CountingHook("fx.score", lambda i, n: 1 / 0)),
    )
    built.service.append("d1", [put("/name", "Alice")], contributor=ALICE)  # no /country
    plain = by_path(built.service.get("d1"))
    assert plain["/creditLimit"].waiting_for == () and plain["/creditLimit"].reason is None
    asked = by_path(built.service.get("d1", include=("pending",)))
    assert (asked["/creditLimit"].status, asked["/creditLimit"].reason) == ("pending", "upstream")
    assert asked["/creditLimit"].waiting_for == ("/creditScore", "/country")


def test_an_input_that_is_missing_is_named(backend: Storage) -> None:
    built = build(backend, SCORE_UP)
    built.service.append("d1", [put("/country", "US")], contributor=ALICE)
    status = by_path(built.service.get("d1", include=("pending",)))["/creditScore"]
    assert (status.status, status.reason, status.waiting_for) == ("pending", "inputs_missing", ("/name",))


def test_a_field_not_yet_fetched_says_so(backend: Storage) -> None:
    hourly = f'"/band": {deferred(hook="fx.pair@1", depends=chr(34) + "/country" + chr(34), type_="string", refresh="scheduled::{every: " + chr(34) + "1h" + chr(34) + "}")}'
    built = build(backend, hourly, sources=["fx.pair@1"])
    built.service.append("d1", [put("/country", "US")], contributor=ALICE)
    status = by_path(built.service.get("d1", include=("pending",)))["/band"]
    assert (status.status, status.reason, status.waiting_for) == ("pending", "not_fetched", ())


def test_an_upstream_in_error_with_nothing_to_serve_holds_the_dependent_back(backend: Storage) -> None:
    built = build(
        backend, f"{SCORE_UP.replace('fx.score@1', 'fx.down@1')}, {LIMIT}", sources=["fx.down@1", "fx.limit@1"]
    )
    built.service.append("d1", [put("/name", "Alice"), put("/country", "US")], contributor=ALICE)
    shown = by_path(built.service.get("d1", include=("pending",)))
    assert (shown["/creditScore"].status, shown["/creditLimit"].status) == ("error", "pending")
    assert built.calls("fx.limit@1") == 0


def test_a_stale_upstream_does_not_hold_the_dependent_back(backend: Storage) -> None:
    class Flip:
        down = False

        def __call__(self, inputs: dict[str, object], timeout: float) -> object:
            if self.down:
                raise RuntimeError("down")
            return 700

    flip = Flip()

    def register(behaviors: BehaviorRegistry) -> None:
        behaviors.register_hook("fx.flip", 1, flip)

    ttl = chr(34) + "5m" + chr(34)
    up = f'"/creditScore": {deferred(hook="fx.flip@1", refresh="per_fetch", ttl=ttl, on_failure="serve_stale", retry="{max: 0}")}'
    built = build(backend, f"{up}, {LIMIT}", sources=["fx.limit@1"], extra=register)
    built.service.append("d1", [put("/name", "Alice"), put("/country", "US")], contributor=ALICE)
    built.service.get("d1")
    flip.down = True
    built.clock.advance(301)
    shown = by_path(built.service.get("d1"))
    assert shown["/creditScore"].status == "stale" and shown["/creditLimit"].status == "ok"


def test_the_stored_state_is_listed_without_any_value(backend: Storage) -> None:
    built = build(backend, SCORE_UP)
    built.service.append("d1", [put("/name", "Alice")], contributor=ALICE)
    assert built.service.list_deferred("d1") == []
    built.service.get("d1")
    (info,) = built.service.list_deferred("d1")
    assert (info.field, info.status, info.attempts, info.invalidated) == ("/creditScore", "ok", 1, False)
    assert not hasattr(info, "value") and "712" not in repr(info)
