"""US6: a failing source only changes a field's status. Retries, the per-source breaker, the three `on_failure` modes, and
recovery (FR-017, FR-018, FR-019, FR-020, SC-002, SC-008)."""

from aduku.behaviors.registry import BehaviorRegistry
from aduku.errors import ErrorCode, RejectedError
from aduku.storage.base import Storage
from tests.deferred_helpers import build, deferred, deferred_value
from tests.service_helpers import ALICE, ROOT, put

TTL = chr(34) + "5m" + chr(34)


class Toggle:
    """A source that answers 5 while it is up and raises while `down` is set."""

    def __init__(self) -> None:
        self.down = False
        self.calls = 0

    def __call__(self, inputs: dict[str, object], timeout: float) -> object:
        self.calls += 1
        if self.down:
            raise RuntimeError("secret detail the log must not repeat")
        return 5


def setup(backend: Storage, policy: str = "error", refresh: str = "per_fetch", **options: str):  # type: ignore[no-untyped-def]
    source = Toggle()

    def register(behaviors: BehaviorRegistry) -> None:
        behaviors.register_hook("fx.toggle", 1, source)

    extra = {"ttl": TTL} if refresh == "per_fetch" else {}
    fields = f'"/score": {deferred(hook="fx.toggle@1", refresh=refresh, on_failure=policy, **extra, **options)}'
    built = build(backend, fields, extra=register, sources=[])
    built.service.append("d1", [put("/name", "Alice")], contributor=ALICE)
    return built, source


def test_a_source_that_is_down_never_changes_what_a_write_does(backend: Storage) -> None:
    built, source = setup(backend)
    source.down = True
    plain = build(backend, '"/note": {type: string, merge: lww}', prefix="y", schema_state=False)
    outcomes = []
    for number, service in enumerate((built.service, plain.service)):
        doc = f"w{number}"
        first = service.append(doc, [put("/name", "Alice")], contributor=ALICE)
        try:
            service.append(doc, [put("/shout", "x", tier="admin")], contributor=ROOT)
            second = "accepted"
        except RejectedError as error:
            second = error.code
        outcomes.append((first.model_errors_added, second, len(service.list_entries(doc))))
    assert outcomes[0] == outcomes[1] == ((), ErrorCode.FIELD_NOT_WRITABLE, 1)
    built.service.get("w0")  # a read meets the failure; it is a status and nothing else
    assert built.service.get("w0").model_errors == ()


def test_error_policy_status_error_and_the_field_shown_without_a_value(backend: Storage) -> None:
    built, source = setup(backend, "error")
    built.service.get("d1")
    source.down = True
    built.clock.advance(301)
    result = built.service.get("d1")
    (status,) = result.deferred
    assert (status.status, status.error, status.attempts) == ("error", "error", 3)
    assert deferred_value(result, "score") is None and "score" in result.document


def test_serve_stale_shows_the_last_good_value_marked_stale(backend: Storage) -> None:
    built, source = setup(backend, "serve_stale")
    good = built.service.get("d1")
    source.down = True
    built.clock.advance(301)
    result = built.service.get("d1")
    (status,) = result.deferred
    assert (status.status, status.error, status.as_of) == ("stale", "error", good.deferred[0].as_of)
    assert deferred_value(result, "score") == 5


def test_serve_stale_without_a_good_value_is_an_error(backend: Storage) -> None:
    built, source = setup(backend, "serve_stale")
    source.down = True
    (status,) = built.service.get("d1").deferred
    assert status.status == "error"


def test_absent_policy_leaves_the_field_out_with_status_error(backend: Storage) -> None:
    built, source = setup(backend, "absent")
    built.service.get("d1")
    source.down = True
    built.clock.advance(301)
    result = built.service.get("d1")
    assert "score" not in result.document and result.deferred[0].status == "error"


def test_the_schema_default_applies_to_fields_that_name_no_policy(backend: Storage) -> None:
    source = Toggle()

    def register(behaviors: BehaviorRegistry) -> None:
        behaviors.register_hook("fx.toggle", 1, source)

    plain = f'"/score": {deferred(hook="fx.toggle@1", refresh="per_fetch", ttl=TTL)}'
    built = build(backend, plain, extra=register, sources=[], top="deferred_on_failure: absent,")
    built.service.append("d1", [put("/name", "Alice")], contributor=ALICE)
    source.down = True
    assert "score" not in built.service.get("d1").document


def test_a_call_is_retried_up_to_the_maximum_with_the_backoff_between_attempts(backend: Storage) -> None:
    built, source = setup(backend, retry="{max: 2, backoff: " + chr(34) + "100ms" + chr(34) + "}")
    source.down = True
    started = built.clock.monotonic()
    built.service.get("d1")
    assert source.calls == 3 and built.clock.monotonic() - started == 0.2  # two backoffs of 100 ms (manual time)


