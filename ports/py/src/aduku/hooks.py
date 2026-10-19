"""Calling impure hooks (SPEC 9.3.1, feature 002 research R-8). The edge of the engine: this module does IO and uses time.

A hook is called on its own daemon thread so that a timeout can abandon a call that does not return; hooks are idempotent
by contract, so an abandoned call is harmless. The pure packages never import this module (checked by a test).
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Literal

from aduku.ion_codec import IonError, to_canonical
from aduku.schema.model import ImpureSpec

__all__ = ["HookResult", "HookRunner"]


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


class HookRunner:
    def __init__(
        self,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._sleep = sleep
        self._monotonic = monotonic

    def run(self, call: Callable[..., Any], spec: ImpureSpec, inputs: Mapping[str, Any]) -> HookResult:
        """Call `call(inputs, timeout)` up to 1 + retry_max times within the deadline; the first value wins."""
        started = self._monotonic()
        attempts = 0
        last_error = "error"
        while attempts <= spec.retry_max:
            remaining = spec.deadline - (self._monotonic() - started)
            if remaining <= 0:
                return HookResult("failure", None, "deadline", attempts)
            timeout = min(spec.timeout, remaining)
            attempts += 1
            attempt = self._attempt(call, inputs, timeout)
            if attempt is None:
                last_error = "timeout"
            elif attempt.failed:
                last_error = "error"
            else:
                return self._checked(attempt.value, attempts)
            if attempts <= spec.retry_max and spec.backoff > 0:
                self._sleep(spec.backoff)
        return HookResult("failure", None, last_error, attempts)

    def _checked(self, value: Any, attempts: int) -> HookResult:
        if value is None:
            return HookResult("failure", None, "bad_value", attempts)
        try:
            to_canonical(value)
        except (TypeError, IonError):
            return HookResult("failure", None, "bad_value", attempts)
        return HookResult("value", value, None, attempts)

    def _attempt(self, call: Callable[..., Any], inputs: Mapping[str, Any], timeout: float) -> _Attempt | None:
        attempt = _Attempt()

        def body() -> None:
            try:
                attempt.value = call(dict(inputs), timeout)
            except BaseException:  # noqa: BLE001 - whatever a hook raises is a failed attempt, never a crash of the engine
                attempt.failed = True
            finally:
                attempt.done.set()

        thread = threading.Thread(target=body, name="aduku-hook", daemon=True)
        thread.start()
        return attempt if attempt.done.wait(timeout) else None
