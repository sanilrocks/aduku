"""Calling hooks (SPEC 9.3.1 and 9.5; features 002 and 003). The edge of the engine: this module does IO and uses time.

A hook is called on its own daemon thread so that a timeout can abandon a call that does not return; hooks are idempotent
by contract, so an abandoned call is harmless. Impure computes and deferred sources share one call policy (timeout per
attempt, bounded retries with a fixed backoff, an overall deadline); a read may also cap a call with its own deadline.
The pure packages never import this module (checked by a test).
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

from aduku.ion_codec import IonError, to_canonical
from aduku.schema.model import CallPolicy

__all__ = ["CallPolicy", "HookResult", "HookRunner"]


@dataclass(frozen=True)
class HookResult:
    """What a hook came to after its attempts. `error` is the kind of failure ("error", "timeout", "deadline",
    "bad_value"); it never carries a message or a value, which could hold sensitive data."""

    outcome: Literal["value", "failure"]
    value: Any = None
    error: str | None = None
    attempts: int = 1


class _Attempt:
    def __init__(self) -> None:
        self.done = threading.Event()
        self.value: Any = None
        self.failed = False
        self.lock = threading.Lock()
        self.abandoned = False  # the caller stopped waiting


class HookRunner:
    def __init__(
        self,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._sleep = sleep
        self._monotonic = monotonic

    def run(
        self,
        call: Callable[..., Any],
        policy: CallPolicy,
        inputs: Mapping[str, Any],
        *,
        deadline_at: float | None = None,
        context: Mapping[str, Any] | None = None,
        on_late: Callable[[Any], None] | None = None,
    ) -> HookResult:
        """Call `call(inputs, timeout)` (or `call(inputs, timeout, ctx)` when a `context` is given) up to 1 + retry_max
        times within the policy's deadline and, when given, before the monotonic time `deadline_at`; the first value wins.

        `on_late(value)` is called, on the hook's own thread, with the answer of an attempt that finishes after the
        caller stopped waiting for it (an abandoned call that turned out to succeed)."""

        def make(timeout: float, number: int, remaining: float) -> _Attempt | None:
            if context is None:
                return self._attempt(lambda: call(dict(inputs), timeout), timeout, on_late)
            ctx = {**context, "attempt": number, "deadline": remaining}
            return self._attempt(lambda: call(dict(inputs), timeout, ctx), timeout, on_late)

        attempt, attempts, error = self._drive(policy, deadline_at, make)
        if attempt is None:
            return HookResult("failure", None, error, attempts)
        return self._checked(attempt.value, attempts)

    def run_batch(
        self,
        call: Callable[..., Any],
        policy: CallPolicy,
        items: Sequence[Mapping[str, Any]],
        *,
        deadline_at: float | None = None,
        context: Mapping[str, Any] | None = None,
        on_late: Callable[[Any], None] | None = None,
    ) -> list[HookResult]:
        """Call a batch source with all `items` at once; the answer has one element per item, a value or an `Exception`
        for that item. A call that fails, times out or answers the wrong number of elements fails every item."""
        listed = [dict(item) for item in items]

        def make(timeout: float, number: int, remaining: float) -> _Attempt | None:
            if context is None:
                return self._attempt(lambda: call(listed, timeout), timeout, on_late)
            ctx = {**context, "attempt": number, "deadline": remaining}
            return self._attempt(lambda: call(listed, timeout, ctx), timeout, on_late)

        attempt, attempts, error = self._drive(policy, deadline_at, make)
        if attempt is None:
            return [HookResult("failure", None, error, attempts) for _ in listed]
        answer = attempt.value
        if not isinstance(answer, list | tuple) or len(answer) != len(listed):
            return [HookResult("failure", None, "bad_value", attempts) for _ in listed]
        return [
            HookResult("failure", None, "error", attempts)
            if isinstance(element, BaseException)
            else self._checked(element, attempts)
            for element in answer
        ]

    def _drive(
        self,
        policy: CallPolicy,
        deadline_at: float | None,
        make: Callable[[float, int, float], _Attempt | None],
    ) -> tuple[_Attempt | None, int, str]:
        """The attempts, retries and backoff; returns the successful attempt (None if none), the attempts made and the
        kind of the last failure."""
        started = self._monotonic()
        attempts = 0
        last_error = "error"
        while attempts <= policy.retry_max:
            remaining = policy.deadline - (self._monotonic() - started)
            if deadline_at is not None:
                remaining = min(remaining, deadline_at - self._monotonic())
            if remaining <= 0:
                return None, attempts, "deadline"
            timeout = min(policy.timeout, remaining)
            attempts += 1
            attempt = make(timeout, attempts, remaining)
            if attempt is None:
                last_error = "timeout"
            elif attempt.failed:
                last_error = "error"
            else:
                return attempt, attempts, "error"
            if attempts <= policy.retry_max and policy.backoff > 0:
                left = policy.deadline - (self._monotonic() - started)
                if deadline_at is not None:
                    left = min(left, deadline_at - self._monotonic())
                if left > 0:  # never sleep past the deadline: the loop then ends the call
                    self._sleep(min(policy.backoff, left))
        return None, attempts, last_error

    def _checked(self, value: Any, attempts: int) -> HookResult:
        if value is None:
            return HookResult("failure", None, "bad_value", attempts)
        try:
            to_canonical(value)
        except (TypeError, IonError):
            return HookResult("failure", None, "bad_value", attempts)
        return HookResult("value", value, None, attempts)

    def _attempt(
        self, body_call: Callable[[], Any], timeout: float, on_late: Callable[[Any], None] | None = None
    ) -> _Attempt | None:
        attempt = _Attempt()

        def body() -> None:
            try:
                attempt.value = body_call()
            except BaseException:  # noqa: BLE001 - whatever a hook raises is a failed attempt, never a crash of the engine
                attempt.failed = True
                return
            finally:
                attempt.done.set()
            with attempt.lock:
                late = attempt.abandoned
            if late and on_late is not None:
                try:
                    on_late(attempt.value)
                except BaseException:  # noqa: BLE001 - keeping a late answer is best effort
                    return

        thread = threading.Thread(target=body, name="aduku-hook", daemon=True)
        thread.start()
        if attempt.done.wait(timeout):
            return attempt
        with attempt.lock:
            if attempt.done.is_set():  # it finished just as the wait ran out: on time
                return attempt
            attempt.abandoned = True
        return None