def test_after_the_threshold_the_breaker_stops_calls_for_every_document_and_one_trial_follows_the_cool_down(
    backend: Storage,
) -> None:
    built, source = setup(backend, retry="{max: 0}")
    source.down = True
    for n in range(5):  # five failed calls to the same source, on five documents
        built.service.append(f"b{n}", [put("/name", f"N{n}")], contributor=ALICE)
        built.service.get(f"b{n}")
    assert source.calls == 5
    built.service.append("b9", [put("/name", "N9")], contributor=ALICE)
    refused = built.service.get("b9")
    assert source.calls == 5 and refused.deferred[0].error == "breaker_open"  # no call: the breaker is open
    built.clock.advance(29)
    built.service.get("b9", consistency="fresh")
    assert source.calls == 5
    built.clock.advance(1)  # the cool-down is over: one trial call
    source.down = False
    built.service.get("b9", consistency="fresh")
    assert source.calls == 6 and built.service.get("b9").deferred[0].status == "ok"
    built.service.append("c0", [put("/name", "C0")], contributor=ALICE)
    built.service.get("c0")
    assert source.calls == 7  # closed again: calls flow


def test_a_failed_trial_keeps_the_breaker_open_for_another_cool_down(backend: Storage) -> None:
    built, source = setup(backend, retry="{max: 0}")
    source.down = True
    for n in range(5):
        built.service.append(f"b{n}", [put("/name", f"N{n}")], contributor=ALICE)
        built.service.get(f"b{n}")
    built.clock.advance(30)
    built.service.append("t", [put("/name", "T")], contributor=ALICE)
    built.service.get("t")
    assert source.calls == 6  # the trial, which failed
    built.service.append("u", [put("/name", "U")], contributor=ALICE)
    built.service.get("u")
    assert source.calls == 6  # open again


def test_a_later_success_clears_the_failure(backend: Storage) -> None:
    built, source = setup(backend, "serve_stale")
    built.service.get("d1")
    source.down = True
    built.clock.advance(301)
    assert built.service.get("d1").deferred[0].status == "stale"
    source.down = False
    built.clock.advance(301)  # the failed attempt waits for the next ttl, then the source is asked again
    result = built.service.get("d1")
    assert (result.deferred[0].status, result.deferred[0].error) == ("ok", None)
    record = built.stored()["/score"]
    assert (record.status, record.error, record.invalidated) == ("ok", None, False)


def test_a_failed_attempt_is_not_repeated_inside_the_ttl(backend: Storage) -> None:
    built, source = setup(backend, retry="{max: 0}")
    source.down = True
    built.service.get("d1")
    built.service.get("d1")
    built.service.get("d1")
    assert source.calls == 1  # a down source is not asked again on every read


def test_a_failure_does_not_affect_a_rebuild(backend: Storage) -> None:
    built, source = setup(backend, "serve_stale")
    built.service.get("d1")
    source.down = True
    built.clock.advance(301)
    built.service.get("d1")
    before = source.calls
    assert built.service.rebuild("d1").identical and source.calls == before


def test_a_refusal_is_not_an_attempt_so_the_next_read_after_the_cool_down_asks_without_waiting_for_the_ttl(
    backend: Storage,
) -> None:
    built, source = setup(backend, retry="{max: 0}")
    source.down = True
    for n in range(5):
        built.service.append(f"b{n}", [put("/name", f"N{n}")], contributor=ALICE)
        built.service.get(f"b{n}")
    built.service.append("b9", [put("/name", "N9")], contributor=ALICE)
    assert built.service.get("b9").deferred[0].error == "breaker_open" and source.calls == 5
    source.down = False
    built.clock.advance(30)  # the cool-down is over, the 5 minute ttl is not
    assert built.service.get("b9").deferred[0].status == "ok" and source.calls == 6


def test_a_rate_limit_refusal_is_asked_again_as_soon_as_a_call_is_allowed(backend: Storage) -> None:
    from aduku.behaviors.registry import BehaviorRegistry
    from tests.fixtures import CountingHook

    def register(behaviors: BehaviorRegistry) -> None:
        behaviors.register_hook("fx.limited", 1, CountingHook("fx.limited", lambda i, n: 5), rate_limit=1.0)

    fields = ", ".join(f'"/{n}": {deferred(hook="fx.limited@1", refresh="per_fetch", ttl=TTL)}' for n in "ab")
    built = build(backend, fields, extra=register, sources=[])
    built.service.append("d1", [put("/name", "Alice")], contributor=ALICE)
    built.service.get("d1")  # one field gets the token, the other is refused
    assert built.calls("fx.limited@1") == 1
    built.clock.advance(1)
    result = built.service.get("d1")  # the refused one is not held for the ttl
    assert built.calls("fx.limited@1") == 2 and [d.status for d in result.deferred] == ["ok", "ok"]
