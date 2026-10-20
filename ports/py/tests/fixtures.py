"""Fixture behaviors that conformance cases name (conformance/README.md)."""

from __future__ import annotations

import threading
from collections.abc import Callable, Sequence
from datetime import datetime, timedelta
from decimal import Decimal

from aduku.behaviors.registry import BehaviorRegistry, Laws
from aduku.behaviors.validate import Subject
from aduku.ion_codec import ABSENT
from aduku.model import Violation
from aduku.storage.base import FakeClock


def max_spouses_by_country(subject: Subject) -> list[Violation]:
    """Document rule: at most one spouse when /country is "US"."""
    document = subject.document
    spouses = document.get("spouses")
    if document.get("country") == "US" and isinstance(spouses, tuple) and len(spouses) > 1:
        return [Violation("max_spouses_by_country", "/spouses", "at most 1 spouse is allowed in US")]
    return []


def subtract(path: object, old: object, new: object, ctx: object) -> object:
    """Fixture merge: old minus new. Not associative, and declared as such."""
    return new if old is ABSENT else old - new  # type: ignore[operator]


def longest(path: object, old: object, new: object, ctx: object) -> object:
    """Fixture merge: the longer string wins, ties go to the larger string. Associative, commutative, idempotent."""
    if old is ABSENT:
        return new
    return max(old, new, key=lambda text: (len(text), text))  # type: ignore[arg-type]


def add(path: object, old: object, new: object, ctx: object) -> object:
    """Fixture merge: a running total. Associative and commutative, and not idempotent (a repeat counts twice)."""
    return new if old is ABSENT else old + new  # type: ignore[operator]


MERGES = {
    "acme.add@1": ("acme.add", 1, add, Laws(associative=True, commutative=True)),
    "acme.subtract@1": ("acme.subtract", 1, subtract, Laws()),
    "acme.longest@1": ("acme.longest", 1, longest, Laws(associative=True, commutative=True, idempotent=True)),
}


class CountingHook:
    """A hook that counts its calls (conformance fixtures `fx.*`, feature 002). `calls` counts every call; `inputs` keeps
    the arguments of each, so a test can check that a hook is given only its declared dependencies."""

    def __init__(self, name: str, behavior: Callable[[dict[str, object], int], object]) -> None:
        self.name = name
        self._behavior = behavior
        self.calls = 0
        self.inputs: list[dict[str, object]] = []
        self._seen: dict[str, int] = {}

    def __call__(self, inputs: dict[str, object], timeout: float) -> object:
        self.calls += 1
        self.inputs.append(dict(inputs))
        key = repr(sorted(inputs.items()))
        self._seen[key] = self._seen.get(key, 0) + 1
        return self._behavior(inputs, self._seen[key])


def _echo(inputs: dict[str, object], attempt: int) -> object:
    return "tax-" + str(inputs["/country"])


def _fail(inputs: dict[str, object], attempt: int) -> object:
    raise RuntimeError("the registry is down")


def _flaky(inputs: dict[str, object], attempt: int) -> object:
    if attempt == 1:
        raise RuntimeError("try again")
    return "tax-" + str(inputs["/country"])


HOOKS = {"fx.echo@1": ("fx.echo", _echo), "fx.fail@1": ("fx.fail", _fail), "fx.flaky@1": ("fx.flaky", _flaky)}


def hook_of(behaviors: BehaviorRegistry, name: str, version: int = 1) -> CountingHook:
    """The counting hook registered under `name` (to read its call count)."""
    behavior = behaviors.get(name, version)
    assert behavior is not None and isinstance(behavior.impl, CountingHook), name
    return behavior.impl


def register_fixtures(behaviors: BehaviorRegistry, names: list[str] | None = None) -> None:
    wanted = names if names is not None else ["acme.max_spouses_by_country@1", "acme.label@1", *MERGES, *HOOKS]
    for key in wanted:
        if key == "acme.label@1":
            if behaviors.get("acme.label", 1) is None:
                behaviors.register_macro("acme.label", 1, ["name", "city"], 'name + " (" + city + ")"')
        elif key in DEFERRED_SOURCES:
            register_sources(behaviors, [key])
        elif key in HOOKS:
            name, behavior = HOOKS[key]
            if behaviors.get(name, 1) is None:
                behaviors.register_hook(name, 1, CountingHook(name, behavior))
        elif key in MERGES:
            name, version, combine, laws = MERGES[key]
            if behaviors.get(name, version) is None:
                behaviors.register_merge(name, version, combine, laws)
        elif behaviors.get("acme.max_spouses_by_country", 1) is None:
            behaviors.register_validate("acme.max_spouses_by_country", 1, max_spouses_by_country)


