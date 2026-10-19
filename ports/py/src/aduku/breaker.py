"""The circuit breaker and rate limiter of deferred sources (SPEC 9.5, feature 003 research R-10). An edge module.

One breaker per source (name and version), shared by every field and document that uses it: when the system behind the
source is down, all calls to it stop together. After the cool-down exactly one trial call is allowed; its result closes
the breaker or opens it for another cool-down. State lives in memory in one service instance. Time is a function passed
in (monotonic seconds), so a test moves it by hand.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Literal

__all__ = ["Breaker", "BreakerBank", "RateLimiter"]

State = Literal["closed", "open", "half_open"]
OnChange = Callable[[str, str, str], None]

DEFAULT_THRESHOLD = 5  # consecutive failed calls (each after its retries) that open the breaker
DEFAULT_COOLDOWN = 30.0  # seconds an open breaker refuses calls


class Breaker:
    def __init__(
        self,
        key: str,
        clock: Callable[[], float],
        threshold: int = DEFAULT_THRESHOLD,
        cooldown: float = DEFAULT_COOLDOWN,
        on_change: OnChange | None = None,
    ) -> None:
        if threshold < 1 or cooldown <= 0:
            raise ValueError("a breaker needs a threshold of at least 1 and a cool-down above zero")
        self.key = key
        self._clock = clock
        self._threshold = threshold
        self._cooldown = cooldown
        self._on_change = on_change
        self._lock = threading.Lock()
        self._state: State = "closed"
        self._failures = 0
        self._since = 0.0  # when it opened, or when the trial in flight started

    @property
    def state(self) -> State:
        return self._state

    def allow(self) -> bool:
        """True if a call may be made now. Over an open breaker the first caller after the cool-down gets the trial."""
        with self._lock:
            if self._state == "closed":
                return True
            now = self._clock()
            if now - self._since < self._cooldown:
                return False  # still cooling down, or a trial is in flight and has not yet outlived a cool-down
            self._set("half_open")
            self._since = now
            return True

    def success(self) -> None:
        with self._lock:
            self._failures = 0
            if self._state != "closed":
                self._set("closed")

    def failure(self) -> None:
        with self._lock:
            if self._state == "half_open":
                self._set("open")
                self._since = self._clock()
                return
            self._failures += 1
            if self._state == "closed" and self._failures >= self._threshold:
                self._set("open")
                self._since = self._clock()

    def _set(self, state: State) -> None:
        old, self._state = self._state, state
        if self._on_change is not None:
            self._on_change(self.key, old, state)


class BreakerBank:
    """The breakers of a service, made on first use, one per source key."""

    def __init__(
        self,
        clock: Callable[[], float],
        threshold: int = DEFAULT_THRESHOLD,
        cooldown: float = DEFAULT_COOLDOWN,
        on_change: OnChange | None = None,
    ) -> None:
        self._clock = clock
        self._threshold = threshold
        self._cooldown = cooldown
        self._on_change = on_change
        self._lock = threading.Lock()
        self._breakers: dict[str, Breaker] = {}

    def for_source(self, key: str) -> Breaker:
        with self._lock:
            breaker = self._breakers.get(key)
            if breaker is None:
                breaker = self._breakers[key] = Breaker(
                    key, self._clock, self._threshold, self._cooldown, self._on_change
                )
            return breaker


class RateLimiter:
    """At most `rate` calls per second, spaced evenly: one call, then none until `1 / rate` seconds have passed. With
    no rate it never refuses."""

    def __init__(self, rate: float | None, clock: Callable[[], float]) -> None:
        if rate is not None and not rate > 0:
            raise ValueError("a rate limit is above zero calls per second")
        self._interval = None if rate is None else 1.0 / rate
        self._clock = clock
        self._lock = threading.Lock()
        self._next = float("-inf")  # the earliest time the next call may be made

    def try_acquire(self) -> bool:
        """Take the next call if it may be made now (a read does not wait)."""
        if self._interval is None:
            return True
        with self._lock:
            now = self._clock()
            if now < self._next:
                return False
            self._next = now + self._interval
            return True

    def wait_time(self) -> float:
        """Seconds until the next call may be made (the scheduler waits, then acquires)."""
        if self._interval is None:
            return 0.0
        with self._lock:
            return max(0.0, self._next - self._clock())
