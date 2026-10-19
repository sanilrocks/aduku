"""Feature 002, research R-8: hooks run with a timeout per attempt, bounded retries, and an overall deadline."""

import threading
import time

import pytest

from aduku.hooks import HookResult, HookRunner
from aduku.schema.model import ImpureSpec


def spec(timeout: float = 1.0, retry_max: int = 2, backoff: float = 0.0, deadline: float = 5.0) -> ImpureSpec:
    return ImpureSpec(
        hook="h",
        version=1,
        depends=("/country",),
        reason="r",
        timeout=timeout,
        retry_max=retry_max,
        backoff=backoff,
        deadline=deadline,
        recompute_by=("admin",),
    )


class Script:
    """A hook that follows a script: each call raises or returns the next item, and notes what it was given."""

    def __init__(self, *steps: object) -> None:
        self.steps = list(steps)
        self.given: list[tuple[dict[str, object], float]] = []

    def __call__(self, inputs: dict[str, object], timeout: float) -> object:
        self.given.append((dict(inputs), timeout))
        step = self.steps.pop(0)
        if isinstance(step, BaseException):
            raise step
        return step


def test_a_hook_that_succeeds_is_called_once() -> None:
    hook = Script("tax-US")
    result = HookRunner().run(hook, spec().policy, {"/country": "US"})
    assert result == HookResult("value", "tax-US", None, 1)
    assert hook.given == [({"/country": "US"}, 1.0)]  # only the declared dependencies and the timeout


def test_a_failing_hook_is_retried_up_to_the_maximum_with_the_backoff_between_attempts() -> None:
    slept: list[float] = []
    runner = HookRunner(sleep=slept.append)
    hook = Script(RuntimeError("down"), RuntimeError("down"), "tax-US")
    result = runner.run(hook, spec(retry_max=2, backoff=0.25).policy, {"/country": "US"})
    assert (result.outcome, result.value, result.attempts) == ("value", "tax-US", 3)
    assert slept == [0.25, 0.25]


def test_running_out_of_retries_is_a_failure_with_the_kind_and_the_attempts() -> None:
    hook = Script(RuntimeError("secret detail"), RuntimeError("secret detail"))
    result = HookRunner(sleep=lambda _: None).run(hook, spec(retry_max=1).policy, {"/country": "US"})
    assert (result.outcome, result.value, result.attempts) == ("failure", None, 2)
    assert result.error == "error" and "secret" not in str(result)  # the kind only; never the message or a value


def test_no_retries_means_one_attempt() -> None:
    result = HookRunner().run(Script(RuntimeError("x")), spec(retry_max=0).policy, {"/country": "US"})
    assert (result.outcome, result.attempts) == ("failure", 1)


def test_a_hook_that_takes_too_long_is_a_timeout_and_the_caller_is_not_blocked() -> None:
    release = threading.Event()

    def slow(inputs: dict[str, object], timeout: float) -> object:
        release.wait(5)
        return "late"

    started = time.monotonic()
    try:
        result = HookRunner().run(slow, spec(timeout=0.05, retry_max=1, deadline=2.0).policy, {"/country": "US"})
        elapsed = time.monotonic() - started
    finally:
        release.set()
    assert (result.outcome, result.error, result.attempts) == ("failure", "timeout", 2)
    assert elapsed < 1.0


def test_the_overall_deadline_stops_further_attempts() -> None:
    release = threading.Event()
    calls: list[int] = []

    def slow(inputs: dict[str, object], timeout: float) -> object:
        calls.append(1)
        release.wait(5)
        return "late"

    try:
        result = HookRunner().run(slow, spec(timeout=0.15, retry_max=5, deadline=0.2).policy, {"/country": "US"})
    finally:
        release.set()
    assert result.outcome == "failure" and result.error in ("timeout", "deadline")
    assert len(calls) <= 2 and result.attempts == len(calls)


def test_each_attempt_is_given_no_more_than_the_time_left_before_the_deadline() -> None:
    hook = Script(RuntimeError("x"), "ok")
    times = iter([0.0, 0.0, 0.9])  # the start, then the clock at each attempt
    runner = HookRunner(sleep=lambda _: None, monotonic=lambda: next(times))
    result = runner.run(hook, spec(timeout=1.0, retry_max=1, deadline=1.0).policy, {"/country": "US"})
    assert result.outcome == "value"
    assert hook.given[0][1] == 1.0 and abs(hook.given[1][1] - 0.1) < 1e-9


def test_a_value_that_cannot_be_stored_is_a_failure() -> None:
    result = HookRunner().run(Script(object()), spec(retry_max=0).policy, {"/country": "US"})
    assert (result.outcome, result.error) == ("failure", "bad_value")


def test_a_hook_that_returns_none_is_a_failure() -> None:
    result = HookRunner().run(Script(None), spec(retry_max=0).policy, {"/country": "US"})
    assert (result.outcome, result.error) == ("failure", "bad_value")


def test_a_hook_is_called_in_another_thread_so_that_a_timeout_can_abandon_it() -> None:
    seen: list[str] = []

    def hook(inputs: dict[str, object], timeout: float) -> object:
        seen.append(threading.current_thread().name)
        return "v"

    HookRunner().run(hook, spec(retry_max=0).policy, {"/country": "US"})
    assert seen and seen[0] != threading.current_thread().name


# -- feature 003: one call policy for impure computes and deferred sources (FR-002, FR-008, FR-017) ----------------------