# -- feature 003: deferred sources and a manual clock ------------------------------------------------------------


class ManualClock(FakeClock):
    """One clock for the service (`now`) and for the deferred stage (`monotonic`, `sleep`): time moves only when a test
    advances it, so ttl, schedules and cool-downs are exact."""

    def __init__(self, start: datetime | None = None, step_seconds: float = 0.0) -> None:
        super().__init__(start, step_seconds)
        self._origin = self._now

    def advance(self, seconds: float) -> None:
        self._now += timedelta(seconds=seconds)

    def monotonic(self) -> float:
        return (self._now - self._origin).total_seconds()

    def sleep(self, seconds: float) -> None:
        self.advance(seconds)


def _score(inputs: dict[str, object], attempt: int) -> object:
    return 712


def _rate(inputs: dict[str, object], attempt: int) -> object:
    return Decimal("0.92")


def _rate2(inputs: dict[str, object], attempt: int) -> object:
    return Decimal("0.92") if attempt == 1 else Decimal("0.93")


def _limit(inputs: dict[str, object], attempt: int) -> object:
    score = inputs["/creditScore"]
    assert isinstance(score, int)
    return score * 10


def _down(inputs: dict[str, object], attempt: int) -> object:
    raise RuntimeError("the source is down")


class BatchHook:
    """A batch source (`batch_max` 50): one call carries a list of inputs and answers one value per item; it records
    the size of every batch. A call of one item is what a read makes."""

    BATCH_MAX = 50

    def __init__(self, name: str, batch_max: int = BATCH_MAX) -> None:
        self.name = name
        self.batch_max = batch_max
        self.calls = 0
        self.batches: list[int] = []
        self.fail_calls: set[int] = set()  # the nth calls (counting retries) raise: a batch the source breaks on

    def __call__(self, items: Sequence[dict[str, object]], timeout: float) -> list[object]:
        self.calls += 1
        self.batches.append(len(items))
        if len(self.batches) in self.fail_calls:
            raise RuntimeError("the source broke in the middle of a run")
        return ["band-" + str(item.get("/country")) for item in items]


class SlowHook(CountingHook):
    """A source that does not answer in time: it blocks until `release` is set (tests give it millisecond deadlines)."""

    def __init__(self, name: str) -> None:
        super().__init__(name, lambda inputs, attempt: 0)
        self.release = threading.Event()

    def __call__(self, inputs: dict[str, object], timeout: float) -> object:
        self.calls += 1
        self.inputs.append(dict(inputs))
        self.release.wait(30)
        return 0


DEFERRED_HOOKS = {
    "fx.score@1": ("fx.score", _score),
    "fx.rate@1": ("fx.rate", _rate),
    "fx.rate2@1": ("fx.rate2", _rate2),
    "fx.limit@1": ("fx.limit", _limit),
    "fx.down@1": ("fx.down", _down),
}


DEFERRED_SOURCES = (*DEFERRED_HOOKS, "fx.batch@1", "fx.pair@1", "fx.paced@1", "fx.slow@1")


def register_sources(behaviors: BehaviorRegistry, names: Sequence[str] | None = None) -> None:
    """Register the feature 003 fixture sources (conformance/README.md); `names` limits which."""
    wanted = (
        list(names)
        if names is not None
        else [*DEFERRED_HOOKS, "fx.batch@1", "fx.pair@1", "fx.paced@1", "fx.slow@1", "fx.flaky@1"]
    )
    for key in wanted:
        name, version = key.split("@")
        if behaviors.get(name, int(version)) is not None:
            continue
        if key == "fx.batch@1":
            behaviors.register_hook(name, 1, BatchHook(name), batch_max=BatchHook.BATCH_MAX)
        elif key == "fx.paced@1":  # a batch of one and one call a second, for cases that meet the rate limit
            behaviors.register_hook(name, 1, BatchHook(name, 1), batch_max=1, rate_limit=1.0)
        elif key == "fx.pair@1":  # the same source with a batch maximum of two, for vectors that stay readable
            behaviors.register_hook(name, 1, BatchHook(name, 2), batch_max=2)
        elif key == "fx.slow@1":
            behaviors.register_hook(name, 1, SlowHook(name))
        elif key in DEFERRED_HOOKS:
            behaviors.register_hook(name, 1, CountingHook(name, DEFERRED_HOOKS[key][1]))
        else:
            register_fixtures(behaviors, [key])
