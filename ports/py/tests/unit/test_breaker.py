"""Feature 003, research R-10: the circuit breaker (one per source) and the rate limiter (FR-017, SC-008)."""

import threading

import pytest

from aduku.breaker import Breaker, BreakerBank, RateLimiter


class Time:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def make(threshold: int = 5, cooldown: float = 30.0) -> tuple[Breaker, Time, list[tuple[str, str, str]]]:
    clock = Time()
    changes: list[tuple[str, str, str]] = []
    breaker = Breaker("fx.rate@1", clock, threshold, cooldown, lambda key, old, new: changes.append((key, old, new)))
    return breaker, clock, changes


def fail(breaker: Breaker, times: int) -> None:
    for _ in range(times):
        assert breaker.allow()
        breaker.failure()


def test_it_starts_closed_and_lets_every_call_through() -> None:
    breaker, _, changes = make()
    assert breaker.state == "closed" and all(breaker.allow() for _ in range(20)) and changes == []


def test_it_opens_after_the_threshold_of_consecutive_failures_and_not_before() -> None:
    breaker, _, changes = make()
    fail(breaker, 4)
    assert breaker.state == "closed"
    fail(breaker, 1)
    assert breaker.state == "open" and changes == [("fx.rate@1", "closed", "open")]


def test_a_success_resets_the_count() -> None:
    breaker, _, _ = make()
    fail(breaker, 4)
    assert breaker.allow()
    breaker.success()
    fail(breaker, 4)
    assert breaker.state == "closed"


def test_an_open_breaker_refuses_until_the_cool_down_has_passed_exactly() -> None:
    breaker, clock, _ = make()
    fail(breaker, 5)
    clock.now = 29.99
    assert breaker.allow() is False and breaker.state == "open"
    clock.now = 30.0
    assert breaker.allow() is True and breaker.state == "half_open"


def test_exactly_one_trial_call_is_allowed_and_the_others_wait_for_its_result() -> None:
    breaker, clock, _ = make()
    fail(breaker, 5)
    clock.now = 31.0
    assert [breaker.allow() for _ in range(4)] == [True, False, False, False]


def test_a_successful_trial_closes_the_breaker_and_a_failed_one_opens_it_for_a_new_cool_down() -> None:
    breaker, clock, changes = make()
    fail(breaker, 5)
    clock.now = 31.0
    assert breaker.allow()
    breaker.success()
    assert breaker.state == "closed" and breaker.allow()
    fail(breaker, 5)
    clock.now = 62.0
    assert breaker.allow()
    breaker.failure()
    assert breaker.state == "open" and breaker.allow() is False
    clock.now = 91.9
    assert breaker.allow() is False  # a new cool-down from the failed trial at 62
    clock.now = 92.0
    assert breaker.allow() is True
    assert [(old, new) for _, old, new in changes] == [
        ("closed", "open"), ("open", "half_open"), ("half_open", "closed"),
        ("closed", "open"), ("open", "half_open"), ("half_open", "open"), ("open", "half_open"),
    ]  # fmt: skip


def test_a_trial_that_never_reports_does_not_block_the_source_for_ever() -> None:
    breaker, clock, _ = make()
    fail(breaker, 5)
    clock.now = 31.0
    assert breaker.allow()  # the trial, abandoned
    clock.now = 50.0
    assert breaker.allow() is False
    clock.now = 61.0
    assert breaker.allow() is True  # another trial after a further cool-down


def test_a_refusal_is_not_a_failure() -> None:
    breaker, clock, _ = make()
    fail(breaker, 5)
    for _ in range(10):
        assert breaker.allow() is False
    clock.now = 31.0
    assert breaker.allow()
    breaker.success()
    assert breaker.state == "closed"


def test_two_threads_cannot_both_take_the_trial() -> None:
    breaker, clock, _ = make()
    fail(breaker, 5)
    clock.now = 31.0
    granted: list[bool] = []
    start = threading.Barrier(8)

    def attempt() -> None:
        start.wait()
        granted.append(breaker.allow())

    threads = [threading.Thread(target=attempt) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert granted.count(True) == 1


def test_one_breaker_per_source_shared_by_everything_that_uses_it() -> None:
    clock = Time()
    changes: list[tuple[str, str, str]] = []
    bank = BreakerBank(clock, 2, 10.0, lambda key, old, new: changes.append((key, old, new)))
    assert bank.for_source("fx.rate@1") is bank.for_source("fx.rate@1")
    for _ in range(2):
        assert bank.for_source("fx.rate@1").allow()
        bank.for_source("fx.rate@1").failure()
    assert bank.for_source("fx.rate@1").state == "open" and bank.for_source("fx.rate@2").state == "closed"
    assert changes == [("fx.rate@1", "closed", "open")]


@pytest.mark.parametrize("threshold, cooldown", [(0, 30.0), (3, 0.0), (3, -1.0)])
def test_nonsense_settings_are_refused(threshold: int, cooldown: float) -> None:
    with pytest.raises(ValueError):
        Breaker("h", Time(), threshold, cooldown)


# -- the rate limiter --------------------------------------------------------------------------------------------


def test_a_rate_limiter_lets_one_call_through_per_interval() -> None:
    clock = Time()
    limiter = RateLimiter(2.0, clock)  # two calls a second: one every half second
    assert limiter.try_acquire() is True
    assert limiter.try_acquire() is False
    clock.now = 0.49
    assert limiter.try_acquire() is False
    clock.now = 0.5
    assert limiter.try_acquire() is True and limiter.try_acquire() is False


def test_it_says_how_long_until_the_next_call_may_be_made() -> None:
    clock = Time()
    limiter = RateLimiter(4.0, clock)
    assert limiter.wait_time() == 0.0
    assert limiter.try_acquire()
    clock.now = 0.1
    assert limiter.wait_time() == pytest.approx(0.15)
    clock.now = 0.25
    assert limiter.wait_time() == 0.0


def test_no_limit_never_refuses() -> None:
    limiter = RateLimiter(None, Time())
    assert all(limiter.try_acquire() for _ in range(1000)) and limiter.wait_time() == 0.0


def test_a_rate_must_be_above_zero() -> None:
    with pytest.raises(ValueError):
        RateLimiter(0.0, Time())