def test_a_call_policy_is_what_the_runner_needs_and_both_kinds_of_field_have_one() -> None:
    from aduku.hooks import CallPolicy
    from aduku.schema.model import DeferredSpec, RefreshPolicy

    assert spec(timeout=2.0, retry_max=1, backoff=0.5, deadline=9.0).policy == CallPolicy(2.0, 1, 0.5, 9.0)
    deferred = DeferredSpec("h", 1, ("/a",), (RefreshPolicy("once"),), ("admin",))
    assert deferred.policy == CallPolicy(2.0, 2, 0.2, 6.0)  # the platform defaults
    hook = Script("v")
    assert HookRunner().run(hook, deferred.policy, {"/a": 1}).value == "v"


def test_a_read_deadline_caps_a_call_below_its_own_timeout() -> None:
    from aduku.hooks import CallPolicy

    clock = {"now": 100.0}
    runner = HookRunner(sleep=lambda _: None, monotonic=lambda: clock["now"])
    seen: list[float] = []

    def slow(inputs: dict[str, object], timeout: float) -> object:
        seen.append(timeout)
        time.sleep(0.5)
        return "late"

    started = time.monotonic()
    result = runner.run(slow, CallPolicy(30.0, 0, 0.0, 60.0), {"/a": 1}, deadline_at=100.05)
    assert result.outcome == "failure" and result.error == "timeout" and time.monotonic() - started < 0.4
    assert seen == [pytest.approx(0.05)]  # the call was given the time left, not its own 30 seconds


def test_a_read_deadline_already_past_makes_no_call() -> None:
    from aduku.hooks import CallPolicy

    hook = Script("never")
    runner = HookRunner(monotonic=lambda: 10.0)
    result = runner.run(hook, CallPolicy(1.0, 3, 0.0, 5.0), {}, deadline_at=9.0)
    assert (result.outcome, result.error, result.attempts) == ("failure", "deadline", 0) and hook.given == []


def test_a_source_that_asks_for_it_gets_the_call_context() -> None:
    from aduku.hooks import CallPolicy

    got: list[dict[str, object]] = []

    def hook(inputs: dict[str, object], timeout: float, ctx: dict[str, object]) -> object:
        got.append(dict(ctx))
        if len(got) < 3:
            raise RuntimeError("again")
        return "ok"

    result = HookRunner(sleep=lambda _: None).run(
        hook, CallPolicy(1.0, 2, 0.0, 5.0), {"/a": 1}, context={"doc": "d1", "field": "/score"}
    )
    assert result.outcome == "value" and [c["attempt"] for c in got] == [1, 2, 3]
    assert all(c["doc"] == "d1" and c["field"] == "/score" and 0 < c["deadline"] <= 5.0 for c in got)  # type: ignore[operator]


def test_a_batch_call_gives_one_result_per_item() -> None:
    from aduku.hooks import CallPolicy

    def batch(items: list[dict[str, object]], timeout: float) -> list[object]:
        return [f"band-{item['/country']}" for item in items[:1]] + [ValueError("no data"), None]

    results = HookRunner().run_batch(
        batch, CallPolicy(1.0, 0, 0.0, 5.0), [{"/country": "US"}, {"/country": "DE"}, {"/country": "FR"}]
    )
    assert [(r.outcome, r.value, r.error) for r in results] == [
        ("value", "band-US", None),
        ("failure", None, "error"),
        ("failure", None, "bad_value"),
    ]
    assert {r.attempts for r in results} == {1}


def test_a_batch_that_answers_the_wrong_number_or_raises_fails_every_item_after_its_retries() -> None:
    from aduku.hooks import CallPolicy

    wrong = HookRunner().run_batch(lambda items, timeout: ["x"], CallPolicy(1.0, 0, 0.0, 5.0), [{}, {}])
    assert [(r.outcome, r.error) for r in wrong] == [("failure", "bad_value")] * 2
    calls: list[int] = []

    def boom(items: list[dict[str, object]], timeout: float) -> list[object]:
        calls.append(len(items))
        raise RuntimeError("down")

    broken = HookRunner(sleep=lambda _: None).run_batch(boom, CallPolicy(1.0, 1, 0.0, 5.0), [{}, {}, {}])
    assert [(r.outcome, r.error, r.attempts) for r in broken] == [("failure", "error", 2)] * 3 and calls == [3, 3]


def test_an_answer_that_arrives_after_the_caller_gave_up_is_handed_to_on_late() -> None:
    from aduku.hooks import CallPolicy

    late: list[object] = []
    arrived = threading.Event()

    def slow(inputs: dict[str, object], timeout: float) -> object:
        time.sleep(0.15)
        return "late-answer"

    def keep(value: object) -> None:
        late.append(value)
        arrived.set()

    result = HookRunner().run(slow, CallPolicy(0.03, 0, 0.0, 5.0), {}, on_late=keep)
    assert (result.outcome, result.error) == ("failure", "timeout") and late == []
    assert arrived.wait(2.0) and late == ["late-answer"]


def test_an_answer_in_time_is_not_late() -> None:
    from aduku.hooks import CallPolicy

    late: list[object] = []
    result = HookRunner().run(lambda inputs, timeout: "on-time", CallPolicy(1.0, 0, 0.0, 5.0), {}, on_late=late.append)
    time.sleep(0.05)
    assert result.value == "on-time" and late == []
