"""US3: a deferred field that needs another deferred field is asked only once that field has a value, in a fixed order,
and a source is given only its declared dependencies (FR-002, FR-013, FR-014, SC-005)."""

import time

from aduku.behaviors.registry import BehaviorRegistry
from aduku.storage.base import Storage
from tests.deferred_helpers import build, deferred, deferred_value, delayed, together
from tests.fixtures import CountingHook
from tests.service_helpers import ALICE, put

TTL = chr(34) + "5m" + chr(34)


def scored(log: list[str], name: str, answer: object):  # type: ignore[no-untyped-def]
    """An `extra` registering a counting source that notes its name in `log` each time it is called."""

    def register(behaviors: BehaviorRegistry) -> None:
        def call(inputs: dict[str, object], attempt: int) -> object:
            log.append(name)
            if callable(answer):
                return answer(inputs)
            return answer

        behaviors.register_hook(name, 1, CountingHook(name, call))

    return register


class Switch:
    """A source that fails while `down` is set."""

    def __init__(self, log: list[str], answer: object) -> None:
        self.down = True
        self.calls = 0
        self.log = log
        self.answer = answer

    def __call__(self, inputs: dict[str, object], timeout: float) -> object:
        self.calls += 1
        self.log.append("score")
        if self.down:
            raise RuntimeError("down")
        return self.answer


def test_a_dependent_is_not_asked_while_its_upstream_has_no_value_and_follows_it_once_it_does(backend: Storage) -> None:
    log: list[str] = []
    switch = Switch(log, 712)

    def register(behaviors: BehaviorRegistry) -> None:
        behaviors.register_hook("fx.flip", 1, switch)
        behaviors.register_hook("fx.limit", 1, CountingHook("fx.limit", lambda i, n: log.append("limit") or 7120))

    fields = (
        f'"/creditScore": {deferred(hook="fx.flip@1", retry="{max: 0}")}, '
        f'"/creditLimit": {deferred(hook="fx.limit@1", depends=chr(34) + "/creditScore" + chr(34) + ", " + chr(34) + "/country" + chr(34))}'
    )
    built = build(backend, fields, extra=register, sources=[])
    built.service.append("d1", [put("/name", "Alice"), put("/country", "US")], contributor=ALICE)
    down = built.service.get("d1", include=("pending",))
    statuses = {d.path: d for d in down.deferred}
    assert statuses["/creditScore"].status == "error"
    assert (statuses["/creditLimit"].status, statuses["/creditLimit"].reason) == ("pending", "upstream")
    assert statuses["/creditLimit"].waiting_for == ("/creditScore",)
    assert log == ["score"] and built.calls("fx.limit@1") == 0  # the dependent's source was never called
    switch.down = False
    built.service.append("d1", [put("/name", "Bob")], contributor=ALICE)  # a new question for the upstream
    up = built.service.get("d1")
    assert deferred_value(up, "creditScore") == 712 and deferred_value(up, "creditLimit") == 7120
    assert log == ["score", "score", "limit"]  # the upstream first, then the dependent


def test_a_chain_is_fetched_in_dependency_order_whatever_the_declaration_order(backend: Storage) -> None:
    log: list[str] = []
    fields = [
        f'"/c": {deferred(hook="fx.c@1", depends=chr(34) + "/b" + chr(34))}',
        f'"/a": {deferred(hook="fx.a@1")}',
        f'"/b": {deferred(hook="fx.b@1", depends=chr(34) + "/a" + chr(34) + ", " + chr(34) + "/country" + chr(34))}',
        f'"/z": {deferred(hook="fx.a@1")}',
    ]
    extra = together(scored(log, "fx.a", 1), scored(log, "fx.b", 2), scored(log, "fx.c", 3))
    built = build(backend, ", ".join(fields), extra=extra, sources=[])
    built.service.append("d1", [put("/name", "Alice"), put("/country", "US")], contributor=ALICE)
    result = built.service.get("d1")
    assert [deferred_value(result, key) for key in ("a", "b", "c", "z")] == [1, 2, 3, 1]
    assert log.index("fx.a") < log.index("fx.b") < log.index("fx.c") and len(log) == 4  # a (twice: /a and /z), b, c


