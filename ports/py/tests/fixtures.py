"""Fixture behaviors that conformance cases name (conformance/README.md)."""

from __future__ import annotations

from collections.abc import Callable

from aduku.behaviors.registry import BehaviorRegistry, Laws
from aduku.behaviors.validate import Subject
from aduku.ion_codec import ABSENT
from aduku.model import Violation


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


MERGES = {
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
