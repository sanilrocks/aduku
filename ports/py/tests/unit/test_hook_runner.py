"""Feature 002, research R-8: hooks run with a timeout per attempt, bounded retries, and an overall deadline."""

import threading
import time

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
    result = HookRunner().run(hook, spec(), {"/country": "US"})
    assert result == HookResult("value", "tax-US", None, 1)
    assert hook.given == [({"/country": "US"}, 1.0)]  # only the declared dependencies and the timeout


def test_a_failing_hook_is_retried_up_to_the_maximum_with_the_backoff_between_attempts() -> None:
    slept: list[float] = []
    runner = HookRunner(sleep=slept.append)
    hook = Script(RuntimeError("down"), RuntimeError("down"), "tax-US")
    result = runner.run(hook, spec(retry_max=2, backoff=0.25), {"/country": "US"})
    assert (result.outcome, result.value, result.attempts) == ("value", "tax-US", 3)
    assert slept == [0.25, 0.25]


def test_running_out_of_retries_is_a_failure_with_the_kind_and_the_attempts() -> None:
    hook = Script(RuntimeError("secret detail"), RuntimeError("secret detail"))
    result = HookRunner(sleep=lambda _: None).run(hook, spec(retry_max=1), {"/country": "US"})
    assert (result.outcome, result.value, result.attempts) == ("failure", None, 2)
    assert result.error == "error" and "secret" not in str(result)  # the kind only; never the message or a value


def test_no_retries_means_one_attempt() -> None:
    result = HookRunner().run(Script(RuntimeError("x")), spec(retry_max=0), {"/country": "US"})
    assert (result.outcome, result.attempts) == ("failure", 1)


def test_a_hook_that_takes_too_long_is_a_timeout_and_the_caller_is_not_blocked() -> None:
    release = threading.Event()

    def slow(inputs: dict[str, object], timeout: float) -> object:
        release.wait(5)
        return "late"

    started = time.monotonic()
    try:
        result = HookRunner().run(slow, spec(timeout=0.05, retry_max=1, deadline=2.0), {"/country": "US"})
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
        result = HookRunner().run(slow, spec(timeout=0.15, retry_max=5, deadline=0.2), {"/country": "US"})
    finally:
        release.set()
    assert result.outcome == "failure" and result.error in ("timeout", "deadline")
    assert len(calls) <= 2 and result.attempts == len(calls)


def test_each_attempt_is_given_no_more_than_the_time_left_before_the_deadline() -> None:
    hook = Script(RuntimeError("x"), "ok")
    times = iter([0.0, 0.0, 0.9])  # the start, then the clock at each attempt
    runner = HookRunner(sleep=lambda _: None, monotonic=lambda: next(times))
    result = runner.run(hook, spec(timeout=1.0, retry_max=1, deadline=1.0), {"/country": "US"})
    assert result.outcome == "value"
    assert hook.given[0][1] == 1.0 and abs(hook.given[1][1] - 0.1) < 1e-9


def test_a_value_that_cannot_be_stored_is_a_failure() -> None:
    result = HookRunner().run(Script(object()), spec(retry_max=0), {"/country": "US"})
    assert (result.outcome, result.error) == ("failure", "bad_value")


def test_a_hook_that_returns_none_is_a_failure() -> None:
    result = HookRunner().run(Script(None), spec(retry_max=0), {"/country": "US"})
    assert (result.outcome, result.error) == ("failure", "bad_value")


def test_a_hook_is_called_in_another_thread_so_that_a_timeout_can_abandon_it() -> None:
    seen: list[str] = []

    def hook(inputs: dict[str, object], timeout: float) -> object:
        seen.append(threading.current_thread().name)
        return "v"

    HookRunner().run(hook, spec(retry_max=0), {"/country": "US"})
    assert seen and seen[0] != threading.current_thread().name