def test_a_source_is_given_only_its_declared_dependencies_and_the_context_it_asked_for(backend: Storage) -> None:
    seen: list[dict[str, object]] = []

    def register(behaviors: BehaviorRegistry) -> None:
        behaviors.register_hook("fx.score", 1, CountingHook("fx.score", lambda i, n: 712))

        def with_context(inputs: dict[str, object], timeout: float, ctx: dict[str, object]) -> object:
            seen.append({"inputs": dict(inputs), "ctx": dict(ctx)})
            return int(inputs["/creditScore"]) * 10  # type: ignore[call-overload]

        behaviors.register_hook("fx.limit", 1, with_context, with_context=True)

    fields = (
        f'"/creditScore": {deferred()}, '
        f'"/creditLimit": {deferred(hook="fx.limit@1", depends=chr(34) + "/creditScore" + chr(34) + ", " + chr(34) + "/country" + chr(34))}'
    )
    built = build(backend, fields, extra=register, sources=[])
    built.service.append(
        "d1", [put("/name", "Alice"), put("/country", "US"), put("/currency", "EUR")], contributor=ALICE
    )
    built.service.get("d1")
    (call,) = seen
    assert call["inputs"] == {"/creditScore": 712, "/country": "US"}  # not /name, not /currency
    ctx = call["ctx"]
    assert (ctx["doc"], ctx["field"], ctx["attempt"]) == ("d1", "/creditLimit", 1) and 0 < ctx["deadline"] <= 6.0  # type: ignore[operator]


def test_a_once_dependent_keeps_its_value_when_the_upstream_refreshes_unless_it_opted_in(backend: Storage) -> None:
    for opt_in, expected in ((False, 1), (True, 2)):
        log: list[str] = []
        counter = iter(range(1, 100))
        fields = (
            f'"/rate": {deferred(hook="fx.up@1", refresh="per_fetch", ttl=TTL)}, '
            f'"/limit": {deferred(hook="fx.down@1", depends=chr(34) + "/rate" + chr(34), **({"on_dependency_change": "recompute"} if opt_in else {}))}'
        )
        extra = together(scored(log, "fx.up", lambda inputs, c=counter: next(c)), scored(log, "fx.down", 5))
        built = build(backend, fields, extra=extra, sources=[], prefix=f"p{opt_in}", schema_state=False)
        doc = f"d{opt_in}"
        built.service.append(doc, [put("/name", "Alice")], contributor=ALICE)
        built.service.get(doc)
        built.clock.advance(301)  # the upstream is past its ttl: it answers 2 now
        result = built.service.get(doc)
        assert deferred_value(result, "rate") == 2 and log.count("fx.down") == expected


def test_a_deferred_field_may_read_computed_and_impure_fields(backend: Storage) -> None:
    impure = (
        '{type: string, computed: {impure: {hook: "fx.echo@1", depends: ["/country"], reason: "registry", '
        'timeout: "1s", retry: {max: 0}, deadline: "2s", recompute_by: [admin]}}}'
    )
    seen: list[dict[str, object]] = []

    def register(behaviors: BehaviorRegistry) -> None:
        behaviors.register_hook("fx.mix", 1, CountingHook("fx.mix", lambda i, n: seen.append(dict(i)) or "mixed"))

    fields = (
        f'"/tax": {impure}, '
        f'"/mixed": {deferred(hook="fx.mix@1", type_="string", depends=chr(34) + "/shout" + chr(34) + ", " + chr(34) + "/tax" + chr(34))}'
    )
    built = build(backend, fields, extra=register)
    built.service.append("d1", [put("/name", "Alice"), put("/country", "US")], contributor=ALICE)
    assert deferred_value(built.service.get("d1"), "mixed") == "mixed"
    assert seen == [{"/shout": "ALICE", "/tax": "tax-US"}]  # the computed value and the recorded impure value, plain


def test_a_chain_shares_the_deadline_and_the_unfinished_field_follows_its_policy(backend: Storage) -> None:
    first = f'"/a": {deferred(hook="fx.slowish@1", refresh="per_fetch", ttl=TTL)}'
    second = f'"/b": {deferred(hook="fx.never@1", depends=chr(34) + "/a" + chr(34), refresh="per_fetch", ttl=TTL)}'
    built = build(
        backend,
        f"{first}, {second}",
        extra=together(delayed("fx.slowish", 0.1), delayed("fx.never", None)),
        real_time=True,
        fetch_deadline=0.3,
    )
    built.service.append("d1", [put("/name", "Alice")], contributor=ALICE)
    started = time.monotonic()
    result = built.service.get("d1")
    assert time.monotonic() - started < 0.3 + 0.4  # one deadline for both, not 0.1 + a full wait for the second
    assert {d.path: d.status for d in result.deferred} == {"/a": "ok", "/b": "error"}
    assert deferred_value(result, "a") == 1
